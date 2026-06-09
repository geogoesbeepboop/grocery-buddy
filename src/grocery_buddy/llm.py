"""Shared Anthropic client + token/cost telemetry + caching helpers.

Three jobs, one module, because they're entangled:

1. **One process-wide client.** Every call-site used to build a fresh
   ``AsyncAnthropic`` per call, which threw away httpx's connection pool (and its
   kept-alive TLS connections) on every request. ``get_client()`` hands back a
   single shared client so pooling actually works.

2. **The only token/cost telemetry in the system.** None of the call-sites read
   ``response.usage`` — so cost and cache-hit rate were invisible, which made
   every other "efficiency" change unmeasurable. ``record_usage()`` is the single
   place that prices a response, logs tokens + cost + cache-hit rate, and feeds
   the Langfuse trace (``tracing.log_generation``, previously dead code).

3. **Caching helpers that respect the floor.** Haiku won't cache a prefix under
   4096 tokens, so naive ``cache_control`` silently no-ops on most calls. The
   helpers here are built for the one place it pays — the multi-turn
   onboarding/import loops, whose replayed transcript clears the floor on later
   turns — and no-op harmlessly everywhere else.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from grocery_buddy import tracing
from grocery_buddy.config import settings

logger = logging.getLogger(__name__)

_EPHEMERAL = {"type": "ephemeral"}


# ── Shared client (fixes per-call client construction) ────────────────────────

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    """The process-wide ``AsyncAnthropic``.

    Reused across every call-site so httpx keeps one connection pool alive for the
    life of the process. Constructing a client per call (the old pattern) dropped
    the pool — and paid a fresh TLS handshake — on every single request.
    """
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# ── Pricing + cost ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Price:
    """USD per 1M tokens."""

    input: float
    output: float


# Cache writes bill at 1.25x input (5-minute ephemeral TTL); cache reads at ~0.1x.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10

# Keyed by the bare model alias; matched by prefix so dated IDs
# (e.g. "claude-haiku-4-5-20251001") resolve too. Source: Anthropic pricing.
_PRICING: dict[str, _Price] = {
    "claude-haiku-4-5": _Price(input=1.00, output=5.00),
    "claude-sonnet-4-6": _Price(input=3.00, output=15.00),
    "claude-opus-4-8": _Price(input=5.00, output=25.00),
}


def _price_for(model: str) -> _Price | None:
    if model in _PRICING:
        return _PRICING[model]
    for key, price in _PRICING.items():
        if model.startswith(key):
            return price
    return None


def _field(usage, name: str) -> int:
    return int(getattr(usage, name, 0) or 0)


def cost_usd(model: str, usage) -> float:
    """Dollar cost of one response's token usage, cache tiers included.

    Returns 0.0 for an unknown model rather than guessing a price.
    """
    price = _price_for(model)
    if price is None or usage is None:
        return 0.0
    inp = _field(usage, "input_tokens")
    out = _field(usage, "output_tokens")
    cache_write = _field(usage, "cache_creation_input_tokens")
    cache_read = _field(usage, "cache_read_input_tokens")
    return (
        inp * price.input
        + cache_write * price.input * _CACHE_WRITE_MULT
        + cache_read * price.input * _CACHE_READ_MULT
        + out * price.output
    ) / 1_000_000


def record_usage(model: str, usage, *, label: str) -> float:
    """Price one response, log it, feed the Langfuse trace. Returns cost in USD.

    Call this at EVERY Anthropic call-site — it is the system's only token/cost
    telemetry. ``label`` names the call-site in the log line. Safe to call with
    ``usage=None`` (no-op); never raises, so telemetry can't break a real call.
    """
    if usage is None:
        return 0.0

    inp = _field(usage, "input_tokens")
    out = _field(usage, "output_tokens")
    cache_write = _field(usage, "cache_creation_input_tokens")
    cache_read = _field(usage, "cache_read_input_tokens")
    cost = cost_usd(model, usage)

    # Cache-hit rate = cached reads / all input tokens that went through the cache
    # tiers + full-price input. Zero across repeated same-prefix calls means a
    # silent invalidator (or a prefix below the cacheable floor).
    total_in = inp + cache_write + cache_read
    hit_rate = (cache_read / total_in) if total_in else 0.0

    logger.info(
        "llm %s model=%s in=%d cache_write=%d cache_read=%d out=%d "
        "cache_hit=%.0f%% cost=$%.5f",
        label, model, inp, cache_write, cache_read, out, hit_rate * 100, cost,
    )

    try:
        tracing.log_generation(
            model=model,
            usage={
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
            cost_usd=cost,
        )
    except Exception:  # telemetry must never break the actual request
        logger.debug("log_generation failed", exc_info=True)

    return cost


# ── Caching helpers ───────────────────────────────────────────────────────────


def cacheable_system(text: str) -> list[dict]:
    """Render a (stable) system prompt as one cacheable block.

    Caches tools+system together — they render before messages, so a breakpoint on
    the last system block covers both. Only worth it when the system prompt is
    byte-stable across calls; interpolating volatile data into it invalidates the
    cache every turn (the bug this codebase had in the review/briefing prompts).
    Silently no-ops below the model's cacheable floor (4096 tokens for Haiku).
    """
    return [{"type": "text", "text": text, "cache_control": _EPHEMERAL}]


def with_transcript_cache(messages: list[dict]) -> list[dict]:
    """Copy of ``messages`` with one ephemeral cache breakpoint on the last block.

    This is the multi-turn replay pattern: the whole prefix up to the breakpoint
    (tools + system + every prior turn) is cached, and the next turn reads it back
    instead of re-billing the full transcript. Non-mutating, so the persisted
    transcript never accumulates stale breakpoints.

    Below the cacheable floor this no-ops — which is exactly the early-turn
    behavior we want. It starts paying once the replayed transcript clears the
    floor, which is the only place caching helps these Haiku loops.
    """
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content = list(content)
        content[-1] = {**content[-1], "cache_control": _EPHEMERAL}
    else:
        # Empty or unexpected content shape — skip rather than risk a 400.
        return messages
    last["content"] = content
    out[-1] = last
    return out
