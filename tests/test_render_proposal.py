"""The import proposal must summarize, not dump — a 90-item list overflowed
Telegram's 4096-char limit (400 "message is too long") and never reached the user.
"""
from __future__ import annotations

from grocery_buddy.agents.order_history import (
    _low_preview,
    render_full_proposal,
    render_proposal,
)
from grocery_buddy.notifications import TELEGRAM_MAX_CHARS


def _item(product, category="other", qty=10.0, rate=0.1, brand=None):
    return {
        "product": product,
        "unit": "count",
        "estimated_qty": qty,
        "par_level": 5.0,
        "daily_rate": rate,
        "category": category,
        "preferred_brand": brand,
    }


def _big_proposal(n=120):
    cats = ["dairy & eggs", "snacks", "beverages", "cleaning", "pantry staples"]
    return [_item(f"product {i}", category=cats[i % len(cats)]) for i in range(n)]


class TestRenderProposal:
    def test_empty(self):
        assert "couldn't find" in render_proposal([])

    def test_summary_stays_under_telegram_limit_for_huge_history(self):
        # The exact failure from the logs: 90 (here 120) items must not overflow.
        msg = render_proposal(_big_proposal(120))
        assert len(msg) < TELEGRAM_MAX_CHARS

    def test_summary_reports_item_count_and_categories(self):
        msg = render_proposal(_big_proposal(120))
        assert "120 items" in msg
        # Category spread is shown with counts rather than every item.
        assert "Snacks" in msg
        assert "(24)" in msg  # 120 / 5 categories

    def test_low_preview_surfaces_depleted_items(self):
        items = [
            _item("fresh oats", qty=100.0, rate=0.1),   # ~1000 days → not low
            _item("almost-gone milk", qty=0.2, rate=1.0),  # 0.2 days → low
        ]
        preview = _low_preview(items)
        assert "almost-gone milk" in preview[0]
        assert all("fresh oats" not in p for p in preview)

    def test_low_preview_includes_brand_when_known(self):
        items = [_item("milk", qty=0.1, rate=1.0, brand="Horizon")]
        assert "Horizon" in _low_preview(items)[0]


class TestRenderFullProposal:
    def test_lists_every_item(self):
        items = [_item("eggs", "dairy & eggs"), _item("chips", "snacks")]
        full = render_full_proposal(items)
        assert "eggs" in full
        assert "chips" in full

    def test_escapes_ampersand_in_category(self):
        # Telegram HTML mode rejects a bare "&" — categories like "dairy & eggs"
        # must be escaped or the send fails.
        full = render_full_proposal([_item("eggs", "dairy & eggs")])
        assert "&amp;" in full
        assert "dairy & eggs" not in full  # the raw, unescaped form must not appear
