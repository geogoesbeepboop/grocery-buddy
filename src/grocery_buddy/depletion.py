"""Estimated-quantity depletion — the "assume they kept eating" arithmetic.

When a scheduled grocery checkup runs, we don't actually know how much the user
consumed since we last looked. We assume they kept going through each item at its
usual effective rate and decrement the running ESTIMATE accordingly:

    consumed = effective_daily_rate × days_since(last_estimated_at)
    new_estimate = max(0, qty - consumed)

This only ever moves the estimate (``inventory_items.qty``). The user's last
confirmed quantity (``actual_qty``) is left untouched — it's the anchor we snap
back to when they correct us (see tools.inventory.set_actual_quantity).

Each non-trivial decrement is logged as an ``inferred`` consumption event for the
audit trail. By source, inferred events are excluded from the observed-rate blend
(see predictor.effective_daily_rate), so the model's own arithmetic can never feed
back on the rate it's derived from.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from grocery_buddy.predictor import (
    ConsumptionEvent,
    ConsumptionProfile,
    effective_daily_rate,
)
from grocery_buddy.tools.consumption import (
    get_consumption_profile,
    get_recent_consumption_events,
)
from grocery_buddy.tools.inventory import get_inventory

logger = logging.getLogger(__name__)

# Skip decrements smaller than this so we don't churn the DB on tiny elapsed
# windows. Because last_estimated_at is only advanced when we DO write, fractional
# consumption keeps accumulating against the old anchor until it crosses this — so
# nothing is lost, we just batch it.
_MIN_DECREMENT = 0.01


def _as_utc(ts) -> datetime:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


async def apply_estimated_depletion(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    """Decay every tracked item's estimate by its assumed consumption since last seen.

    Returns a list of ``{product, before, after, consumed, days}`` for each item
    actually decremented (useful for logging / debugging). Items without a declared
    rate, or with too little elapsed time to matter, are left alone.
    """
    inventory = await get_inventory(pool, user_id)
    profiles = await get_consumption_profile(pool, user_id)
    events = await get_recent_consumption_events(pool, user_id, lookback_days=30)

    profile_map = {
        p["product"]: ConsumptionProfile(
            product=p["product"],
            declared_rate=float(p["declared_rate"]),
            unit=p["unit"],
            household_factor=float(p.get("household_factor") or 1.0),
        )
        for p in profiles
    }
    events_by_product: dict[str, list[ConsumptionEvent]] = {}
    for e in events:
        events_by_product.setdefault(e["product"], []).append(
            ConsumptionEvent(
                delta=float(e["delta"]),
                ts=_as_utc(e["ts"]),
                source=e.get("source", "user_update"),
            )
        )

    now = datetime.now(UTC)
    applied: list[dict] = []

    for item in inventory:
        profile = profile_map.get(item["product"])
        if profile is None:
            continue  # no declared rate → nothing to extrapolate from

        rate = effective_daily_rate(profile, events_by_product.get(item["product"], []))
        if rate <= 0:
            continue

        last = _as_utc(item["last_estimated_at"])
        elapsed_days = (now - last).total_seconds() / 86_400.0
        if elapsed_days <= 0:
            continue

        consumed = rate * elapsed_days
        if consumed < _MIN_DECREMENT:
            continue  # accumulate against the same anchor until it's worth a write

        before = float(item["qty"])
        after = max(0.0, before - consumed)
        if abs(before - after) < _MIN_DECREMENT:
            continue

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE inventory_items
                    SET qty = $3, last_estimated_at = $4, updated_at = NOW()
                    WHERE user_id = $1 AND product = $2
                    """,
                    UUID(user_id), item["product"], after, now,
                )
                await conn.execute(
                    "INSERT INTO consumption_events (user_id, product, delta, source) "
                    "VALUES ($1, $2, $3, 'inferred')",
                    UUID(user_id), item["product"], after - before,
                )

        applied.append({
            "product": item["product"],
            "before": round(before, 3),
            "after": round(after, 3),
            "consumed": round(before - after, 3),
            "days": round(elapsed_days, 2),
        })

    if applied:
        logger.info("Depleted %d estimated quantities for %s", len(applied), user_id)
    return applied
