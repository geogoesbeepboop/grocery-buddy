"""Pantry stock summary — load inventory and bucket it into low / medium / large.

Shared by /status (show the whole pantry, grouped by how soon it runs out) and any
caller that wants a human-readable picture of what's on hand. Reuses the same
prediction math the grocery run uses so the two never disagree.
"""
from __future__ import annotations

from datetime import UTC, datetime

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
    """Load the user's pantry and classify each item by stock level.

    Counts in-transit orders (confirmed but not yet arrived) as covered stock, so an
    item the user just ordered shows as well-stocked here and isn't re-flagged as low.
    """
    from grocery_buddy.replenishment import get_incoming_by_product

    inventory_rows = await get_inventory(pool, user_id)
    profile_rows = await get_consumption_profile(pool, user_id)
    event_rows = await get_recent_consumption_events(pool, user_id, lookback_days=30)
    incoming_by_product = await get_incoming_by_product(pool, user_id)

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
            ConsumptionEvent(delta=float(e["delta"]), ts=ts, source=e.get("source", "user_update"))
        )

    return classify_stock_levels(
        inventory, profiles, events_by_product, lead_time_days, buffer_days,
        incoming_by_product=incoming_by_product,
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
    """Render the pantry grouped by bucket, most-urgent group first.

    A big pantry (90+ items) is mostly well-stocked staples — listing every one
    overflows Telegram's 4096-char limit and buries what matters. So we lead with
    what's low and what's getting there (capped, with a "…and N more" tail), and
    collapse the well-stocked bucket to a count.
    """
    if not levels:
        return (
            "📦 Your pantry is empty. Say <b>/import</b> to build it from your recent "
            "Amazon orders (fastest), or <b>/start</b> to set it up by hand."
        )

    from grocery_buddy.config import settings

    cap = settings.status_max_items_per_bucket
    groups: dict[str, list[StockLevel]] = {b: [] for b in _BUCKET_ORDER}
    for lv in levels:
        groups[lv.bucket].append(lv)

    parts = ["📦 <b>Your pantry</b>"]
    for bucket in _BUCKET_ORDER:
        items = groups[bucket]
        if not items:
            continue
        # Well-stocked items are the bulk of a large pantry and the least useful to
        # enumerate — collapse them to a count.
        if bucket == LARGE:
            n = len(items)
            parts.append(f"{_BUCKET_HEADERS[LARGE]} — {n} item{'s' if n != 1 else ''}")
            continue
        shown = items if (not cap or len(items) <= cap) else items[:cap]
        lines = [_BUCKET_HEADERS[bucket]]
        lines.extend(f"• {lv.product} — {_qty_str(lv)}{_days_str(lv)}" for lv in shown)
        hidden = len(items) - len(shown)
        if hidden > 0:
            lines.append(f"…and {hidden} more")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _eta_phrase(eta: datetime) -> str:
    """Friendly relative ETA: 'today', 'tomorrow', 'in N days', else a short date."""

    now = datetime.now(eta.tzinfo or UTC)
    days = (eta.date() - now.date()).days
    if days <= 0:
        return "expected today"
    if days == 1:
        return "expected tomorrow"
    if days <= 6:
        return f"expected in {days} days"
    return f"expected {eta.strftime('%b %-d')}"


def format_in_transit(items: list[dict]) -> str:
    """Render the 'on the way' block for /status from get_in_transit() rows.

    Each row is ``{product, qty, unit, eta}``. Returns '' when nothing is in transit
    so the caller can omit the section entirely.
    """
    if not items:
        return ""
    lines = ["🚚 <b>On the way</b> — already ordered, I won't re-suggest these"]
    for it in items:
        name = (it.get("product") or "item").strip()
        qty = float(it.get("qty") or 1)
        unit = (it.get("unit") or "").strip()
        qty_str = f" ({qty:g} {unit})".rstrip() if (qty != 1 or unit) else ""
        eta = it.get("eta")
        eta_str = f" · {_eta_phrase(eta)}" if isinstance(eta, datetime) else ""
        lines.append(f"• {name}{qty_str}{eta_str}")
    return "\n".join(lines)
