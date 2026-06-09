"""Synthetic scraper health monitor — catch silent Amazon-selector breakage.

The Amazon automation fails *silently* on UI churn: a renamed selector returns ``[]``
and is swallowed at ``logger.warning``, so pricing/import quietly under-deliver and the
only symptom is a vague "couldn't pull anything from Amazon" message. This probe
searches for known-stable staples and asserts we still extract a price + ASIN; on
regression it pages the user so churn surfaces on first break, not after a week of
empty runs. It is also a precondition of the money-live gate (``gating.py``).

Run it on a schedule (cron / a Temporal schedule) or ad-hoc: ``grocery-buddy scraper-health``.
"""
from __future__ import annotations

import logging

from grocery_buddy.notifications import send_error_notification

logger = logging.getLogger(__name__)

# Staples that should always return grocery results on Amazon search.
_PROBE_QUERIES = ["milk", "eggs", "paper towels"]


async def check_scraper_health(notify: bool = True) -> dict:
    """Probe Amazon search and assert price + ASIN still extract.

    Returns ``{"status": "green"|"red", "checked": [...], "detail": str}``. Best-effort:
    a thrown error becomes a red status (never an exception to the caller).
    """
    from grocery_buddy.automation.amazon import get_browser_context, search_grocery_price

    checked: list[dict] = []
    status = "green"
    detail = ""
    try:
        p, context = await get_browser_context()
        try:
            for q in _PROBE_QUERIES:
                cands = await search_grocery_price(q, context)
                ok = bool(cands) and any(c.get("price_usd") and c.get("asin") for c in cands)
                checked.append({"query": q, "candidates": len(cands), "ok": ok})
                if not ok:
                    status = "red"
        finally:
            await context.close()
            await p.stop()
    except Exception as exc:
        status = "red"
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("Scraper health probe errored: %s", detail)

    summary = {"status": status, "checked": checked, "detail": detail}
    logger.info("Scraper health: %s", summary)

    if status == "red" and notify:
        bad = [c["query"] for c in checked if not c["ok"]] or ["probe errored"]
        await send_error_notification(
            "🔴 Scraper health RED — Amazon extraction failed for: "
            + ", ".join(bad)
            + (f" ({detail})" if detail else "")
            + ". Selectors likely churned; pricing + import will silently under-deliver."
        )
    return summary
