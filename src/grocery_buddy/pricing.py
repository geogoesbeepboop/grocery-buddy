"""Approximate Claude token prices for the cost ledger + per-run cost alert.

Figures are public list prices (USD per 1,000,000 tokens) and may drift — they
only drive an *internal* cost estimate and the cost alert, so approximate is fine.
Anthropic reports cached reads / cache writes separately from ``input_tokens``,
so each bucket is priced independently (input_tokens already EXCLUDES cache reads).
Override or extend ``MODEL_PRICES`` to refine.
"""
from __future__ import annotations

from typing import Any

# model-prefix → (input, output, cache_read, cache_write) USD per 1M tokens.
# Longest matching prefix wins, so dated snapshots ("claude-haiku-4-5-20251001")
# resolve to their family rate.
MODEL_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-4": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4": (1.00, 5.00, 0.10, 1.25),
    # family fallbacks
    "claude-opus": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku": (1.00, 5.00, 0.10, 1.25),
}

# Used when a model name matches nothing above — bias to Sonnet so we never
# under-estimate cost (a cost alert that under-reports is worse than one that over-reports).
_DEFAULT = (3.00, 15.00, 0.30, 3.75)


def _rates(model: str) -> tuple[float, float, float, float]:
    m = (model or "").lower()
    best_key: str | None = None
    best_rates = _DEFAULT
    for key, rates in MODEL_PRICES.items():
        if m.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key, best_rates = key, rates
    return best_rates


def _tok(usage: Any, name: str) -> int:
    if isinstance(usage, dict):
        return int(usage.get(name) or 0)
    return int(getattr(usage, name, 0) or 0)


def cost_of(model: str, usage: Any) -> float:
    """USD cost of one generation, given an Anthropic ``usage`` object or dict.

    Recognized usage fields: ``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens`` (missing → 0).
    """
    in_rate, out_rate, cr_rate, cw_rate = _rates(model)
    return (
        _tok(usage, "input_tokens") * in_rate
        + _tok(usage, "output_tokens") * out_rate
        + _tok(usage, "cache_read_input_tokens") * cr_rate
        + _tok(usage, "cache_creation_input_tokens") * cw_rate
    ) / 1_000_000.0
