"""Best-effort Langfuse observability — no-ops without keys or on any SDK drift.

The *authoritative* cost/usage record is the ``llm_usage`` table (see
``grocery_buddy.llm`` + migration 011); Langfuse here is optional dashboards layered
on top. Every call is wrapped so a Langfuse version/API change can never break a
model call or an eval. (The repo's installed Langfuse is v4 — OTEL-based — so the
old v2 ``lf.trace()/lf.score()`` calls this module used to make were dead; these
helpers probe for whichever API surface is present and degrade silently.)
"""
from __future__ import annotations

import logging
from typing import Any

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)


def _client() -> Any | None:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import]

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:  # import error, bad keys, version drift — stay silent
        return None


def _flush(lf: Any) -> None:
    try:
        lf.flush()
    except Exception:
        pass


def record_generation(
    *,
    model: str,
    usage: dict[str, int],
    cost_usd: float,
    label: str,
    user_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Mirror one generation's tokens+cost into Langfuse (best-effort)."""
    lf = _client()
    if lf is None:
        return
    meta = {"cost_usd": cost_usd, "run_id": run_id, "model": model, "label": label, **usage}
    try:
        # v3/v4 OTEL SDK: a span/event carries the metadata; v2 used lf.trace().
        if hasattr(lf, "create_event"):
            lf.create_event(name=label or "llm", metadata=meta)
        elif hasattr(lf, "trace"):
            lf.trace(name=label or "llm", user_id=user_id, metadata=meta)
        _flush(lf)
    except Exception as exc:
        logger.debug("langfuse generation skipped (%s)", exc)


def record_score(
    *, name: str, value: float, user_id: str | None = None, comment: str | None = None
) -> None:
    """Emit a numeric eval score to Langfuse (best-effort)."""
    lf = _client()
    if lf is None:
        return
    try:
        if hasattr(lf, "create_score"):  # v3/v4
            lf.create_score(name=name, value=value, comment=comment)
        elif hasattr(lf, "score"):  # v2
            lf.score(name=name, value=value, comment=comment)
        _flush(lf)
    except Exception as exc:
        logger.debug("langfuse score skipped (%s)", exc)
