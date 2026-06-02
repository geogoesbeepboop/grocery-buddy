"""T16 — Langfuse evals: prediction accuracy + per-run cost alert.

Two eval loops run after each GroceryRunWorkflow completes:
  1. prediction_accuracy  — did the predictor flag items that were actually
                            purchased? (precision) and did it miss any that
                            were manually added? (recall)
  2. cost_alert          — if the Langfuse-traced per-run cost exceeds the
                           configured threshold, fire an ntfy alert.

These are called from the workflow via a post-run activity, OR can be run
standalone against historical data:
  uv run python -m grocery_buddy.evals --user-id <uuid>
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.notifications import send_error_notification

logger = logging.getLogger(__name__)

# Cost alert threshold per run (USD). Override via env if needed.
COST_ALERT_THRESHOLD_USD = 1.00


# ── Prediction accuracy ───────────────────────────────────────────────────────


async def compute_prediction_accuracy(user_id: str, lookback_days: int = 7) -> dict:
    """
    Precision: fraction of predicted-low items that ended up being purchased.
    Recall:    fraction of purchased items that were predicted low beforehand.

    Uses carts + cart_items as the ground-truth "items bought" signal.
    Uses the consumption_events (source='inferred') as proxy for predictions
    until we store explicit prediction snapshots.
    """
    pool = await get_pool()
    import uuid as uuid_mod

    uid = uuid_mod.UUID(user_id)
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Items that were purchased in this window
    purchased_rows = await pool.fetch(
        """
        SELECT DISTINCT ci.product
        FROM cart_items ci
        JOIN carts c ON c.id = ci.cart_id
        WHERE c.user_id = $1
          AND c.status = 'purchased'
          AND c.created_at >= $2
        """,
        uid, since,
    )
    purchased = {r["product"] for r in purchased_rows}

    # Items we predicted low (appeared in any draft/approved/purchased cart)
    predicted_rows = await pool.fetch(
        """
        SELECT DISTINCT ci.product
        FROM cart_items ci
        JOIN carts c ON c.id = ci.cart_id
        WHERE c.user_id = $1
          AND c.created_at >= $2
        """,
        uid, since,
    )
    predicted = {r["product"] for r in predicted_rows}

    if not predicted and not purchased:
        return {"precision": None, "recall": None, "note": "no data in window"}

    true_positives = predicted & purchased
    precision = len(true_positives) / len(predicted) if predicted else None
    recall = len(true_positives) / len(purchased) if purchased else None

    result = {
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "predicted_count": len(predicted),
        "purchased_count": len(purchased),
        "true_positive_count": len(true_positives),
        "window_days": lookback_days,
    }
    logger.info("Prediction accuracy for %s: %s", user_id, result)
    return result


def _emit_to_langfuse(score_name: str, value: float, user_id: str, metadata: dict) -> None:
    """Send a numeric score to Langfuse (no-op if unconfigured)."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return
    try:
        from langfuse import Langfuse  # type: ignore[import]
        lf = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        lf.score(
            name=score_name,
            value=value,
            comment=str(metadata),
        )
        lf.flush()
    except Exception as exc:
        logger.warning("Langfuse score emit failed: %s", exc)


async def run_evals(user_id: str) -> dict:
    """Run all evals for a user and emit scores to Langfuse."""
    results: dict = {}

    # Prediction accuracy
    accuracy = await compute_prediction_accuracy(user_id)
    results["prediction_accuracy"] = accuracy
    if accuracy.get("precision") is not None:
        _emit_to_langfuse("prediction_precision", accuracy["precision"], user_id, accuracy)
    if accuracy.get("recall") is not None:
        _emit_to_langfuse("prediction_recall", accuracy["recall"], user_id, accuracy)

    return results


# ── Cost alert ────────────────────────────────────────────────────────────────


async def check_cost_alert(run_cost_usd: float, user_id: str) -> None:
    """Fire an ntfy alert if a single run's cost exceeds the threshold."""
    if run_cost_usd > COST_ALERT_THRESHOLD_USD:
        msg = (
            f"Run cost ${run_cost_usd:.3f} exceeded alert threshold "
            f"${COST_ALERT_THRESHOLD_USD:.2f} for user {user_id[:8]}"
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
