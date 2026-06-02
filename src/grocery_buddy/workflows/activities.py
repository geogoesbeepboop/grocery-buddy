"""Temporal activities — all I/O and side-effects live here (workflows stay pure)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from temporalio import activity

from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.models import (
    BuildCartInput,
    DraftCart,
    LookupInput,
    LowItem,
    NotificationInput,
    PricedItem,
    PurchaseInput,
    UpdateCartInput,
    UserPreferences,
)
from grocery_buddy.notifications import (
    send_approval_push,
    send_error_notification,
    send_purchase_confirmation,
)
from grocery_buddy.predictor import (
    ConsumptionEvent,
    ConsumptionProfile,
    InventoryItem,
    predict_low_items,
)
from grocery_buddy.tools.consumption import (
    get_consumption_profile,
    get_recent_consumption_events,
)
from grocery_buddy.tools.inventory import get_inventory

logger = logging.getLogger(__name__)


# ── T9: Data loading ──────────────────────────────────────────────────────────


@activity.defn
async def load_user_data(user_id: str) -> dict:
    """Load inventory, consumption profiles, recent events, and preferences."""
    pool = await get_pool()
    inventory = await get_inventory(pool, user_id)
    profiles = await get_consumption_profile(pool, user_id)
    events = await get_recent_consumption_events(pool, user_id, lookback_days=30)

    row = await pool.fetchrow(
        "SELECT * FROM preferences WHERE user_id = $1",
        uuid.UUID(user_id),
    )
    prefs = dict(row) if row else {}

    return {
        "user_id": user_id,
        "inventory": inventory,
        "profiles": profiles,
        "events": events,
        "auto_purchase_cap": float(prefs.get("auto_purchase_cap_usd", settings.auto_purchase_cap_usd)),
        "lead_time_days": float(prefs.get("lead_time_days", 2.0)),
        "buffer_days": float(prefs.get("buffer_days", 1.0)),
    }


# ── T6: Prediction ────────────────────────────────────────────────────────────


@activity.defn
async def predict_low_items_activity(user_data: dict) -> list[dict]:
    """Run the rule-based predictor; return list of low items."""
    inventory = [
        InventoryItem(
            product=i["product"],
            qty=float(i["qty"]),
            unit=i["unit"],
            par_level=float(i["par_level"]),
        )
        for i in user_data["inventory"]
    ]
    profiles = [
        ConsumptionProfile(
            product=p["product"],
            declared_rate=float(p["declared_rate"]),
            unit=p["unit"],
            household_factor=float(p.get("household_factor", 1.0)),
        )
        for p in user_data["profiles"]
    ]

    events_by_product: dict[str, list[ConsumptionEvent]] = {}
    for e in user_data["events"]:
        ts = e["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        events_by_product.setdefault(e["product"], []).append(
            ConsumptionEvent(delta=float(e["delta"]), ts=ts)
        )

    low = predict_low_items(
        inventory,
        profiles,
        events_by_product,
        lead_time_days=user_data.get("lead_time_days", 2.0),
        buffer_days=user_data.get("buffer_days", 1.0),
    )

    return [
        {
            "product": r.product,
            "qty": r.qty,
            "unit": r.unit,
            "days_remaining": r.days_remaining,
            "par_level": r.par_level,
        }
        for r in low
    ]


# ── T8: Amazon pricing ────────────────────────────────────────────────────────


@activity.defn
async def lookup_amazon_prices(payload: dict) -> list[dict]:
    """Look up Amazon prices for each low item using Playwright."""
    from grocery_buddy.automation.amazon import get_browser_context, search_grocery_price

    user_id = payload["user_id"]
    items = payload["items"]

    p, context = await get_browser_context()
    priced: list[dict] = []
    try:
        for item in items:
            result = await search_grocery_price(item["product"], context)
            if result:
                priced.append({
                    "product": item["product"],
                    "qty": item["par_level"],  # buy up to par
                    "unit": item["unit"],
                    "price_usd": result["price_usd"],
                    "price_source": "amazon_scraped",
                    "asin": result.get("asin"),
                    "kroger_sku": None,
                    "notes": f"Amazon search: {result['product']}",
                })
            else:
                logger.warning("No Amazon price found for %r — skipping", item["product"])
    finally:
        await context.close()
        await p.stop()

    return priced


# ── T13: Kroger price comparison ──────────────────────────────────────────────


@activity.defn
async def lookup_kroger_prices(payload: dict) -> list[dict]:
    """Fetch Kroger prices via their public Products API for price comparison."""
    import httpx

    items = payload["items"]
    kroger_token = payload.get("kroger_token", "")
    if not kroger_token:
        logger.info("No Kroger token — skipping price comparison")
        return []

    priced: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for item in items:
            try:
                resp = await client.get(
                    "https://api.kroger.com/v1/products",
                    params={"filter.term": item["product"], "filter.limit": 1},
                    headers={"Authorization": f"Bearer {kroger_token}", "Accept": "application/json"},
                )
                data = resp.json()
                products = data.get("data", [])
                if not products:
                    continue
                p = products[0]
                price_info = p.get("items", [{}])[0].get("price", {})
                reg_price = price_info.get("regular")
                if reg_price is None:
                    continue
                priced.append({
                    "product": p.get("description", item["product"]),
                    "qty": item["par_level"],
                    "unit": item["unit"],
                    "price_usd": float(reg_price),
                    "price_source": "kroger_api",
                    "asin": None,
                    "kroger_sku": p.get("productId"),
                    "notes": None,
                })
            except Exception as exc:
                logger.warning("Kroger price lookup failed for %r: %s", item["product"], exc)

    return priced


# ── T9: Cart building ─────────────────────────────────────────────────────────


@activity.defn
async def build_draft_cart(payload: dict) -> dict:
    """Persist a draft cart to the DB and return its ID + total."""
    user_id = payload["user_id"]
    priced_items: list[dict] = payload["priced_items"]
    workflow_id: str = payload.get("workflow_id", "")

    total = sum(i["price_usd"] * i["qty"] for i in priced_items if i.get("price_usd"))
    pool = await get_pool()

    cart_row = await pool.fetchrow(
        """
        INSERT INTO carts (user_id, status, total_usd, retailer, workflow_id)
        VALUES ($1, 'draft', $2, 'amazon', $3)
        RETURNING id, total_usd
        """,
        uuid.UUID(user_id), round(total, 2), workflow_id,
    )
    cart_id = str(cart_row["id"])

    for item in priced_items:
        await pool.execute(
            """
            INSERT INTO cart_items
                (cart_id, product, qty, unit, price_usd, price_source, asin, kroger_sku, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(cart_id),
            item["product"], item["qty"], item["unit"],
            round(item.get("price_usd") or 0, 2),
            item.get("price_source"),
            item.get("asin"),
            item.get("kroger_sku"),
            item.get("notes"),
        )

    logger.info("Built draft cart %s: $%.2f (%d items)", cart_id, total, len(priced_items))
    return {"cart_id": cart_id, "total_usd": round(total, 2), "item_count": len(priced_items), "retailer": "amazon"}


# ── T10: Notifications ────────────────────────────────────────────────────────


@activity.defn
async def send_approval_notification(payload: dict) -> None:
    await send_approval_push(
        cart_id=payload["cart_id"],
        total_usd=payload["total_usd"],
        item_count=payload["item_count"],
        workflow_id=payload["workflow_id"],
    )


# ── T11/T12: Cart status + purchase ──────────────────────────────────────────


@activity.defn
async def update_cart_status(payload: dict) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE carts SET status = $1, updated_at = NOW() WHERE id = $2",
        payload["status"], uuid.UUID(payload["cart_id"]),
    )


@activity.defn
async def execute_purchase_activity(payload: dict) -> dict:
    """Execute the Amazon checkout with idempotency protection.

    Idempotency: checks whether a purchase with this key already exists before
    executing — re-runs are safe. Returns the purchase record.
    """
    from grocery_buddy.automation.amazon import (
        add_to_cart_by_asin,
        get_browser_context,
        proceed_to_checkout,
    )

    cart_id = payload["cart_id"]
    user_id = payload["user_id"]
    idempotency_key = payload["idempotency_key"]
    pool = await get_pool()

    # Idempotency guard
    existing = await pool.fetchrow(
        "SELECT * FROM purchases WHERE idempotency_key = $1", idempotency_key
    )
    if existing and existing["status"] == "completed":
        logger.info("Purchase %s already completed — skipping", idempotency_key)
        return {"status": "completed", "idempotency_key": idempotency_key, "already_done": True}

    # Insert pending purchase record
    purchase_row = await pool.fetchrow(
        """
        INSERT INTO purchases (cart_id, idempotency_key, status)
        VALUES ($1, $2, 'pending')
        ON CONFLICT (idempotency_key) DO UPDATE SET status = 'pending'
        RETURNING id
        """,
        uuid.UUID(cart_id), idempotency_key,
    )
    purchase_id = str(purchase_row["id"])

    # Fetch cart items
    items = await pool.fetch(
        "SELECT * FROM cart_items WHERE cart_id = $1", uuid.UUID(cart_id)
    )

    p, context = await get_browser_context()
    try:
        # Add items to cart
        for item in items:
            asin = item["asin"]
            if not asin:
                logger.warning("No ASIN for %s — cannot add to cart", item["product"])
                continue
            success = await add_to_cart_by_asin(asin, context)
            if not success:
                raise RuntimeError(f"Failed to add {item['product']} (ASIN {asin}) to cart")

        # Proceed to checkout (gets checkout URL without placing order)
        checkout_url = await proceed_to_checkout(context)

        # Update purchase record
        total_usd = float(await pool.fetchval(
            "SELECT total_usd FROM carts WHERE id = $1", uuid.UUID(cart_id)
        ) or 0)

        await pool.execute(
            """
            UPDATE purchases
            SET status = 'completed', retailer_order_ref = $2, total_usd = $3
            WHERE id = $1
            """,
            uuid.UUID(purchase_id), checkout_url or "checkout_initiated", round(total_usd, 2),
        )
        await pool.execute(
            "UPDATE carts SET status = 'purchased', updated_at = NOW() WHERE id = $1",
            uuid.UUID(cart_id),
        )

        await send_purchase_confirmation(cart_id, total_usd, checkout_url)
        logger.info("Purchase completed for cart %s", cart_id)
        return {"status": "completed", "idempotency_key": idempotency_key, "total_usd": total_usd}

    except Exception as exc:
        await pool.execute(
            "UPDATE purchases SET status = 'failed', error = $2 WHERE id = $1",
            uuid.UUID(purchase_id), str(exc),
        )
        await pool.execute(
            "UPDATE carts SET status = 'failed', updated_at = NOW() WHERE id = $1",
            uuid.UUID(cart_id),
        )
        await send_error_notification(f"Purchase failed: {exc}")
        raise
    finally:
        await context.close()
        await p.stop()


@activity.defn
async def send_purchase_confirmation_activity(payload: dict) -> None:
    await send_purchase_confirmation(
        cart_id=payload["cart_id"],
        total_usd=payload["total_usd"],
        order_ref=payload.get("order_ref"),
    )
