-- 009_pending_replenishments.sql
--
-- In-transit / "on the way" inventory.
--
-- Until now the agent's view of the pantry ended at the moment it handed the user
-- a checkout link: it had no idea whether the order was actually placed, and no
-- memory that eggs are *already on the way* — so the next morning's run would
-- happily suggest eggs again. This table closes that loop.
--
-- When the user confirms they placed the staged Amazon order, we record each cart
-- line here as an in-transit replenishment with an estimated arrival (ordered_at +
-- the user's lead_time_days). Two things then change:
--
--   (1) Prediction counts in-transit qty as already-covered stock, so a confirmed
--       order is never re-suggested while it's on the way ("don't buy eggs tomorrow
--       if I accepted eggs today").
--   (2) Once the ETA passes, a reconcile step LANDS the arrival: it adds the qty to
--       the pantry estimate, logs a 'purchase' consumption event, and flips the row
--       to 'arrived'. Idempotent — only ever lands a row once.
--
-- This is the consumer-grocery analog of the procurement-agent's open-mandate +
-- settled-purchase lifecycle (see docs/PROCUREMENT_CONVERGENCE.md): a purchase is a
-- first-class, time-aware state, not a fire-and-forget link.

CREATE TABLE IF NOT EXISTS pending_replenishments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The order this line came from. SET NULL (not CASCADE) so a cart cleanup never
    -- silently drops an in-transit record the pantry math still depends on.
    cart_id     UUID REFERENCES carts(id) ON DELETE SET NULL,
    product     TEXT NOT NULL,            -- canonical (normalized) product name
    qty         FLOAT NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'unit',
    ordered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Estimated arrival = ordered_at + lead_time_days. Until this passes the qty is
    -- counted as incoming stock; after it passes the reconcile step lands it.
    eta         TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'in_transit'
                  CHECK (status IN ('in_transit', 'arrived', 'cancelled')),
    arrived_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The two hot queries: "what's still on the way for this user" (incoming-stock map +
-- /status) and "what's due to land now" (reconcile). Both filter by user + status.
CREATE INDEX IF NOT EXISTS idx_pending_replen_user_status
    ON pending_replenishments (user_id, status, eta);

-- Idempotency guard for confirmation: one in-transit set per cart. A double-confirm
-- (button tap + text reply, or a workflow signal racing the webhook) can't create a
-- second batch of in-transit rows for the same order.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_replen_cart_product
    ON pending_replenishments (cart_id, product)
    WHERE status = 'in_transit';
