"""Rule-based pantry predictor: days_left = qty / effective_daily_rate.

Blends a declared consumption rate (prior) with observed consumption events
(posterior). Observation weight grows as data accumulates, capping at 80%.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


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


@dataclass
class LowItemResult:
    product: str
    qty: float
    unit: str
    days_remaining: float
    effective_rate: float
    par_level: float


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def effective_daily_rate(
    profile: ConsumptionProfile,
    recent_events: list[ConsumptionEvent],
    lookback_days: int = 30,
) -> float:
    """Bayesian-style blend of declared rate and observed consumption."""
    base_rate = profile.declared_rate * profile.household_factor
    if not recent_events:
        return base_rate

    now = datetime.now(timezone.utc)
    consumption_events = [
        e for e in recent_events
        if (now - _as_utc(e.ts)).days <= lookback_days and e.delta < 0
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
) -> list[LowItemResult]:
    """Return items that need restocking, sorted by urgency."""
    profile_map = {p.product: p for p in profiles}
    low: list[LowItemResult] = []

    for item in inventory:
        profile = profile_map.get(item.product)

        if profile is None:
            # No declared rate — flag if qty is at or below par level
            if item.qty <= item.par_level:
                low.append(LowItemResult(
                    product=item.product,
                    qty=item.qty,
                    unit=item.unit,
                    days_remaining=0.0,
                    effective_rate=0.0,
                    par_level=item.par_level,
                ))
            continue

        events = events_by_product.get(item.product, [])
        rate = effective_daily_rate(profile, events)
        d_left = days_left(item, rate)

        if is_low(item, rate, lead_time_days, buffer_days):
            low.append(LowItemResult(
                product=item.product,
                qty=item.qty,
                unit=item.unit,
                days_remaining=d_left,
                effective_rate=rate,
                par_level=item.par_level,
            ))

    return sorted(low, key=lambda x: x.days_remaining)
