"""Evals: real prediction precision/recall (from snapshots) + per-run cost alert.

prediction accuracy
    At run time ``select_run_candidates_activity`` snapshots the predictor's decision
    for every pantry item (``prediction_snapshots``). Here we score each snapshot
    against what was actually purchased within ``horizon_days``:
        precision = of the items we flagged low, how many were bought
        recall    = of the items bought, how many we'd flagged low
    Micro-averaged across snapshots in the lookback window. Because ``predicted``
    comes from the snapshot (NOT from cart membership), recall is no longer pinned
    to 1.0 — buying something we never flagged lowers it, as it should.

cost alert
    Every LLM call records tokens + cost to the ``llm_usage`` ledger (see
    ``grocery_buddy.llm``). A run's cost = SUM(cost_usd) for its workflow_id; if it
    exceeds the threshold we alert. (It used to be fed a hardcoded 0.0, so the alert
    could never fire.)

Run standalone:  uv run python -m grocery_buddy.evals --user-id <uuid>
"""
from __future__ import annotations

import asyncio
import logging
import uuid as uuid_mod
from datetime import UTC, datetime, timedelta

from grocery_buddy import tracing
from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.notifications import send_error_notification
from grocery_buddy.products import normalize_product
from grocery_buddy.tools.predictions import get_recent_snapshots

logger = logging.getLogger(__name__)


# ── Prediction accuracy ───────────────────────────────────────────────────────


def prediction_metrics(predicted: set[str], relevant: set[str]) -> dict:
    """Pure precision/recall/F1 for a single snapshot.

    ``predicted`` = items the predictor flagged low.
    ``relevant``  = items actually needed (bought) in the horizon.
    Returns confusion counts so callers can micro-average across snapshots.
    """
    tp = predicted & relevant
    fp = predicted - relevant
    fn = relevant - predicted
    precision = len(tp) / len(predicted) if predicted else None
    recall = len(tp) / len(relevant) if relevant else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall
        else None
    )
    return {
        "tp": len(tp),
        "fp": len(fp),
        "fn": len(fn),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _ratio(num: int, den: int) -> float | None:
    return (num / den) if den else None


async def compute_prediction_accuracy(
    user_id: str,
    lookback_days: int | None = None,
    horizon_days: int | None = None,
) -> dict:
    """Micro-averaged precision/recall over prediction snapshots vs. real purchases."""
    lookback_days = lookback_days or settings.eval_lookback_days
    horizon_days = horizon_days or settings.eval_horizon_days
    pool = await get_pool()
    uid = uuid_mod.UUID(user_id)
    now = datetime.now(UTC)
    since = now - timedelta(days=lookback_days)

    snapshots = await get_recent_snapshots(pool, user_id, since)
    if not snapshots:
        return {
            "precision": None,
            "recall": None,
            "note": "no prediction snapshots in window (predictor hasn't run yet, "
            "or runs pre-date this eval)",
            "window_days": lookback_days,
            "horizon_days": horizon_days,
        }

    tp_total = fp_total = fn_total = 0
    snapshots_scored = 0
    for snap in snapshots:
        predicted = {
            normalize_product(p["product"])
            for p in snap["predicted"]
            if p.get("flagged_low")
        }
        window_start = snap["created_at"]
        window_end = window_start + timedelta(days=horizon_days)
        purchased_rows = await pool.fetch(
            """
            SELECT DISTINCT ci.product
            FROM cart_items ci
            JOIN carts c ON c.id = ci.cart_id
            WHERE c.user_id = $1
              AND c.status = 'purchased'
              AND c.created_at >= $2
              AND c.created_at <  $3
            """,
            uid,
            window_start,
            window_end,
        )
        relevant = {normalize_product(r["product"]) for r in purchased_rows}
        if not predicted and not relevant:
            continue
        m = prediction_metrics(predicted, relevant)
        tp_total += m["tp"]
        fp_total += m["fp"]
        fn_total += m["fn"]
        snapshots_scored += 1

    result = {
        "precision": (
            round(_ratio(tp_total, tp_total + fp_total), 3)
            if (tp_total + fp_total)
            else None
        ),
        "recall": (
            round(_ratio(tp_total, tp_total + fn_total), 3)
            if (tp_total + fn_total)
            else None
        ),
        "true_positives": tp_total,
        "false_positives": fp_total,
        "false_negatives": fn_total,
        "snapshots_scored": snapshots_scored,
        "window_days": lookback_days,
        "horizon_days": horizon_days,
    }
    logger.info("Prediction accuracy for %s: %s", user_id, result)
    return result


# ── Per-run LLM cost ──────────────────────────────────────────────────────────


async def sum_run_cost(workflow_id: str | None) -> float:
    """Total LLM cost (USD) recorded for a Temporal run, from the llm_usage ledger."""
    if not workflow_id:
        return 0.0
    pool = await get_pool()
    val = await pool.fetchval(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE workflow_id = $1",
        workflow_id,
    )
    return float(val or 0.0)


# ── Aggregate runner + cost alert ─────────────────────────────────────────────


async def run_evals(user_id: str) -> dict:
    """Run all evals for a user and emit scores to Langfuse (best-effort)."""
    results: dict = {}
    accuracy = await compute_prediction_accuracy(user_id)
    results["prediction_accuracy"] = accuracy
    if accuracy.get("precision") is not None:
        tracing.record_score(
            name="prediction_precision",
            value=accuracy["precision"],
            user_id=user_id,
            comment=str(accuracy),
        )
    if accuracy.get("recall") is not None:
        tracing.record_score(
            name="prediction_recall",
            value=accuracy["recall"],
            user_id=user_id,
            comment=str(accuracy),
        )
    return results


async def check_cost_alert(run_cost_usd: float, user_id: str) -> None:
    """Fire an alert if a single run's cost exceeds the configured threshold."""
    threshold = settings.cost_alert_threshold_usd
    if run_cost_usd > threshold:
        msg = (
            f"Run cost ${run_cost_usd:.4f} exceeded alert threshold "
            f"${threshold:.2f} for user {user_id[:8]}"
        )
        logger.warning(msg)
        await send_error_notification(f"⚠️ Cost alert: {msg}")
    else:
        logger.info("Run cost $%.4f within threshold for %s", run_cost_usd, user_id)


# ── Standalone CLI ────────────────────────────────────────────────────────────


async def _main(user_id: str) -> None:
    results = await run_evals(user_id)
    print("\n── Eval results ──────────────────────────────")
    for key, val in results.items():
        print(f"  {key}: {val}")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run grocery-buddy evals")
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.user_id))
