"""Unit tests for the free-shipping cart assembler."""
from __future__ import annotations

from grocery_buddy.predictor import LARGE, LOW, MEDIUM, StockLevel
from grocery_buddy.runlist import (
    FILLER,
    MUST_BUY,
    assemble_for_free_shipping,
    split_run_candidates,
)


def _level(product: str, bucket: str, days: float) -> StockLevel:
    return StockLevel(
        product=product,
        qty=1.0,
        unit="count",
        days_remaining=days,
        effective_rate=1.0,
        par_level=1.0,
        bucket=bucket,
    )


def _priced(product: str, tier: str, price: float, days=None, qty: float = 1.0) -> dict:
    return {
        "product": product,
        "tier": tier,
        "price_usd": price,
        "qty": qty,
        "days_remaining": days,
    }


class TestSplitRunCandidates:
    def test_partitions_low_and_medium(self):
        levels = [
            _level("eggs", LOW, 1.0),
            _level("milk", LOW, 2.0),
            _level("coffee", MEDIUM, 8.0),
            _level("rice", LARGE, 40.0),
        ]
        must, fillers = split_run_candidates(levels, max_fillers=6)
        assert {m.product for m in must} == {"eggs", "milk"}
        assert [f.product for f in fillers] == ["coffee"]  # LARGE excluded

    def test_fillers_sorted_soonest_first(self):
        levels = [
            _level("a", MEDIUM, 12.0),
            _level("b", MEDIUM, 4.0),
            _level("c", MEDIUM, 9.0),
        ]
        _, fillers = split_run_candidates(levels, max_fillers=6)
        assert [f.product for f in fillers] == ["b", "c", "a"]

    def test_fillers_capped(self):
        levels = [_level(f"m{i}", MEDIUM, float(i)) for i in range(10)]
        _, fillers = split_run_candidates(levels, max_fillers=3)
        assert len(fillers) == 3
        assert [f.product for f in fillers] == ["m0", "m1", "m2"]


class TestAssembleForFreeShipping:
    def test_must_buys_already_clear_threshold_adds_nothing(self):
        priced = [
            _priced("eggs", MUST_BUY, 15.0),
            _priced("milk", MUST_BUY, 12.0),
            _priced("coffee", FILLER, 9.0, days=8),
        ]
        final, reason = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=6)
        assert {p["product"] for p in final} == {"eggs", "milk"}
        assert reason is None  # no padding → default briefing header

    def test_pads_with_soonest_fillers_until_threshold(self):
        priced = [
            _priced("eggs", MUST_BUY, 8.0),
            _priced("late", FILLER, 10.0, days=12),
            _priced("soon", FILLER, 10.0, days=3),
        ]
        final, reason = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=6)
        names = [p["product"] for p in final]
        # eggs ($8) + soonest filler ($10) = $18 < 25 → add next → $28 ≥ 25, stop.
        assert names == ["eggs", "soon", "late"]
        assert reason is not None and "free next-day shipping" in reason

    def test_stops_as_soon_as_threshold_crossed(self):
        priced = [
            _priced("eggs", MUST_BUY, 20.0),
            _priced("f1", FILLER, 6.0, days=3),
            _priced("f2", FILLER, 6.0, days=4),
        ]
        final, _ = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=6)
        # $20 + $6 = $26 ≥ 25 → second filler not needed.
        assert [p["product"] for p in final] == ["eggs", "f1"]

    def test_respects_max_fillers_even_if_still_under(self):
        priced = [_priced("eggs", MUST_BUY, 2.0)] + [
            _priced(f"f{i}", FILLER, 1.0, days=float(i)) for i in range(6)
        ]
        final, reason = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=2)
        assert len(final) == 1 + 2  # must-buy + only 2 fillers
        # Still under threshold → reason warns about the delivery fee.
        assert reason is not None and "delivery fee" in reason

    def test_no_must_buy_returns_empty(self):
        priced = [_priced("coffee", FILLER, 9.0, days=8)]
        final, reason = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=6)
        assert final == []
        assert reason is None

    def test_none_days_sorts_last(self):
        priced = [
            _priced("eggs", MUST_BUY, 5.0),
            _priced("norate", FILLER, 10.0, days=None),
            _priced("soon", FILLER, 10.0, days=2),
        ]
        final, _ = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=6)
        # soonest (days=2) added before the unknown-rate one.
        assert [p["product"] for p in final] == ["eggs", "soon", "norate"]

    def test_qty_multiplies_into_line_total(self):
        priced = [_priced("eggs", MUST_BUY, 10.0, qty=3.0)]  # $30 line
        final, reason = assemble_for_free_shipping(priced, threshold=25.0, max_fillers=6)
        assert [p["product"] for p in final] == ["eggs"]
        assert reason is None  # one line already clears $25
