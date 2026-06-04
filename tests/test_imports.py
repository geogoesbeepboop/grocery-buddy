"""Unit tests for import-proposal edit application (pure, no I/O)."""
from __future__ import annotations

from grocery_buddy.tools.imports import apply_edits


def _items() -> list[dict]:
    return [
        {"product": "large brown eggs", "unit": "count", "estimated_qty": 6, "daily_rate": 0.86},
        {"product": "oat milk", "unit": "carton", "estimated_qty": 1, "daily_rate": 0.14},
        {"product": "donuts", "unit": "box", "estimated_qty": 1, "daily_rate": 0.2},
        {"product": "potato chips", "unit": "bag", "estimated_qty": 2, "daily_rate": 0.3},
    ]


class TestApplyEdits:
    def test_remove_matches_by_canonical_name(self):
        result = apply_edits(_items(), remove=["Donuts", "POTATO CHIPS"])
        names = {it["product"] for it in result}
        assert "donuts" not in names
        assert "potato chips" not in names
        assert "large brown eggs" in names
        assert len(result) == 2

    def test_remove_unknown_is_noop(self):
        result = apply_edits(_items(), remove=["caviar"])
        assert len(result) == 4

    def test_update_overwrites_given_fields_only(self):
        result = apply_edits(
            _items(),
            update=[{"product": "oat milk", "daily_rate": 0.5, "estimated_qty": None}],
        )
        oat = next(it for it in result if it["product"] == "oat milk")
        assert oat["daily_rate"] == 0.5
        assert oat["estimated_qty"] == 1  # None is ignored, original kept
        assert oat["unit"] == "carton"

    def test_update_unknown_product_is_noop(self):
        result = apply_edits(_items(), update=[{"product": "ghost", "daily_rate": 9}])
        assert all(it["product"] != "ghost" for it in result)

    def test_add_new_item(self):
        result = apply_edits(
            _items(),
            add=[{"product": "greek yogurt", "unit": "tub", "daily_rate": 0.25}],
        )
        names = {it["product"] for it in result}
        assert "greek yogurt" in names
        assert len(result) == 5

    def test_add_existing_merges(self):
        result = apply_edits(
            _items(),
            add=[{"product": "oat milk", "preferred_brand": "Oatly"}],
        )
        oat = next(it for it in result if it["product"] == "oat milk")
        assert oat["preferred_brand"] == "Oatly"
        assert oat["daily_rate"] == 0.14  # original field preserved
        assert len(result) == 4

    def test_does_not_mutate_input(self):
        original = _items()
        apply_edits(original, remove=["donuts"])
        assert len(original) == 4

    def test_combined_remove_update_add(self):
        result = apply_edits(
            _items(),
            remove=["donuts", "potato chips"],
            update=[{"product": "large brown eggs", "estimated_qty": 12}],
            add=[{"product": "bananas", "unit": "count", "daily_rate": 1.0}],
        )
        by_name = {it["product"]: it for it in result}
        assert "donuts" not in by_name
        assert by_name["large brown eggs"]["estimated_qty"] == 12
        assert "bananas" in by_name
        assert len(result) == 3
