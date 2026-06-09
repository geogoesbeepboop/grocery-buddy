-- 011_llm_usage.sql
--
-- Per-call LLM cost/usage ledger — the authoritative cost record.
--
-- Before this, no call site read `response.usage`, `tracing.py` was never invoked,
-- and the per-run cost alert was fed a hardcoded 0.0 (so it could never fire). Now
-- every Anthropic call (via grocery_buddy.llm) writes one row here with token counts
-- and a computed USD cost. A grocery run's cost = SUM(cost_usd) for its workflow_id,
-- which `evals.sum_run_cost()` feeds into `check_cost_alert()`.
--
-- `user_id` is intentionally NOT a foreign key: this is an append-only observability
-- ledger and a stray/unknown id should never block a write. `workflow_id` is NULL
-- for calls made outside a Temporal run (e.g. the Telegram webhook's intent parse).

CREATE TABLE IF NOT EXISTS llm_usage (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID,
    workflow_id       TEXT,
    label             TEXT,          -- call-site name: parse_request, compose_briefing, ...
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          DECIMAL(12,6) NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot query: sum a run's cost for the cost alert.
CREATE INDEX IF NOT EXISTS idx_llm_usage_workflow ON llm_usage (workflow_id);
-- Cost trend per user over time.
CREATE INDEX IF NOT EXISTS idx_llm_usage_user_created ON llm_usage (user_id, created_at DESC);
