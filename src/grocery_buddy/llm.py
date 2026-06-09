"""Shared Anthropic client + a thin wrapper that records token usage and cost.

Every LLM call in the app should go through ``create_message()`` (single-shot) or
``get_client()`` + ``record_usage()`` (streaming) so we get:

  1. ONE pooled ``AsyncAnthropic`` client (httpx connection reuse), and
  2. a per-call row in the ``llm_usage`` ledger — the authoritative cost record
     that powers the per-run cost alert and cost observability.

Run attribution: wrap an activity body in ``with run_scope(activity.info().workflow_id,
user_id):`` and every LLM call inside (even deep ones like ``compose_briefing``)
is tagged with that workflow id, so ``evals.sum_run_cost(workflow_id)`` can total a
run's spend. Cost recording is best-effort — a DB hiccup never breaks a model call.
"""
from __future__ import annotations

import contextvars
import logging
import uuid as _uuid
from typing import Any

import anthropic

from grocery_buddy import tracing
from grocery_buddy.config import settings
from grocery_buddy.pricing import cost_of

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None

# Set by run_scope() so LLM calls can be attributed to a Temporal run + user.
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("gb_run_id", default=None)
_run_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("gb_run_user", default=None)


def get_client() -> anthropic.AsyncAnthropic:
    """The process-wide shared async Anthropic client (lazily constructed)."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


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


def _usage_dict(usage: Any) -> dict[str, int]:
    def g(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    return {
        "input_tokens": g("input_tokens"),
        "output_tokens": g("output_tokens"),
        "cache_read_input_tokens": g("cache_read_input_tokens"),
        "cache_creation_input_tokens": g("cache_creation_input_tokens"),
    }


def _uuid_or_none(uid: str | None) -> Any:
    if not uid:
        return None
    try:
        return _uuid.UUID(str(uid))
    except (ValueError, TypeError):
        return None


async def record_usage(
    model: str, usage: Any, label: str, user_id: str | None = None
) -> float:
    """Persist one generation's tokens + cost to the ledger (and Langfuse).

    Best-effort: returns the computed cost but never raises. Used directly for the
    streaming synthesis call (which can't go through ``create_message``).
    """
    u = _usage_dict(usage)
    cost = cost_of(model, u)
    run_id = _run_id.get()
    uid = user_id or _run_user.get()

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
            u["input_tokens"],
            u["output_tokens"],
            u["cache_read_input_tokens"],
            u["cache_creation_input_tokens"],
            round(cost, 6),
        )
    except Exception as exc:  # never break the call over telemetry
        logger.debug("llm_usage ledger insert skipped (%s)", exc)

    tracing.record_generation(
        model=model, usage=u, cost_usd=cost, label=label, user_id=uid, run_id=run_id
    )
    return cost


async def create_message(*, model: str, label: str, user_id: str | None = None, **kwargs: Any):
    """``client.messages.create`` on the shared client, recording usage/cost.

    Drop-in for ``client.messages.create(...)`` plus a required ``label`` (the
    call-site name used in the ledger) and an optional ``user_id``.
    """
    client = get_client()
    resp = await client.messages.create(model=model, **kwargs)
    try:
        await record_usage(model, getattr(resp, "usage", None), label, user_id)
    except Exception as exc:
        logger.debug("usage recording failed (%s)", exc)
    return resp
