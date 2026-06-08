"""In-transit replenishments — the "on the way" half of the pantry.

The predictor (predictor.py) answers "what's running low *right now*". But once the
user confirms they placed a staged order, those items are no longer a need — they're
en route. If the agent forgets that, tomorrow's run re-suggests eggs the user already
bought today. This module is the missing memory:

    confirm order  →  record_replenishments()   (cart lines → in-transit rows, eta = now + lead)
    next run       →  get_incoming_by_product()  (prediction counts in-transit as covered stock)
    eta passes     →  reconcile_arrivals()        (in-transit → on-hand: restock the pantry)

Two invariants make this safe to call from several places (a button tap, a text
reply, a durable workflow timer, and the top of every scheduled run all converge
here):

  • record_replenishments is idempotent per cart — a confirm can't double-book.
  • reconcile_arrivals lands each row exactly once — the 'in_transit' → 'arrived'
    flip is the claim, guarded in-transaction, so overlapping reconciles can't
    double-restock.

This mirrors the procurement-agent's purchase lifecycle (open mandate → settled
purchase → consumption history); see docs/PROCUREMENT_CONVERGENCE.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg

from grocery_buddy.products import normalize_product

logger = logging.getLogger(__name__)


# ── Pure: the shipping-time arithmetic ────────────────────────────────────────


def eta_for(ordered_at: datetime, lead_time_days: float) -> datetime:
    """Estimated arrival = order time + the user's delivery lead time.

    Isolated and pure so the one piece of math the whole feature hinges on is
    unit-tested without a database. Negative lead times are clamped to "arrives now".
    """
    return ordered_at + timedelta(days=max(0.0, lead_time_days))


# ── Confirm: stage → ordered (record in-transit) ──────────────────────────────


async def record_replenishments(
    pool: asyncpg.Pool,
    user_id: str,
    cart_id: str,
    lead_time_days: float = 2.0,
    ordered_at: datetime | None = None,
) -> dict:
    """Mark a staged cart as actually ordered; log every line as in-transit.

    Called when the user confirms they placed the Amazon order (button tap or text
    reply), and again — harmlessly — by the workflow's confirm signal. Idempotent per
    cart: if in-transit rows already exist for it, returns the existing summary
    instead of recording a second batch.

    Side effects on first confirm:
      • one in_transit row per cart line, eta = ordered_at + lead_time_days
      • cart → 'purchased', its purchase record → 'completed'

    Returns ``{already, eta, count, items: [{product, qty, unit}]}``.
    """
    cart_uuid = UUID(cart_id)
    now = ordered_at or datetime.now(timezone.utc)
    eta = eta_for(now, lead_time_days)

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetch(
                "SELECT product, qty, unit, eta FROM pending_replenishments "
                "WHERE cart_id = $1 AND status = 'in_transit' ORDER BY product",
                cart_uuid,
            )
            already = bool(existing)

            if not already:
                lines = await conn.fetch(
                    "SELECT product, qty, unit FROM cart_items WHERE cart_id = $1",
                    cart_uuid,
                )
                for it in lines:
                    await conn.execute(
                        """
                        INSERT INTO pending_replenishments
                            (user_id, cart_id, product, qty, unit, ordered_at, eta, status)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, 'in_transit')
                        ON CONFLICT (cart_id, product) WHERE status = 'in_transit'
                        DO NOTHING
                        """,
                        UUID(user_id), cart_uuid, normalize_product(it["product"]),
                        float(it["qty"]), it["unit"] or "unit", now, eta,
                    )
                await conn.execute(
                    "UPDATE carts SET status = 'purchased', updated_at = NOW() WHERE id = $1",
                    cart_uuid,
                )
                await conn.execute(
                    "UPDATE purchases SET status = 'completed' "
                    "WHERE cart_id = $1 AND status IN ('checkout_ready', 'pending')",
                    cart_uuid,
                )

            # Build the summary from the rows that are *actually* in transit for this
            # cart — not from what we think we inserted. This stays correct even when a
            # concurrent confirm wins the race and our INSERTs all hit ON CONFLICT (the
            # ETA/items returned are the recorded ones, not our later `now`).
            rows = existing or await conn.fetch(
                "SELECT product, qty, unit, eta FROM pending_replenishments "
                "WHERE cart_id = $1 AND status = 'in_transit' ORDER BY product",
                cart_uuid,
            )

    items = [{"product": r["product"], "qty": float(r["qty"]), "unit": r["unit"]} for r in rows]
    eta_out = rows[0]["eta"].isoformat() if rows else eta.isoformat()
    logger.info(
        "Recorded %d in-transit items for cart %s (already=%s)", len(items), cart_id, already
    )
    return {"already": already, "eta": eta_out, "count": len(items), "items": items}


# ── Prediction inputs: what's already on the way ──────────────────────────────


async def get_incoming_by_product(pool: asyncpg.Pool, user_id: str) -> dict[str, float]:
    """Map canonical product → total in-transit qty, for prediction.

    Prediction adds this to on-hand stock so a confirmed order is never re-suggested
    while it's still in transit.
    """
    rows = await pool.fetch(
        "SELECT product, SUM(qty) AS qty FROM pending_replenishments "
        "WHERE user_id = $1 AND status = 'in_transit' GROUP BY product",
        UUID(user_id),
    )
    return {normalize_product(r["product"]): float(r["qty"]) for r in rows}


async def get_in_transit(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    """In-transit items with ETAs, soonest first — for /status and the predictor."""
    rows = await pool.fetch(
        "SELECT product, qty, unit, eta, ordered_at FROM pending_replenishments "
        "WHERE user_id = $1 AND status = 'in_transit' ORDER BY eta",
        UUID(user_id),
    )
    return [
        {
            "product": r["product"],
            "qty": float(r["qty"]),
            "unit": r["unit"],
            "eta": r["eta"],
            "ordered_at": r["ordered_at"],
        }
        for r in rows
    ]


# ── Arrival: in-transit → on-hand (restock the pantry) ────────────────────────


async def reconcile_arrivals(
    pool: asyncpg.Pool, user_id: str, now: datetime | None = None
) -> list[dict]:
    """Land every in-transit order whose ETA has passed: restock the pantry.

    For each due row, in one transaction: claim it (flip 'in_transit' → 'arrived',
    which is also the idempotency guard), add its qty to the pantry estimate, re-anchor
    depletion to now so the just-arrived stock isn't retroactively decayed, and log a
    'purchase' consumption event for the audit trail.

    Returns the landed lines ``[{product, qty, unit}]`` (empty if nothing was due).
    Safe to call from overlapping callers — ``FOR UPDATE SKIP LOCKED`` + the status
    flip mean a row is never landed twice.
    """
    now = now or datetime.now(timezone.utc)
    landed: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE pending_replenishments
                SET status = 'arrived', arrived_at = $2
                WHERE id IN (
                    SELECT id FROM pending_replenishments
                    WHERE user_id = $1 AND status = 'in_transit' AND eta <= $2
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING product, qty, unit
                """,
                UUID(user_id), now,
            )
            for r in rows:
                product = normalize_product(r["product"])
                qty = float(r["qty"])
                unit = r["unit"] or "unit"
                # Add to the running estimate (upsert-add) and re-anchor last_estimated_at
                # so the new stock starts depleting from arrival, not from the past.
                await conn.execute(
                    """
                    INSERT INTO inventory_items
                        (user_id, product, qty, actual_qty, unit, par_level,
                         last_estimated_at, updated_at)
                    VALUES ($1, $2, $3, NULL, $4, GREATEST($3, 1), NOW(), NOW())
                    ON CONFLICT (user_id, product) DO UPDATE
                    SET qty = inventory_items.qty + EXCLUDED.qty,
                        unit = EXCLUDED.unit,
                        last_estimated_at = NOW(), updated_at = NOW()
                    """,
                    UUID(user_id), product, qty, unit,
                )
                await conn.execute(
                    "INSERT INTO consumption_events (user_id, product, delta, source) "
                    "VALUES ($1, $2, $3, 'purchase')",
                    UUID(user_id), product, qty,
                )
                landed.append({"product": product, "qty": qty, "unit": unit})

    if landed:
        logger.info("Landed %d arrivals for %s", len(landed), user_id)
    return landed


async def cancel_in_transit(
    pool: asyncpg.Pool,
    user_id: str,
    product: str | None = None,
    cart_id: str | None = None,
) -> list[dict]:
    """Cancel in-transit rows so they stop counting as incoming and never land.

    Used when the user tells us an order fell through ("it never came", "I cancelled
    that"). Scope by product (most specific) or by cart; with neither, cancels all
    in-transit rows for the user. Returns the cancelled lines.
    """
    clauses = ["user_id = $1", "status = 'in_transit'"]
    params: list = [UUID(user_id)]
    if product:
        params.append(normalize_product(product))
        clauses.append(f"product = ${len(params)}")
    if cart_id:
        params.append(UUID(cart_id))
        clauses.append(f"cart_id = ${len(params)}")

    rows = await pool.fetch(
        f"UPDATE pending_replenishments SET status = 'cancelled' "
        f"WHERE {' AND '.join(clauses)} RETURNING product, qty, unit",
        *params,
    )
    return [{"product": r["product"], "qty": float(r["qty"]), "unit": r["unit"]} for r in rows]
