"""Unit tests for the shared LLM client / cost telemetry / caching helpers."""
from __future__ import annotations

import pytest

from grocery_buddy import llm


class _Usage:
    """Stand-in for an Anthropic ``response.usage`` object."""

    def __init__(
        self,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


# ── Pricing / cost ────────────────────────────────────────────────────────────


class TestCost:
    def test_haiku_plain_tokens(self):
        cost = llm.cost_usd("claude-haiku-4-5", _Usage(input_tokens=1000, output_tokens=500))
        assert cost == pytest.approx((1000 * 1.0 + 500 * 5.0) / 1_000_000)

    def test_dated_model_id_resolves_by_prefix(self):
        # settings.model_fast is the dated form "claude-haiku-4-5-20251001".
        cost = llm.cost_usd("claude-haiku-4-5-20251001", _Usage(input_tokens=1000))
        assert cost == pytest.approx(1000 * 1.0 / 1_000_000)

    def test_cache_tiers_priced_distinctly(self):
        # Cache writes bill at 1.25x input, reads at 0.10x input.
        u = _Usage(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=2000,
            cache_read_input_tokens=4000,
        )
        expected = (
            1000 * 1.0 + 2000 * 1.0 * 1.25 + 4000 * 1.0 * 0.10 + 500 * 5.0
        ) / 1_000_000
        assert llm.cost_usd("claude-haiku-4-5", u) == pytest.approx(expected)

    def test_sonnet_pricing(self):
        cost = llm.cost_usd("claude-sonnet-4-6", _Usage(input_tokens=1000, output_tokens=1000))
        assert cost == pytest.approx((1000 * 3.0 + 1000 * 15.0) / 1_000_000)

    def test_unknown_model_costs_zero(self):
        assert llm.cost_usd("some-other-model", _Usage(input_tokens=1000)) == 0.0

    def test_none_usage_costs_zero(self):
        assert llm.cost_usd("claude-haiku-4-5", None) == 0.0


class TestRecordUsage:
    def test_returns_cost(self):
        cost = llm.record_usage(
            "claude-haiku-4-5", _Usage(input_tokens=1000, output_tokens=500), label="t"
        )
        assert cost == pytest.approx((1000 * 1.0 + 500 * 5.0) / 1_000_000)

    def test_none_usage_is_noop(self):
        assert llm.record_usage("claude-haiku-4-5", None, label="t") == 0.0


# ── Caching helpers ─────────────────────────────────────────────────────────--


class TestCacheableSystem:
    def test_wraps_with_ephemeral_breakpoint(self):
        assert llm.cacheable_system("hello") == [
            {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
        ]


class TestTranscriptCache:
    def test_empty_unchanged(self):
        assert llm.with_transcript_cache([]) == []

    def test_string_content_gets_breakpoint_without_mutating_input(self):
        messages = [{"role": "user", "content": "hi"}]
        out = llm.with_transcript_cache(messages)
        # Persisted transcript is untouched — still a bare string.
        assert messages == [{"role": "user", "content": "hi"}]
        block = out[-1]["content"][-1]
        assert block["text"] == "hi"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_marks_only_the_last_block(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ]},
        ]
        out = llm.with_transcript_cache(messages)
        # Original list-content blocks are not mutated.
        assert "cache_control" not in messages[-1]["content"][-1]
        # Exactly one breakpoint, on the final block.
        breakpoints = sum(
            1
            for m in out
            for b in (m["content"] if isinstance(m["content"], list) else [])
            if isinstance(b, dict) and "cache_control" in b
        )
        assert breakpoints == 1
        assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in out[-1]["content"][0]


class TestGetClient:
    def test_is_a_singleton(self, monkeypatch):
        monkeypatch.setattr(llm.settings, "anthropic_api_key", "test-key")
        monkeypatch.setattr(llm, "_client", None)
        assert llm.get_client() is llm.get_client()


# ── compose_briefing gating (Fix 3: don't pay Haiku for cosmetic work) ────────


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.usage = _Usage(input_tokens=10, output_tokens=10)


def _fake_client(text):
    class _Messages:
        async def create(self, **kwargs):
            return _FakeResp(text)

    class _Client:
        messages = _Messages()

    return _Client()


class TestComposeBriefingGate:
    async def test_no_reason_skips_the_llm(self, monkeypatch):
        from grocery_buddy.agents import assistant

        called = False

        def _boom():
            nonlocal called
            called = True
            raise RuntimeError("LLM must not be called on the no-reason path")

        monkeypatch.setattr(assistant.llm, "get_client", _boom)
        items = [{"product": "eggs", "qty": 1, "unit": "", "price_usd": 3.5, "notes": ""}]
        text = await assistant.compose_briefing(items, 3.5, reason=None)

        assert called is False  # gate returned before any client construction
        assert "eggs" in text and "3.50" in text

    async def test_reason_invokes_the_llm(self, monkeypatch):
        from grocery_buddy.agents import assistant

        monkeypatch.setattr(
            assistant.llm, "get_client", lambda: _fake_client("MODEL_PROSE for $9.00 total")
        )
        items = [{"product": "eggs", "qty": 1, "unit": "", "price_usd": 9.0, "notes": ""}]
        text = await assistant.compose_briefing(
            items, 9.0, reason="Added 2 more to clear the free-shipping minimum."
        )

        # The model's prose (not the deterministic render) was returned.
        assert "MODEL_PROSE" in text

    async def test_reason_falls_back_when_total_drifts(self, monkeypatch):
        from grocery_buddy.agents import assistant

        # Model output drops the exact total → deterministic render must win.
        monkeypatch.setattr(
            assistant.llm, "get_client", lambda: _fake_client("no total here")
        )
        items = [{"product": "eggs", "qty": 1, "unit": "", "price_usd": 9.0, "notes": ""}]
        text = await assistant.compose_briefing(items, 9.0, reason="free shipping")

        assert "MODEL" not in text
        assert "9.00" in text and "eggs" in text
