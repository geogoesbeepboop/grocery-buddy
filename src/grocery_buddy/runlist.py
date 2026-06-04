"""Assemble a grocery-run cart that clears Amazon's free-shipping minimum.

The predictor tells us what's *running low* (must-buy). But a cart of one or two
low items often falls under Amazon's ~$25 free next-day shipping minimum, so the
user either eats a delivery fee or we nudge them toward a tiny order. This module
adds the two judgments a run needs on top of raw prediction:

  split_run_candidates(levels)      what to buy now (LOW) vs. what we *could* add to
                                    round the order out (MEDIUM, soonest-to-deplete
                                    first) — the natural things to batch in.
  assemble_for_free_shipping(...)   given prices, keep every must-buy line and pull
                                    in the next-due fillers until the cart clears the
                                    threshold, returning a human reason so the
                                    briefing can explain why the extras are there.

Pure and I/O-free, so the Temporal activities stay thin and this stays unit-tested.
"""
from __future__ import annotations

from grocery_buddy.predictor import LOW, MEDIUM, StockLevel

# Tier tags carried on each candidate/priced line so the assembler can tell a
# must-buy (running low) from a filler (added only to reach free shipping).
MUST_BUY = "must_buy"
FILLER = "filler"


def split_run_candidates(
    levels: list[StockLevel], max_fillers: int
) -> tuple[list[StockLevel], list[StockLevel]]:
    """Partition classified stock into must-buy (LOW) and filler (MEDIUM) lists.

    Fillers are the items due to run out soonest after the low ones — the natural
    candidates to batch into the same order. At most ``max_fillers`` are returned
    (soonest-first) so we never price an unbounded number of items.
    """
    must_buy = [lv for lv in levels if lv.bucket == LOW]
    fillers = sorted(
        (lv for lv in levels if lv.bucket == MEDIUM),
        key=lambda lv: lv.days_remaining,
    )
    if max_fillers and max_fillers > 0:
        fillers = fillers[:max_fillers]
    return must_buy, fillers


def _line_total(item: dict) -> float:
    return float(item.get("price_usd") or 0) * float(item.get("qty") or 1)


def _filler_sort_key(item: dict) -> float:
    days = item.get("days_remaining")
    return days if isinstance(days, (int, float)) else float("inf")


def assemble_for_free_shipping(
    priced: list[dict],
    threshold: float,
    max_fillers: int,
) -> tuple[list[dict], str | None]:
    """Build the final cart line-items + a human reason for the briefing.

    ``priced`` is the priced output for must-buy AND filler candidates, each tagged
    with ``tier`` (``MUST_BUY``/``FILLER``) and carrying ``days_remaining``. Every
    must-buy line is always kept; fillers are added soonest-first only until the
    running total clears ``threshold`` (and never more than ``max_fillers``).

    Returns ``([], None)`` when nothing must-buy priced — the caller treats that as a
    pricing failure rather than shipping a cart of non-urgent fillers.
    """
    must = [p for p in priced if p.get("tier", MUST_BUY) == MUST_BUY]
    if not must:
        return [], None

    fillers = sorted(
        (p for p in priced if p.get("tier") == FILLER),
        key=_filler_sort_key,
    )

    final = list(must)
    total = sum(_line_total(p) for p in must)
    added: list[dict] = []

    if total < threshold:
        for f in fillers:
            if len(added) >= max_fillers or total >= threshold:
                break
            final.append(f)
            added.append(f)
            total += _line_total(f)

    return final, _reason(len(must), added, total, threshold)


def _reason(n_low: int, added: list[dict], total: float, threshold: float) -> str | None:
    """A natural-language note explaining the cart, or None to use the default."""
    if not added:
        # We already cleared the bar (or had no fillers) — let the briefing use its
        # default "here's what you're low on" framing.
        return None
    low_phrase = f"{n_low} item{'s' if n_low != 1 else ''}"
    add_phrase = f"{len(added)} more you'll need soon"
    if total >= threshold:
        return (
            f"You're running low on {low_phrase}. I added {add_phrase} so the order "
            f"clears the ${threshold:g} free next-day shipping minimum."
        )
    return (
        f"You're running low on {low_phrase}, plus {add_phrase}. Heads up — it's still "
        f"under the ${threshold:g} free-shipping minimum, so you may see a small delivery fee."
    )
