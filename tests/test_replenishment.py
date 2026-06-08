"""Unit tests for in-transit replenishments.

Covers the pure pieces that hold the feature together:
  • eta_for — the shipping-time arithmetic
  • incoming-aware prediction — a confirmed, on-the-way order isn't re-suggested
  • format_in_transit — the /status "on the way" render
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from grocery_buddy.predictor import (
    ConsumptionProfile,
    InventoryItem,
    classify_stock_levels,
    predict_low_items,
)
from grocery_buddy.replenishment import eta_for
from grocery_buddy.stock import format_in_transit


class TestEtaFor:
    def test_adds_lead_time(self):
        ordered = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        assert eta_for(ordered, 2.0) == ordered + timedelta(days=2)

    def test_fractional_lead_time(self):
        ordered = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)
        assert eta_for(ordered, 1.5) == ordered + timedelta(days=1, hours=12)

    def test_negative_lead_time_clamped_to_now(self):
        ordered = datetime(2026, 6, 5, tzinfo=timezone.utc)
        assert eta_for(ordered, -3.0) == ordered


class TestIncomingAwarePrediction:
    """The whole point: don't suggest eggs tomorrow if eggs are already on the way."""

    def test_low_item_suppressed_when_enough_incoming(self):
        # Eggs: 3 on hand, 1/day → 3 days left → low (threshold 3). A dozen on the way
        # pushes effective stock to 15 → ~15 days → no longer low.
        inv = [InventoryItem(product="eggs", qty=3.0, unit="count", par_level=12.0)]
        prof = [ConsumptionProfile(product="eggs", declared_rate=1.0, unit="count")]
        low = predict_low_items(inv, prof, {}, incoming_by_product={"eggs": 12.0})
        assert [r.product for r in low] == []

    def test_low_item_still_flagged_when_incoming_insufficient(self):
        # Only 1 extra on the way → 4 total → still under the 3-day threshold soon enough.
        inv = [InventoryItem(product="eggs", qty=1.0, unit="count", par_level=12.0)]
        prof = [ConsumptionProfile(product="eggs", declared_rate=1.0, unit="count")]
        low = predict_low_items(inv, prof, {}, incoming_by_product={"eggs": 1.0})
        assert [r.product for r in low] == ["eggs"]
        # Real on-hand is preserved; incoming is reported separately.
        assert low[0].qty == 1.0
        assert low[0].incoming == 1.0

    def test_no_incoming_behaves_as_before(self):
        inv = [InventoryItem(product="eggs", qty=2.0, unit="count", par_level=12.0)]
        prof = [ConsumptionProfile(product="eggs", declared_rate=1.0, unit="count")]
        assert [r.product for r in predict_low_items(inv, prof, {})] == ["eggs"]

    def test_no_profile_par_check_uses_incoming(self):
        # No declared rate → par-level check. 0.5 on hand vs par 1.0 would be low, but a
        # unit on the way lifts effective qty to 1.5 → above par → not low.
        inv = [InventoryItem(product="butter", qty=0.5, unit="lb", par_level=1.0)]
        low = predict_low_items(inv, [], {}, incoming_by_product={"butter": 1.0})
        assert low == []

    def test_classify_bucket_reflects_incoming(self):
        inv = [InventoryItem(product="eggs", qty=3.0, unit="count", par_level=12.0)]
        prof = [ConsumptionProfile(product="eggs", declared_rate=1.0, unit="count")]
        # Without incoming → LOW. With a dozen incoming → no longer LOW.
        without = classify_stock_levels(inv, prof, {})
        assert without[0].bucket == "low"
        with_incoming = classify_stock_levels(inv, prof, {}, incoming_by_product={"eggs": 12.0})
        assert with_incoming[0].bucket != "low"
        assert with_incoming[0].incoming == 12.0


class TestFormatInTransit:
    def test_empty_returns_blank(self):
        assert format_in_transit([]) == ""

    def test_renders_items_with_eta(self):
        eta = datetime.now(timezone.utc) + timedelta(days=1)
        out = format_in_transit([{"product": "eggs", "qty": 12, "unit": "count", "eta": eta}])
        assert "On the way" in out
        assert "eggs" in out
        assert "tomorrow" in out

    def test_quantity_and_unit_shown(self):
        eta = datetime.now(timezone.utc) + timedelta(days=3)
        out = format_in_transit([{"product": "milk", "qty": 2, "unit": "gallon", "eta": eta}])
        assert "milk" in out
        assert "2 gallon" in out
        assert "in 3 days" in out
