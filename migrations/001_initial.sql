-- Grocery Buddy initial schema

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Users ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    name        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Preferences ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS preferences (
    user_id             UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    default_store       TEXT NOT NULL DEFAULT 'amazon',
    dietary_notes       TEXT,
    auto_purchase_cap_usd   DECIMAL(10,2) NOT NULL DEFAULT 50.00,
    monthly_budget_usd      DECIMAL(10,2),
    lead_time_days      FLOAT NOT NULL DEFAULT 2.0,
    buffer_days         FLOAT NOT NULL DEFAULT 1.0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Amazon profiles (household / Prime family) ────────────────────────────────

CREATE TABLE IF NOT EXISTS amazon_profiles (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile_name TEXT NOT NULL,
    is_default   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Schedules ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedules (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cadence              TEXT NOT NULL DEFAULT '0 8 * * *',  -- cron (UTC)
    timezone             TEXT NOT NULL DEFAULT 'America/New_York',
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    temporal_schedule_id TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Inventory ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_items (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product    TEXT NOT NULL,
    qty        FLOAT NOT NULL DEFAULT 0,
    unit       TEXT NOT NULL DEFAULT 'unit',
    par_level  FLOAT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, product)
);

-- ── Consumption profiles (declared habits) ────────────────────────────────────

CREATE TABLE IF NOT EXISTS consumption_profile (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product          TEXT NOT NULL,
    declared_rate    FLOAT NOT NULL,  -- units consumed per day
    unit             TEXT NOT NULL DEFAULT 'unit',
    household_factor FLOAT NOT NULL DEFAULT 1.0,
    notes            TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, product)
);

-- ── Consumption events (history) ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS consumption_events (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product TEXT NOT NULL,
    delta   FLOAT NOT NULL,  -- negative = consumed, positive = restocked
    source  TEXT NOT NULL CHECK (source IN ('user_update', 'purchase', 'inferred')),
    ts      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Price snapshots ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_snapshots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product       TEXT NOT NULL,
    store_retailer TEXT NOT NULL,
    price_usd     DECIMAL(10,2) NOT NULL,
    unit          TEXT,
    product_url   TEXT,
    asin          TEXT,
    kroger_sku    TEXT,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Carts ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS carts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','pending_approval','approved','purchased','failed','rejected','expired')),
    total_usd    DECIMAL(10,2),
    retailer     TEXT NOT NULL DEFAULT 'amazon',
    workflow_id  TEXT,  -- Temporal workflow ID (used to send approve/reject signals)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Cart items ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cart_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cart_id      UUID NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
    product      TEXT NOT NULL,
    qty          FLOAT NOT NULL,
    unit         TEXT NOT NULL DEFAULT 'unit',
    price_usd    DECIMAL(10,2),
    price_source TEXT,  -- 'amazon_scraped', 'kroger_api', 'cached'
    asin         TEXT,
    kroger_sku   TEXT,
    notes        TEXT
);

-- ── Approvals ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS approvals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cart_id      UUID NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at   TIMESTAMPTZ,
    decision     TEXT CHECK (decision IN ('approved', 'rejected', 'expired')),
    channel      TEXT NOT NULL DEFAULT 'ntfy'
);

-- ── Purchases ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS purchases (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cart_id           UUID NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
    retailer_order_ref TEXT,
    total_usd         DECIMAL(10,2),
    payment_ref       TEXT,
    idempotency_key   TEXT UNIQUE NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'completed', 'failed')),
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_inventory_items_user      ON inventory_items (user_id);
CREATE INDEX IF NOT EXISTS idx_consumption_events_user   ON consumption_events (user_id, product, ts DESC);
CREATE INDEX IF NOT EXISTS idx_consumption_profile_user  ON consumption_profile (user_id);
CREATE INDEX IF NOT EXISTS idx_carts_user_created        ON carts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_product   ON price_snapshots (product, store_retailer, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_cart_items_cart           ON cart_items (cart_id);
CREATE INDEX IF NOT EXISTS idx_purchases_idempotency     ON purchases (idempotency_key);
