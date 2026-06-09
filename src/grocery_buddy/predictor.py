"""Rule-based pantry predictor: days_left = qty / effective_daily_rate.

Blends a declared consumption rate (prior) with observed consumption events
(posterior). Observation weight grows as data accumulates, capping at 80%.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class InventoryItem:
    product: str
    qty: float
    unit: str
    par_level: float


@dataclass
class ConsumptionProfile:
    product: str
    declared_rate: float  # units per day
    unit: str
    household_factor: float = 1.0


@dataclass
class ConsumptionEvent:
    delta: float  # negative = consumed, positive = restocked
    ts: datetime
    # Only genuine 'user_update' consumption episodes inform the observed rate.
    # 'inferred' (the agent's own arithmetic depletion) and 'correction' (a user
    # resetting their absolute on-hand quantity, e.g. "family used them all") are
    # state signals, not recurring-consumption signals — counting them would let
    # the model feed back on itself or let a one-off spike corrupt the steady rate.
    source: str = "user_update"


@dataclass
class LowItemResult:
    product: str
    qty: float
    unit: str
    days_remaining: float
    effective_rate: float
    par_level: float
    # Qty already on the way (confirmed-but-not-yet-arrived orders). The urgency math
    # runs on qty + incoming, so a just-ordered item isn't flagged again — but we keep
    # incoming separate so callers can still show the real on-hand number.
    incoming: float = 0.0


# ── Stock-level buckets ─────────────────────────────────────────────────────────
# A single vocabulary shared by /status (what's in the pantry) and the grocery run
# (what to buy). "low" is exactly the predict_low_items set; "large" items are the
# ones a scheduled run safely skips.
LOW = "low"
MEDIUM = "medium"
LARGE = "large"

# Items with at most this many days of stock left (but more than the low
# threshold) are "medium"; more than this is "large".
MEDIUM_DAYS = 14.0


@dataclass
class StockLevel:
    product: str
    qty: float
    unit: str
    days_remaining: float  # float("inf") when we can't estimate (no declared rate)
    effective_rate: float
    par_level: float
    bucket: str  # LOW | MEDIUM | LARGE
    incoming: float = 0.0  # qty already on the way (counted toward the bucket)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def effective_daily_rate(
    profile: ConsumptionProfile,
    recent_events: list[ConsumptionEvent],
    lookback_days: int = 30,
) -> float:
    """Bayesian-style blend of declared rate and observed consumption."""
    base_rate = profile.declared_rate * profile.household_factor
    if not recent_events:
        return base_rate

    now = datetime.now(UTC)
    consumption_events = [
        e for e in recent_events
        if (now - _as_utc(e.ts)).days <= lookback_days
        and e.delta < 0
        and e.source == "user_update"
    ]

    if not consumption_events:
        return base_rate

    total_consumed = sum(-e.delta for e in consumption_events)
    observed_rate = total_consumed / lookback_days

    # Weight toward observed more as data accumulates; max 80% after 14+ events
    weight = min(len(consumption_events) / 14.0, 0.8)
    return (1.0 - weight) * base_rate + weight * observed_rate


def days_left(item: InventoryItem, rate: float) -> float:
    if rate <= 0:
        return float("inf")
    return item.qty / rate


def is_low(
    item: InventoryItem,
    rate: float,
    lead_time_days: float = 2.0,
    buffer_days: float = 1.0,
) -> bool:
    return days_left(item, rate) <= (lead_time_days + buffer_days)


def predict_low_items(
    inventory: list[InventoryItem],
    profiles: list[ConsumptionProfile],
    events_by_product: dict[str, list[ConsumptionEvent]],
    lead_time_days: float = 2.0,
    buffer_days: float = 1.0,
    incoming_by_product: dict[str, float] | None = None,
) -> list[LowItemResult]:
    """Return items that need restocking, sorted by urgency.

    ``incoming_by_product`` maps a product to qty already on the way (a confirmed
    order in transit). That qty is added to on-hand for the urgency test, so an item
    the user just ordered isn't flagged as low again until it's actually due to run
    out *after* the incoming order is accounted for.
    """
    profile_map = {p.product: p for p in profiles}
    incoming_map = incoming_by_product or {}
    low: list[LowItemResult] = []

    for item in inventory:
        profile = profile_map.get(item.product)
        incoming = incoming_map.get(item.product, 0.0)
        eff_qty = item.qty + incoming

        if profile is None:
            # No declared rate — flag if effective qty is at or below par level
            if eff_qty <= item.par_level:
                low.append(LowItemResult(
                    product=item.product,
                    qty=item.qty,
                    unit=item.unit,
                    days_remaining=0.0,
                    effective_rate=0.0,
                    par_level=item.par_level,
                    incoming=incoming,
                ))
            continue

        events = events_by_product.get(item.product, [])
        rate = effective_daily_rate(profile, events)
        d_left = float("inf") if rate <= 0 else eff_qty / rate

        if d_left <= (lead_time_days + buffer_days):
            low.append(LowItemResult(
                product=item.product,
                qty=item.qty,
                unit=item.unit,
                days_remaining=d_left,
                effective_rate=rate,
                par_level=item.par_level,
                incoming=incoming,
            ))

    return sorted(low, key=lambda x: x.days_remaining)


def classify_stock_levels(
    inventory: list[InventoryItem],
    profiles: list[ConsumptionProfile],
    events_by_product: dict[str, list[ConsumptionEvent]],
    lead_time_days: float = 2.0,
    buffer_days: float = 1.0,
    incoming_by_product: dict[str, float] | None = None,
    medium_days: float = MEDIUM_DAYS,
) -> list[StockLevel]:
    """Bucket every pantry item into low / medium / large stock.

    Same urgency math as ``predict_low_items`` (the LOW bucket is exactly the set
    that function returns), extended to cover the items we *don't* need to buy yet
    so /status can show the whole pantry and a scheduled run can skip the large
    ones. Items without a declared rate are bucketed by how their qty compares to
    their par level. ``incoming_by_product`` (qty already on the way) is added to
    on-hand for the bucket math, so a confirmed order shows as well-stocked rather
    than re-flagged. Sorted most-urgent first.
    """
    profile_map = {p.product: p for p in profiles}
    incoming_map = incoming_by_product or {}
    levels: list[StockLevel] = []

    for item in inventory:
        profile = profile_map.get(item.product)
        incoming = incoming_map.get(item.product, 0.0)
        eff_qty = item.qty + incoming

        if profile is None:
            # No declared rate — fall back to par-level ratio (on effective qty).
            rate = 0.0
            d_left = float("inf")
            par = item.par_level or 0.0
            if eff_qty <= par:
                bucket = LOW
            elif eff_qty <= 2 * par:
                bucket = MEDIUM
            else:
                bucket = LARGE
        else:
            events = events_by_product.get(item.product, [])
            rate = effective_daily_rate(profile, events)
            d_left = float("inf") if rate <= 0 else eff_qty / rate
            if d_left <= lead_time_days + buffer_days:
                bucket = LOW
            elif d_left <= medium_days:
                bucket = MEDIUM
            else:
                bucket = LARGE

        levels.append(StockLevel(
            product=item.product,
            qty=item.qty,
            unit=item.unit,
            days_remaining=d_left,
            effective_rate=rate,
            par_level=item.par_level,
            bucket=bucket,
            incoming=incoming,
        ))

    return sorted(levels, key=lambda x: x.days_remaining)
