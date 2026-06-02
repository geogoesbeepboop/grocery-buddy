"""Langfuse tracing — no-ops gracefully when keys are absent."""
from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator
from typing import Any

from grocery_buddy.config import settings

_active_trace: contextvars.ContextVar[Any] = contextvars.ContextVar("_active_trace", default=None)


def _langfuse() -> Any | None:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import]
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except ImportError:
        return None


@contextlib.contextmanager
def trace(name: str, user_id: str | None = None, **metadata: Any) -> Iterator[dict[str, Any]]:
    """Wrap a top-level operation in a Langfuse trace (no-op if not configured)."""
    span: dict[str, Any] = {"name": name, "user_id": user_id, "metadata": metadata, "generations": [], "cost_usd": 0.0}
    token = _active_trace.set(span)
    t0 = time.perf_counter()
    try:
        yield span
    finally:
        span["duration_s"] = time.perf_counter() - t0
        _active_trace.reset(token)
        _emit(span)


def log_generation(*, model: str, usage: dict[str, int], cost_usd: float) -> None:
    span = _active_trace.get()
    if span is None:
        return
    span["generations"].append({"model": model, "usage": usage, "cost_usd": cost_usd})
    span["cost_usd"] += cost_usd


def _emit(span: dict[str, Any]) -> None:
    lf = _langfuse()
    if lf is None:
        return
    try:
        trace_obj = lf.trace(
            name=span["name"],
            user_id=span.get("user_id"),
            metadata={
                **span.get("metadata", {}),
                "duration_s": span.get("duration_s"),
                "cost_usd": span.get("cost_usd"),
                "generations": span.get("generations"),
            },
        )
        lf.flush()
    except Exception:
        pass  # never break the run over tracing
