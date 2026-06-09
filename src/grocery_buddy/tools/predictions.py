"""Prediction snapshots — persist what the predictor decided, for the accuracy eval.

Written once per grocery run by ``select_run_candidates_activity``; read by
``evals.compute_prediction_accuracy`` to score precision/recall against what was
actually purchased afterward. See migration 010 for the rationale (the old eval
could not measure the predictor at all).
"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

import asyncpg


async def record_prediction_snapshot(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    workflow_id: str | None,
    run_trigger: str | None,
    predicted: list[dict],
    lead_time_days: float,
    buffer_days: float,
) -> None:
    """Upsert one snapshot per run (keyed by workflow_id).

    Idempotent: a retried select-candidates activity overwrites its own snapshot
    rather than writing a duplicate that would skew the micro-averaged metric.
    """
    await pool.execute(
        """
        INSERT INTO prediction_snapshots
            (user_id, workflow_id, run_trigger, predicted, lead_time_days, buffer_days)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        ON CONFLICT (workflow_id) WHERE workflow_id IS NOT NULL
        DO UPDATE SET predicted      = EXCLUDED.predicted,
                      run_trigger    = EXCLUDED.run_trigger,
                      lead_time_days = EXCLUDED.lead_time_days,
                      buffer_days    = EXCLUDED.buffer_days,
                      created_at     = NOW()
        """,
        UUID(user_id),
        workflow_id,
        run_trigger,
        json.dumps(predicted),
        lead_time_days,
        buffer_days,
    )


async def get_recent_snapshots(
    pool: asyncpg.Pool, user_id: str, since: datetime
) -> list[dict]:
    """Snapshots for a user created on/after ``since``, oldest first."""
    rows = await pool.fetch(
        """
        SELECT id, workflow_id, run_trigger, predicted, created_at
        FROM prediction_snapshots
        WHERE user_id = $1 AND created_at >= $2
        ORDER BY created_at
        """,
        UUID(user_id),
        since,
    )
    out: list[dict] = []
    for r in rows:
        predicted = r["predicted"]
        if isinstance(predicted, str):
            predicted = json.loads(predicted)
        out.append(
            {
                "id": str(r["id"]),
                "workflow_id": r["workflow_id"],
                "run_trigger": r["run_trigger"],
                "predicted": predicted or [],
                "created_at": r["created_at"],
            }
        )
    return out
