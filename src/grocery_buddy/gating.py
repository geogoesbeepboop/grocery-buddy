"""Money-live readiness gate.

The autonomous-buy spine is built **sandbox-first but money-live-READY**: flipping
``settings.money_live`` on is only honored when EVERY condition here passes. The
conditions are outputs of the eval layer — which is exactly why making the evals real
(predictor precision, the cost ledger, the scraper monitor) is the prerequisite for
ever safely flipping the switch. Auto-buy code MUST call ``money_live_ready`` and
refuse to spend when it returns ``ready=False``.

This is intentionally conservative: ``checkout_verified`` is a hard stop until staged-
cart execution verification (cart == approved items, incl. the cart-clear fix) is
implemented, so the gate cannot pass today even with flags on.
"""
from __future__ import annotations

import logging

from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.evals import compute_prediction_accuracy

logger = logging.getLogger(__name__)


async def money_live_ready(user_id: str) -> dict:
    """Evaluate every money-live precondition. Returns {ready, conditions}."""
    conditions: dict[str, dict] = {}

    # 1) Both feature flags explicitly enabled.
    conditions["flags_enabled"] = {
        "pass": bool(settings.auto_buy_enabled and settings.money_live),
        "detail": f"auto_buy_enabled={settings.auto_buy_enabled}, money_live={settings.money_live}",
    }

    # 2) Predictor precision above the floor (don't auto-buy off a bad predictor).
    acc = await compute_prediction_accuracy(user_id)
    prec = acc.get("precision")
    conditions["predictor_precision"] = {
        "pass": prec is not None and prec >= settings.gate_predictor_precision_floor,
        "detail": (
            f"precision={prec} floor={settings.gate_predictor_precision_floor}"
            + (f" — {acc['note']}" if acc.get("note") else "")
        ),
    }

    # 3) Scraper health green (extraction works → we'd add the RIGHT item).
    from grocery_buddy.monitoring import check_scraper_health

    health = await check_scraper_health(notify=False)
    conditions["scraper_green"] = {
        "pass": health["status"] == "green",
        "detail": f"scraper={health['status']} {health.get('detail', '')}".strip(),
    }

    # 4) Recent per-run LLM cost under the ceiling (catch runaway spend).
    pool = await get_pool()
    max_run_cost = await pool.fetchval(
        """
        SELECT COALESCE(MAX(run_cost), 0) FROM (
            SELECT SUM(cost_usd) AS run_cost
            FROM llm_usage
            WHERE workflow_id IS NOT NULL
              AND created_at > NOW() - INTERVAL '14 days'
            GROUP BY workflow_id
        ) t
        """
    )
    conditions["cost_under_ceiling"] = {
        "pass": float(max_run_cost or 0) <= settings.gate_run_cost_ceiling_usd,
        "detail": (
            f"max_recent_run_cost=${float(max_run_cost or 0):.4f} "
            f"ceiling=${settings.gate_run_cost_ceiling_usd}"
        ),
    }

    # 5) Checkout execution verification (staged cart == approved items, incl. the
    #    cart-clear fix). HARD STOP until implemented — a signed mandate bounds intent,
    #    not execution, so we must verify what actually landed in the cart before money.
    conditions["checkout_verified"] = {
        "pass": False,
        "detail": "execution verification (staged cart == approved cart) not yet implemented — hard stop",
    }

    ready = all(c["pass"] for c in conditions.values())
    logger.info("money_live_ready(%s) → %s", user_id, ready)
    return {"ready": ready, "conditions": conditions}
