"""Unit tests for Telegram message chunking (the 4096-char overflow guard)."""
from __future__ import annotations

from grocery_buddy.notifications import _split_for_telegram


class TestSplitForTelegram:
    def test_short_message_is_one_chunk(self):
        assert _split_for_telegram("hello") == ["hello"]

    def test_splits_long_message_under_limit(self):
        # 200 lines of 50 chars = ~10k chars, well over the limit.
        text = "\n".join("x" * 50 for _ in range(200))
        chunks = _split_for_telegram(text, limit=1000)
        assert len(chunks) > 1
        assert all(len(c) <= 1000 for c in chunks)

    def test_splits_on_line_boundaries(self):
        text = "\n".join(f"line {i}" for i in range(100))
        chunks = _split_for_telegram(text, limit=200)
        # No line is broken across chunks: every line appears intact somewhere.
        rejoined_lines = "\n".join(chunks).split("\n")
        assert rejoined_lines == text.split("\n")

    def test_preserves_order_and_content(self):
        text = "\n".join(f"item-{i}" for i in range(50))
        chunks = _split_for_telegram(text, limit=120)
        # Reassembling the chunks recovers every original line in order.
        assert "\n".join(chunks).split("\n") == text.split("\n")

    def test_hard_splits_a_single_oversized_line(self):
        line = "z" * 5000  # one line, no newlines, over the limit
        chunks = _split_for_telegram(line, limit=1000)
        assert len(chunks) == 5
        assert all(len(c) <= 1000 for c in chunks)
        assert "".join(chunks) == line

    def test_default_limit_keeps_chunks_under_telegram_cap(self):
        text = "\n".join("word " * 40 for _ in range(300))
        chunks = _split_for_telegram(text)
        assert len(chunks) > 1
        assert all(len(c) <= 4096 for c in chunks)
