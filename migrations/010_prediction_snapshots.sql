-- 010_prediction_snapshots.sql
--
-- Make the predictor measurable.
--
-- The old prediction eval (evals.py) compared "items in a purchased cart" against
-- "items in ANY cart" — the second set is a superset of the first by construction,
-- so recall was pinned at 1.0 and "precision" was really cart-conversion rate. It
-- could not measure the predictor at all.
--
-- The fix: at the moment the predictor runs, snapshot WHAT IT DECIDED for every
-- pantry item (flagged-low or not). Later we compare that snapshot against what was
-- actually bought in the following horizon to compute real precision/recall:
--   precision = of the items we flagged low, how many were actually bought
--   recall    = of the items actually bought, how many we had flagged low
-- Because `predicted` now comes from the snapshot (independent of what lands in a
-- cart), recall is no longer pinned — buying something we never flagged lowers it.

CREATE TABLE IF NOT EXISTS prediction_snapshots (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The Temporal run that produced this prediction (one snapshot per run).
    workflow_id    TEXT,
    run_trigger    TEXT,          -- schedule | manual | onboarding
    -- Every pantry item the predictor classified, as a JSON array of:
    --   {product, flagged_low, bucket, days_remaining, effective_rate, qty, incoming}
    -- `flagged_low` (bucket == 'low') is the predicted-low set the eval scores.
    predicted      JSONB NOT NULL DEFAULT '[]'::jsonb,
    lead_time_days FLOAT,
    buffer_days    FLOAT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One snapshot per run: a retried select-candidates activity upserts rather than
-- writing a duplicate that would skew the micro-averaged metric.
CREATE UNIQUE INDEX IF NOT EXISTS uq_prediction_snapshots_workflow
    ON prediction_snapshots (workflow_id)
    WHERE workflow_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_prediction_snapshots_user_created
    ON prediction_snapshots (user_id, created_at DESC);
