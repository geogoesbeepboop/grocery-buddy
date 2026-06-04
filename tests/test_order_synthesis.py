"""Unit tests for order-history aggregation (pure, no LLM, no browser).

Covers the pre-synthesis aggregation that collapses raw scraped orders into one
compact, cadence-rich record per product — the step that fixed the max_tokens
truncation (too many raw rows → unparseable proposal → empty pantry).
"""
from __future__ import annotations

from datetime import date

from grocery_buddy.agents.order_history import _aggregate_orders, _parse_date


class TestParseDate:
    def test_scraped_long_form(self):
        assert _parse_date("June 3, 2026") == date(2026, 6, 3)

    def test_abbreviated_and_iso_and_slash(self):
        assert _parse_date("Jun 3, 2026") == date(2026, 6, 3)
        assert _parse_date("2026-06-03") == date(2026, 6, 3)
        assert _parse_date("06/03/2026") == date(2026, 6, 3)

    def test_garbage_and_empty(self):
        assert _parse_date("not a date") is None
        assert _parse_date(None) is None
        assert _parse_date("") is None


class TestAggregateOrders:
    def _orders(self) -> list[dict]:
        return [
            {"order_date": "March 1, 2026", "items": [{"asin": "MILK", "qty": 1, "title": "Horizon 2% Milk"}]},
            {"order_date": "May 20, 2026", "items": [{"asin": "MILK", "qty": 1, "title": "Horizon 2% Milk"}]},
            {"order_date": "June 1, 2026", "items": [{"asin": "MILK", "qty": 2, "title": "Horizon 2% Milk"}]},
            {"order_date": "June 3, 2026", "items": [{"asin": "LENT", "qty": 2, "title": "Goya Dry Lentils, 16 oz"}]},
        ]

    def test_merges_repeat_orders_by_asin(self):
        recs = _aggregate_orders(self._orders(), date(2026, 6, 4))
        milk = next(r for r in recs if r["title"].startswith("Horizon"))
        assert milk["times_ordered"] == 3
        assert milk["total_units"] == 4  # 1 + 1 + 2
        assert milk["first_ordered"] == "2026-03-01"
        assert milk["last_ordered"] == "2026-06-01"
        assert milk["days_since_last"] == 3
        assert milk["span_days"] == 92

    def test_one_record_per_distinct_product(self):
        recs = _aggregate_orders(self._orders(), date(2026, 6, 4))
        assert len(recs) == 2  # milk + lentils, regardless of 4 raw orders

    def test_sorted_by_frequency_then_units(self):
        recs = _aggregate_orders(self._orders(), date(2026, 6, 4))
        assert recs[0]["title"].startswith("Horizon")  # 3 orders > 1 order

    def test_falls_back_to_title_when_no_asin(self):
        orders = [
            {"order_date": "June 1, 2026", "items": [{"qty": 1, "title": "Generic Oat Milk Carton"}]},
            {"order_date": "June 3, 2026", "items": [{"qty": 1, "title": "Generic Oat Milk Carton"}]},
        ]
        recs = _aggregate_orders(orders, date(2026, 6, 4))
        assert len(recs) == 1
        assert recs[0]["times_ordered"] == 2

    def test_undated_orders_still_counted(self):
        orders = [{"order_date": None, "items": [{"asin": "X", "qty": 1, "title": "Mystery Snack Box"}]}]
        recs = _aggregate_orders(orders, date(2026, 6, 4))
        assert recs[0]["times_ordered"] == 1
        # No dates → no cadence fields, but the record still exists for the model.
        assert "last_ordered" not in recs[0]

    def test_same_product_twice_in_one_order_counts_once(self):
        # A single order with the product split across two line items must count as
        # ONE order (times_ordered) while still summing units.
        orders = [{
            "order_date": "June 1, 2026",
            "items": [
                {"asin": "P", "qty": 1, "title": "Sparkling Water 12-pack"},
                {"asin": "P", "qty": 2, "title": "Sparkling Water 12-pack"},
            ],
        }]
        recs = _aggregate_orders(orders, date(2026, 6, 4))
        assert len(recs) == 1
        assert recs[0]["times_ordered"] == 1
        assert recs[0]["total_units"] == 3
        assert recs[0]["order_dates"] == ["2026-06-01"]  # date not double-counted

    def test_caps_order_dates_list(self):
        many = [
            {"order_date": f"2026-01-{d:02d}", "items": [{"asin": "C", "qty": 1, "title": "Coffee Beans 12oz"}]}
            for d in range(1, 13)  # 12 orders
        ]
        recs = _aggregate_orders(many, date(2026, 6, 4))
        assert recs[0]["times_ordered"] == 12
        assert len(recs[0]["order_dates"]) == 8  # capped to most-recent 8
