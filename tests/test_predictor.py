"""Unit tests for the rule-based predictor."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from grocery_buddy.predictor import (
    ConsumptionEvent,
    ConsumptionProfile,
    InventoryItem,
    days_left,
    effective_daily_rate,
    is_low,
    predict_low_items,
)


def _event(delta: float, days_ago: int, source: str = "user_update") -> ConsumptionEvent:
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    return ConsumptionEvent(delta=delta, ts=ts, source=source)


class TestEffectiveDailyRate:
    def test_no_events_returns_declared_rate(self):
        profile = ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count")
        rate = effective_daily_rate(profile, [])
        assert rate == pytest.approx(1.0)

    def test_household_factor_applied(self):
        profile = ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count", household_factor=2.0)
        rate = effective_daily_rate(profile, [])
        assert rate == pytest.approx(2.0)

    def test_observation_weight_grows_with_events(self):
        profile = ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count")
        # 14 events = max weight (0.8) toward observed
        events = [_event(-2.0, i) for i in range(14)]  # 2 consumed each day for 14 days → observed 28/30 ≈ 0.93/day
        rate = effective_daily_rate(profile, events)
        # With 14 events: weight=0.8; rate = 0.2*1.0 + 0.8*observed
        observed = 28 / 30  # total consumed / lookback_days
        expected = 0.2 * 1.0 + 0.8 * observed
        assert rate == pytest.approx(expected, rel=0.01)

    def test_only_negative_deltas_count_as_consumption(self):
        profile = ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count")
        events = [
            _event(-5.0, 5),   # consumed
            _event(+12.0, 10), # restocked — should NOT count
        ]
        rate = effective_daily_rate(profile, events)
        # Only 1 consumption event → weight = 1/14 ≈ 0.071
        weight = min(1 / 14.0, 0.8)
        observed = 5.0 / 30
        expected = (1 - weight) * 1.0 + weight * observed
        assert rate == pytest.approx(expected, rel=0.01)

    def test_inferred_and_correction_events_ignored(self):
        """Only genuine user_update consumption informs the rate — not the model's
        own arithmetic depletion ('inferred') or one-off resets ('correction')."""
        profile = ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count")
        events = [
            _event(-3.0, 2, source="inferred"),    # our own depletion — must not count
            _event(-9.0, 4, source="correction"),  # "family used them all" — must not count
        ]
        rate = effective_daily_rate(profile, events)
        assert rate == pytest.approx(1.0)  # falls back to declared, untouched

    def test_user_update_still_counts_alongside_ignored_sources(self):
        profile = ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count")
        events = [
            _event(-5.0, 5, source="user_update"),  # real consumption — counts
            _event(-50.0, 3, source="inferred"),    # ignored
            _event(-50.0, 3, source="correction"),  # ignored
        ]
        rate = effective_daily_rate(profile, events)
        weight = min(1 / 14.0, 0.8)
        observed = 5.0 / 30  # only the user_update delta
        expected = (1 - weight) * 1.0 + weight * observed
        assert rate == pytest.approx(expected, rel=0.01)


class TestDaysLeft:
    def test_normal(self):
        item = InventoryItem(product="Eggs", qty=7.0, unit="count", par_level=1.0)
        assert days_left(item, rate=1.0) == pytest.approx(7.0)

    def test_zero_rate_is_inf(self):
        item = InventoryItem(product="Eggs", qty=7.0, unit="count", par_level=1.0)
        assert days_left(item, rate=0.0) == float("inf")


class TestIsLow:
    def test_low_when_days_remaining_under_threshold(self):
        item = InventoryItem(product="Eggs", qty=2.0, unit="count", par_level=1.0)
        # rate=1/day → 2 days left; threshold = lead_time(2) + buffer(1) = 3 → is_low
        assert is_low(item, rate=1.0, lead_time_days=2.0, buffer_days=1.0)

    def test_not_low_when_plenty(self):
        item = InventoryItem(product="Eggs", qty=20.0, unit="count", par_level=1.0)
        assert not is_low(item, rate=1.0, lead_time_days=2.0, buffer_days=1.0)


class TestPredictLowItems:
    def test_returns_only_low_items(self, sample_inventory, sample_profiles):
        # Eggs: 3 qty, rate=1/day → 3 days left, threshold=3 → is_low
        # Milk: 0.25 gal, rate=0.14/day → ~1.8 days → is_low
        # Oats: 2 lbs, rate=0.1/day → 20 days → NOT low
        # Coffee: 0.1 lbs, rate=0.05/day → 2 days → is_low
        results = predict_low_items(sample_inventory, sample_profiles, {})
        product_names = [r.product for r in results]
        assert "Oats" not in product_names
        assert "Eggs" in product_names
        assert "Whole milk" in product_names

    def test_sorted_by_urgency(self, sample_inventory, sample_profiles):
        results = predict_low_items(sample_inventory, sample_profiles, {})
        days = [r.days_remaining for r in results]
        assert days == sorted(days)

    def test_no_profile_flags_at_par_level(self):
        inventory = [InventoryItem(product="Butter", qty=0.5, unit="lbs", par_level=1.0)]
        results = predict_low_items(inventory, profiles=[], events_by_product={})
        assert len(results) == 1
        assert results[0].product == "Butter"

    def test_no_profile_above_par_level_not_flagged(self):
        inventory = [InventoryItem(product="Butter", qty=2.0, unit="lbs", par_level=1.0)]
        results = predict_low_items(inventory, profiles=[], events_by_product={})
        assert len(results) == 0
