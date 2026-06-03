"""Canonical product-name normalization.

Products are keyed by their name across inventory, consumption profiles, and
consumption events, so the name must normalize *identically* everywhere — else
"Milk" and "milk" become two separate items that never join (duplicate pantry
entries, double-ordering). Canonical form: trimmed, internal whitespace
collapsed, lowercased.
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def normalize_product(name: str) -> str:
    """Return the canonical key for a product name (trim + collapse WS + lower)."""
    return _WS.sub(" ", (name or "").strip()).lower()
