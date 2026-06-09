"""Shared Anthropic client + token/cost telemetry + caching helpers + run attribution.

Four entangled jobs, one module:

1. **One process-wide client.** Building a fresh ``AsyncAnthropic`` per call threw
   away httpx's connection pool (and a TLS handshake) every request. ``get_client()``
   hands back a single shared client.

2. **Token/cost telemetry.** ``record_usage()`` is the one place that prices a
   response, logs tokens + cost + cache-hit rate, feeds the Langfuse trace, and
   (best-effort, fire-and-forget) writes a row to the ``llm_usage`` ledger — the
   authoritative per-run cost record that powers the cost alert.

3. **Caching helpers** (``cacheable_system`` / ``with_transcript_cache``) that respect
   Haiku's 4096-token cacheable floor — they pay off on the multi-turn onboarding/
   import loops and no-op harmlessly everywhere else.

4. **Run attribution.** ``run_scope(workflow_id, user_id)`` tags every LLM call made
   inside it — even deep ones like ``compose_briefing`` — with the Temporal run, so
   ``evals.sum_run_cost(workflow_id)`` can total a run's spend.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid as _uuid
from dataclasses import dataclass
from typing import Any

import anthropic

from grocery_buddy import tracing
from grocery_buddy.config import settings

logger = logging.getLogger(__name__)

_EPHEMERAL = {"type": "ephemeral"}


# ── Shared client (fixes per-call client construction) ────────────────────────

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    """The process-wide ``AsyncAnthropic`` (reused so httpx keeps one connection pool)."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# ── Run attribution (workflow_id + user, for the cost ledger) ─────────────────

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("gb_run_id", default=None)
_run_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("gb_run_user", default=None)


class run_scope:
    """Tag every LLM call made inside this scope with a run id (+ optional user).

    Usage inside a Temporal activity::

        with run_scope(activity.info().workflow_id, user_id):
            ...  # brand pick / briefing / synthesis costs attribute to this run
    """

    def __init__(self, run_id: str | None, user_id: str | None = None) -> None:
        self._run_id = run_id
        self._user_id = user_id
        self._t_run: Any = None
        self._t_user: Any = None

    def __enter__(self) -> run_scope:
        self._t_run = _run_id.set(self._run_id)
        self._t_user = _run_user.set(self._user_id)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._t_run is not None:
            _run_id.reset(self._t_run)
        if self._t_user is not None:
            _run_user.reset(self._t_user)


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


# ── Usage recording (logs + Langfuse + best-effort cost ledger) ───────────────

# Strong refs to fire-and-forget ledger tasks so they aren't GC'd mid-flight.
_ledger_tasks: set[asyncio.Task] = set()


def _uuid_or_none(uid: str | None) -> Any:
    if not uid:
        return None
    try:
        return _uuid.UUID(str(uid))
    except (ValueError, TypeError):
        return None


async def _persist_ledger(
    model: str, fields: dict[str, int], cost: float, label: str, uid, run_id: str | None
) -> None:
    """Write one ``llm_usage`` row. Best-effort — never raises into the caller."""
    try:
        from grocery_buddy.db import get_pool

        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO llm_usage
                (user_id, workflow_id, label, model,
                 input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            _uuid_or_none(uid),
            run_id,
            label,
            model,
            fields["input_tokens"],
            fields["output_tokens"],
            fields["cache_read_input_tokens"],
            fields["cache_creation_input_tokens"],
            round(cost, 6),
        )
    except Exception as exc:
        logger.debug("llm_usage ledger insert skipped (%s)", exc)


def _schedule_ledger(model: str, fields: dict[str, int], cost: float, label: str) -> None:
    """Fire-and-forget the ledger write on the running loop, tagged with run_scope.

    Skipped when no DB is configured or there's no running event loop (e.g. a sync
    unit test), so ``record_usage`` stays a pure synchronous cost function there.
    """
    if not settings.database_url:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _persist_ledger(model, fields, cost, label, _run_user.get(), _run_id.get())
    )
    _ledger_tasks.add(task)
    task.add_done_callback(_ledger_tasks.discard)


def record_usage(model: str, usage, *, label: str, user_id: str | None = None) -> float:
    """Price one response, log it, feed Langfuse + the cost ledger. Returns cost (USD).

    Call this at EVERY Anthropic call-site — it is the system's token/cost telemetry.
    Synchronous and safe with ``usage=None`` (no-op); never raises, so telemetry can't
    break a real request. The ledger write is fire-and-forget (best-effort), attributed
    to the active ``run_scope`` when there is one.
    """
    if usage is None:
        return 0.0

    inp = _field(usage, "input_tokens")
    out = _field(usage, "output_tokens")
    cache_write = _field(usage, "cache_creation_input_tokens")
    cache_read = _field(usage, "cache_read_input_tokens")
    cost = cost_usd(model, usage)

    total_in = inp + cache_write + cache_read
    hit_rate = (cache_read / total_in) if total_in else 0.0
    logger.info(
        "llm %s model=%s in=%d cache_write=%d cache_read=%d out=%d cache_hit=%.0f%% cost=$%.5f",
        label, model, inp, cache_write, cache_read, out, hit_rate * 100, cost,
    )

    fields = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }
    try:
        tracing.record_generation(
            model=model,
            usage=fields,
            cost_usd=cost,
            label=label,
            user_id=_run_user.get(),
            run_id=_run_id.get(),
        )
    except Exception:  # telemetry must never break the actual request
        logger.debug("record_generation failed", exc_info=True)

    _schedule_ledger(model, fields, cost, label)
    return cost


async def create_message(*, model: str, label: str, user_id: str | None = None, **kwargs: Any):
    """``client.messages.create`` on the shared client, recording usage/cost.

    Convenience wrapper used by the eval harness (``evals/``); the in-app call-sites
    use the explicit ``get_client()`` + ``record_usage()`` pattern so they can apply
    the caching helpers.
    """
    resp = await get_client().messages.create(model=model, **kwargs)
    try:
        record_usage(model, getattr(resp, "usage", None), label=label, user_id=user_id)
    except Exception:
        logger.debug("record_usage failed", exc_info=True)
    return resp


# ── Caching helpers ───────────────────────────────────────────────────────────


def cacheable_system(text: str) -> list[dict]:
    """Render a (stable) system prompt as one cacheable block.

    Caches tools+system together (they render before messages, so a breakpoint on the
    last system block covers both). Only worth it when the system prompt is byte-stable
    across calls; interpolating volatile data into it invalidates the cache every turn.
    Silently no-ops below the model's cacheable floor (4096 tokens for Haiku).
    """
    return [{"type": "text", "text": text, "cache_control": _EPHEMERAL}]


def with_transcript_cache(messages: list[dict]) -> list[dict]:
    """Copy of ``messages`` with one ephemeral cache breakpoint on the last block.

    The multi-turn replay pattern: the whole prefix up to the breakpoint (tools +
    system + every prior turn) is cached and read back instead of re-billed.
    Non-mutating, so the persisted transcript never accumulates stale breakpoints.
    No-ops below the cacheable floor — exactly the early-turn behavior we want.
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
