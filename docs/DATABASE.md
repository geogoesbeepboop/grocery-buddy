# Database

**Project:** `grocery-buddy` (Supabase, `looimknbtjhvwxbpkbyc`, us-east-1)
**Engine:** PostgreSQL 17 via asyncpg (direct connection, session pooler)
**Migration:** `migrations/001_initial.sql` — applied 2026-06-02

## Entity relationship overview

```
users
 ├── preferences          (1:1)
 ├── amazon_profiles      (1:many)
 ├── schedules            (1:many — usually 1)
 ├── inventory_items      (1:many)
 ├── consumption_profile  (1:many)
 ├── consumption_events   (1:many)
 ├── import_proposals     (1:many — staged order-history imports, pre-confirmation)
 ├── conversation_state   (1:1 — Telegram chat flow state)
 └── carts                (1:many)
       ├── cart_items     (1:many)
       ├── approvals      (1:many — usually 1)
       └── purchases      (1:1)

price_snapshots            (no FK — global price cache)
```

---

## Tables

### `users`
The root entity. One row per person using the agent.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | `gen_random_uuid()` |
| `email` | `TEXT UNIQUE NOT NULL` | Primary identifier |
| `name` | `TEXT` | Display name |
| `created_at` | `TIMESTAMPTZ` | |

**Create a user:**
```sql
INSERT INTO users (email, name) VALUES ('you@example.com', 'George')
RETURNING id;
```
Copy the returned UUID — it's your `--user-id` for all CLI commands.

---

### `preferences`
Per-user configuration. 1:1 with `users`.

| Column | Type | Default | Notes |
|---|---|---|---|
| `user_id` | `UUID PK → users.id` | | |
| `default_store` | `TEXT` | `'amazon'` | `'amazon'` or `'kroger'` |
| `dietary_notes` | `TEXT` | null | Free-form notes for future prompt use |
| `auto_purchase_cap_usd` | `DECIMAL(10,2)` | `50.00` | Orders under this execute automatically; above requires approval |
| `monthly_budget_usd` | `DECIMAL(10,2)` | null | Informational — not enforced yet |
| `lead_time_days` | `FLOAT` | `2.0` | Days of delivery lead time to account for |
| `buffer_days` | `FLOAT` | `1.0` | Safety buffer on top of lead time |
| `updated_at` | `TIMESTAMPTZ` | | |

`auto_purchase_cap_usd` is the single most important preference — it controls how much the agent can spend without asking.

---

### `amazon_profiles`
Tracks the Amazon household/Prime family profiles you've set up.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `profile_name` | `TEXT` | e.g. `'George'`, `'Shared'` |
| `is_default` | `BOOLEAN` | `TRUE` = use this profile for automated runs |
| `created_at` | `TIMESTAMPTZ` | |

---

### `schedules`
Configurable per-user cron. Usually one row per user.

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | `UUID PK` | | |
| `user_id` | `UUID → users.id` | | |
| `cadence` | `TEXT` | `'0 8 * * *'` | Cron expression (UTC) |
| `timezone` | `TEXT` | `'America/New_York'` | For display only; cadence is always UTC |
| `enabled` | `BOOLEAN` | `TRUE` | |
| `temporal_schedule_id` | `TEXT` | null | Temporal Schedule ID for lifecycle management |
| `updated_at` | `TIMESTAMPTZ` | | |

Managed via `grocery-buddy schedule --user-id <id> --cron "0 8 * * *"`.

---

### `inventory_items`
Current pantry state. One row per product per user.

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | `UUID PK` | | |
| `user_id` | `UUID → users.id` | | |
| `product` | `TEXT` | | Product name (free-form, e.g. `'Eggs'`) |
| `qty` | `FLOAT` | `0` | Current **estimated** quantity on hand. A scheduled checkup decays this by `rate × days elapsed` (see `last_estimated_at`). |
| `actual_qty` | `FLOAT` | `NULL` | Last quantity the **user confirmed** ("we still have a dozen"). The anchor the estimate snaps back to on a correction. |
| `unit` | `TEXT` | `'unit'` | `'dozen'`, `'lbs'`, `'oz'`, `'gallon'`, `'count'`, etc. |
| `par_level` | `FLOAT` | `1` | Minimum comfortable qty; triggers restock if no consumption profile |
| `last_estimated_at` | `TIMESTAMPTZ` | `NOW()` | When the estimate was last reconciled. Depletion measures elapsed time from here and only advances it when it actually decrements, so nothing is double-counted. |
| `updated_at` | `TIMESTAMPTZ` | | |

**UNIQUE on `(user_id, product)`** — upsert by product name.

The predictor flags an item when: `qty / effective_daily_rate ≤ lead_time + buffer_days`. If there's no consumption profile, it falls back to `qty ≤ par_level`.

**Estimated vs. actual:** `qty` is the agent's running estimate; `actual_qty` is the last user-confirmed truth. A scheduled grocery run first applies *estimated depletion* (assume the household kept consuming at its usual rate). When the user corrects an item on the fly — "we still have plenty of eggs", "the family used them all" — both `qty` and `actual_qty` snap to the stated amount and `last_estimated_at` resets to now.

---

### `consumption_profile`
Declared consumption habits. One row per product per user.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `product` | `TEXT` | Must match `inventory_items.product` exactly |
| `declared_rate` | `FLOAT` | Units consumed **per day** (e.g. `0.143` = 1 gallon/week) |
| `unit` | `TEXT` | Same unit as the inventory item |
| `household_factor` | `FLOAT` | Multiplier (1.0 = individual, 2.0 = couple) |
| `notes` | `TEXT` | e.g. `'more in summer'` |
| `updated_at` | `TIMESTAMPTZ` | |

**UNIQUE on `(user_id, product)`**.

The predictor blends `declared_rate × household_factor` (prior) with observed events (posterior). After 14+ events, observed rate gets 80% weight.

**Rate conversion cheat sheet:**
| Habit | `declared_rate` |
|---|---|
| 1 dozen eggs / week | `12 / 7 ≈ 1.71` (unit: `count`) |
| 1 gallon milk / week | `1 / 7 ≈ 0.143` (unit: `gallon`) |
| 1 lb coffee / 3 weeks | `1 / 21 ≈ 0.048` (unit: `lbs`) |
| 1 loaf bread / 10 days | `1 / 10 = 0.1` (unit: `loaf`) |

---

### `consumption_events`
Immutable audit log of every consumption/restock event.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `product` | `TEXT` | |
| `delta` | `FLOAT` | **Negative = consumed** (`-1.0`), **positive = restocked** (`+12.0`) |
| `source` | `TEXT CHECK` | `'user_update'` \| `'purchase'` \| `'inferred'` \| `'correction'` |
| `ts` | `TIMESTAMPTZ` | When it happened |

The predictor uses the last 30 days of `delta < 0` events **with `source = 'user_update'`** to calculate the observed consumption rate. Other sources are logged for the audit trail but deliberately excluded from the rate blend:
- `'inferred'` — the agent's own arithmetic depletion. Counting it would let the model feed back on itself.
- `'correction'` — a user resetting their absolute on-hand quantity. A one-off ("family came over and used them all") shouldn't permanently inflate the steady-state rate.

Purchases automatically log a positive delta (restocked).

---

### `price_snapshots`
Global cache of scraped/fetched prices. No FK to users — shared across all users.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `product` | `TEXT` | |
| `store_retailer` | `TEXT` | `'amazon'` or `'kroger'` |
| `price_usd` | `DECIMAL(10,2)` | |
| `unit` | `TEXT` | |
| `product_url` | `TEXT` | Product page URL |
| `asin` | `TEXT` | Amazon ASIN (if retailer = amazon) |
| `kroger_sku` | `TEXT` | Kroger product ID (if retailer = kroger) |
| `captured_at` | `TIMESTAMPTZ` | When the price was scraped |

Not yet used for caching (activities always scrape fresh), but indexed for future cache-first lookup to reduce Playwright usage.

---

### `carts`
One cart per grocery run. Progresses through a status state machine.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `status` | `TEXT CHECK` | State machine — see below |
| `total_usd` | `DECIMAL(10,2)` | Sum of `cart_items.price_usd × qty` |
| `retailer` | `TEXT` | `'amazon'` or `'kroger'` |
| `workflow_id` | `TEXT` | Temporal workflow ID — used by webhook to signal approve/reject |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

**Cart status state machine:**
```
draft
  └─► pending_approval  (if total > auto_purchase_cap)
        ├─► approved
        │     └─► purchased
        ├─► rejected
        └─► expired     (no response in 24h)
  └─► purchased         (if total ≤ auto_purchase_cap — skips approval)
  └─► failed            (any unrecoverable error)
```

---

### `cart_items`
Line items for a cart.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `cart_id` | `UUID → carts.id` | |
| `product` | `TEXT` | Product name |
| `qty` | `FLOAT` | Quantity to buy (usually up to `par_level`) |
| `unit` | `TEXT` | |
| `price_usd` | `DECIMAL(10,2)` | Price per unit at time of cart build |
| `price_source` | `TEXT` | `'amazon_scraped'`, `'kroger_api'`, `'cached'` |
| `asin` | `TEXT` | Amazon ASIN (used for add-to-cart automation) |
| `kroger_sku` | `TEXT` | |
| `notes` | `TEXT` | e.g. exact product title found on Amazon |

---

### `approvals`
Tracks the approval request and decision for carts that exceeded the cap.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `cart_id` | `UUID → carts.id` | |
| `requested_at` | `TIMESTAMPTZ` | When the ntfy push was sent |
| `decided_at` | `TIMESTAMPTZ` | When the user tapped Approve/Reject (null = pending/expired) |
| `decision` | `TEXT CHECK` | `'approved'` \| `'rejected'` \| `'expired'` |
| `channel` | `TEXT` | `'ntfy'` (default) |

---

### `purchases`
Immutable record of every executed or attempted purchase.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `cart_id` | `UUID → carts.id` | |
| `retailer_order_ref` | `TEXT` | Amazon order reference or checkout URL |
| `total_usd` | `DECIMAL(10,2)` | Actual total at checkout |
| `payment_ref` | `TEXT` | Reserved for Stripe virtual card ref |
| `idempotency_key` | `TEXT UNIQUE NOT NULL` | `'purchase-{cart_id}'` — prevents double purchase |
| `status` | `TEXT CHECK` | `'pending'` \| `'completed'` \| `'failed'` |
| `error` | `TEXT` | Error message if `status = 'failed'` |
| `created_at` | `TIMESTAMPTZ` | |

**The `idempotency_key` UNIQUE constraint is the safety net.** Even if the Temporal activity retries, the `ON CONFLICT` on this column prevents a second purchase. The activity checks `status = 'completed'` before proceeding.

---

### `import_proposals`
Staged pantry proposals synthesized from a user's Amazon order history, held for review before anything is written to `inventory_items` / `consumption_profile`.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `source` | `TEXT` | `'amazon_orders'` |
| `items` | `JSONB` | Working list the user edits in review (product, unit, estimated_qty, par_level, daily_rate, preferred_brand, brand_flexibility, last_ordered, times_ordered, category) |
| `status` | `TEXT CHECK` | `'pending_review'` \| `'confirmed'` \| `'discarded'` |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

`ImportHistoryWorkflow` scrapes orders → Sonnet synthesizes the list → it's staged here and the user enters `import_review` conversation mode. Edits ("remove the unhealthy snacks") rewrite `items`; on confirm the list is persisted to the live pantry and the first grocery run starts. Nothing touches inventory until confirmation.

---

## Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_inventory_items_user` | `inventory_items(user_id)` | Load all items for a user |
| `idx_consumption_events_user` | `consumption_events(user_id, product, ts DESC)` | Predictor's 30-day lookback |
| `idx_consumption_profile_user` | `consumption_profile(user_id)` | Load all habits for a user |
| `idx_carts_user_created` | `carts(user_id, created_at DESC)` | Recent carts view |
| `idx_price_snapshots_product` | `price_snapshots(product, store_retailer, captured_at DESC)` | Future cache-first lookup |
| `idx_cart_items_cart` | `cart_items(cart_id)` | Load all items for a cart |
| `idx_purchases_idempotency` | `purchases(idempotency_key)` | Idempotency guard check |
| `idx_import_proposals_user_status` | `import_proposals(user_id, status, created_at DESC)` | Find the active proposal for review |

---

## Useful queries

```sql
-- Current pantry state for a user
SELECT product, qty, unit, par_level
FROM inventory_items
WHERE user_id = '<uuid>'
ORDER BY product;

-- Full cart history with totals
SELECT id, status, total_usd, retailer, created_at
FROM carts
WHERE user_id = '<uuid>'
ORDER BY created_at DESC;

-- Items in a specific cart
SELECT ci.product, ci.qty, ci.unit, ci.price_usd, ci.asin
FROM cart_items ci
WHERE ci.cart_id = '<cart-uuid>';

-- Consumption events in the last 30 days
SELECT product, delta, source, ts
FROM consumption_events
WHERE user_id = '<uuid>'
  AND ts >= NOW() - INTERVAL '30 days'
ORDER BY ts DESC;

-- Purchase history
SELECT p.idempotency_key, p.total_usd, p.status, p.created_at, c.retailer
FROM purchases p
JOIN carts c ON c.id = p.cart_id
WHERE c.user_id = '<uuid>'
ORDER BY p.created_at DESC;
```
