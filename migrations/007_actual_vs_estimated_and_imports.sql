-- 007_actual_vs_estimated_and_imports.sql
--
-- Two features:
--
-- (A) Actual vs. estimated pantry quantity
--     Until now `inventory_items.qty` was a single number that only changed when
--     an explicit consumption event was logged. We now treat `qty` as the running
--     ESTIMATE the agent maintains: a scheduled grocery checkup decrements it by
--     (effective daily rate × days elapsed), assuming the household kept consuming
--     at its usual pace. `actual_qty` records the last quantity the USER actually
--     confirmed ("we still have a full dozen eggs", "family used them all"), and is
--     the anchor we snap the estimate back to when they correct us. `last_estimated_at`
--     timestamps when the estimate was last reconciled so depletion never double-counts.
--
-- (B) Order-history import proposals
--     The onboarding importer scrapes the user's Amazon order history, has Sonnet
--     synthesize a candidate pantry + habits list, and stages it here for the user
--     to review/edit BEFORE anything is written to inventory/consumption tables.

-- ── (A) Inventory: estimated vs. actual ──────────────────────────────────────
ALTER TABLE inventory_items
    ADD COLUMN IF NOT EXISTS actual_qty        FLOAT,
    ADD COLUMN IF NOT EXISTS last_estimated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Backfill: existing quantities were user-declared, so they ARE the last actual,
-- and the estimate is fresh as of the last update.
UPDATE inventory_items
SET actual_qty = COALESCE(actual_qty, qty),
    last_estimated_at = COALESCE(last_estimated_at, updated_at);

-- ── (A) Consumption events: add a 'correction' source ────────────────────────
-- 'correction' = the user reset the absolute on-hand quantity (state correction),
-- 'inferred'   = the agent's own arithmetic depletion.
-- Both are EXCLUDED from the observed-consumption-rate blend (see predictor.py):
-- only genuine 'user_update' consumption episodes inform the rate, so a one-off
-- ("family came over") or the model's own estimate can't feed back on the rate.
ALTER TABLE consumption_events DROP CONSTRAINT IF EXISTS consumption_events_source_check;
ALTER TABLE consumption_events ADD CONSTRAINT consumption_events_source_check
    CHECK (source IN ('user_update', 'purchase', 'inferred', 'correction'));

-- ── (B) Import proposals (staged, pre-confirmation) ──────────────────────────
CREATE TABLE IF NOT EXISTS import_proposals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT 'amazon_orders',
    -- Working list the user edits in review; each element holds product, unit,
    -- estimated_qty, par_level, daily_rate, preferred_brand, brand_flexibility,
    -- last_ordered, times_ordered, category. Persisted to inventory + consumption
    -- only on confirm.
    items       JSONB NOT NULL DEFAULT '[]'::jsonb,
    status      TEXT NOT NULL DEFAULT 'pending_review'
                  CHECK (status IN ('pending_review', 'confirmed', 'discarded')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_import_proposals_user_status
    ON import_proposals (user_id, status, created_at DESC);
