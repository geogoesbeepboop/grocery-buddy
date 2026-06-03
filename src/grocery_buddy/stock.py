"""Pantry stock summary — load inventory and bucket it into low / medium / large.

Shared by /status (show the whole pantry, grouped by how soon it runs out) and any
caller that wants a human-readable picture of what's on hand. Reuses the same
prediction math the grocery run uses so the two never disagree.
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from grocery_buddy.predictor import (
    LARGE,
    LOW,
    MEDIUM,
    ConsumptionEvent,
    ConsumptionProfile,
    InventoryItem,
    StockLevel,
    classify_stock_levels,
)
from grocery_buddy.tools.consumption import (
    get_consumption_profile,
    get_recent_consumption_events,
)
from grocery_buddy.tools.inventory import get_inventory


async def summarize_stock(
    pool: asyncpg.Pool,
    user_id: str,
    lead_time_days: float = 2.0,
    buffer_days: float = 1.0,
) -> list[StockLevel]:
    """Load the user's pantry and classify each item by stock level."""
    inventory_rows = await get_inventory(pool, user_id)
    profile_rows = await get_consumption_profile(pool, user_id)
    event_rows = await get_recent_consumption_events(pool, user_id, lookback_days=30)

    inventory = [
        InventoryItem(
            product=i["product"],
            qty=float(i["qty"]),
            unit=i["unit"],
            par_level=float(i["par_level"]),
        )
        for i in inventory_rows
    ]
    profiles = [
        ConsumptionProfile(
            product=p["product"],
            declared_rate=float(p["declared_rate"]),
            unit=p["unit"],
            household_factor=float(p.get("household_factor") or 1.0),
        )
        for p in profile_rows
    ]

    events_by_product: dict[str, list[ConsumptionEvent]] = {}
    for e in event_rows:
        ts = e["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        events_by_product.setdefault(e["product"], []).append(
            ConsumptionEvent(delta=float(e["delta"]), ts=ts)
        )

    return classify_stock_levels(
        inventory, profiles, events_by_product, lead_time_days, buffer_days
    )


# ── Rendering (Telegram HTML) ───────────────────────────────────────────────────

_BUCKET_HEADERS = {
    LOW: "🔴 <b>Running low</b> — buy these soon",
    MEDIUM: "🟡 <b>Getting there</b> — keep an eye on these",
    LARGE: "🟢 <b>Well stocked</b>",
}
_BUCKET_ORDER = (LOW, MEDIUM, LARGE)


def _qty_str(level: StockLevel) -> str:
    unit = (level.unit or "").strip()
    return f"~{level.qty:g} {unit}".strip()


def _days_str(level: StockLevel) -> str:
    d = level.days_remaining
    if d != d or d == float("inf"):  # nan or no estimate available
        return ""
    if d < 1:
        return " · under a day left"
    n = round(d)
    return f" · ~{n} day{'s' if n != 1 else ''} left"


def format_stock_summary(levels: list[StockLevel]) -> str:
    """Render the full pantry grouped by bucket, most-urgent group first."""
    if not levels:
        return "📦 Your pantry is empty — send /start to set it up."

    groups: dict[str, list[StockLevel]] = {b: [] for b in _BUCKET_ORDER}
    for lv in levels:
        groups[lv.bucket].append(lv)

    parts = ["📦 <b>Your pantry</b>"]
    for bucket in _BUCKET_ORDER:
        items = groups[bucket]
        if not items:
            continue
        lines = [_BUCKET_HEADERS[bucket]]
        lines.extend(
            f"• {lv.product} — {_qty_str(lv)}{_days_str(lv)}" for lv in items
        )
        parts.append("\n".join(lines))

    return "\n\n".join(parts)
