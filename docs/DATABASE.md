# Database

**Project:** `grocery-buddy` (Supabase, `looimknbtjhvwxbpkbyc`, us-east-1)
**Engine:** PostgreSQL 17 via asyncpg (direct connection, session pooler)
**Migrations:** `migrations/001`–`011` (18 tables). Apply each `.sql` in order in the Supabase SQL editor.

## Entity relationship overview

```
users
 ├── preferences             (1:1)
 ├── amazon_profiles         (1:many)
 ├── amazon_auth_challenges  (1:many — 2FA-code relay mailbox, self-healing login)
 ├── schedules               (1:many — usually 1)
 ├── inventory_items         (1:many)
 ├── consumption_profile     (1:many)
 ├── consumption_events      (1:many)
 ├── pending_replenishments  (1:many — confirmed orders in transit, not yet arrived)
 ├── import_proposals        (1:many — staged order-history imports, pre-confirmation)
 ├── conversation_state      (1:1 — Telegram chat flow state)
 └── carts                   (1:many)
       ├── cart_items        (1:many)
       ├── approvals         (1:many — usually 1)
       ├── purchases         (1:1)
       └── pending_replenishments (1:many — one per confirmed cart line, ON DELETE SET NULL)

price_snapshots               (no FK — global price cache)
prediction_snapshots          (per-run record of what the predictor decided — scored by the accuracy eval)
llm_usage                     (append-only per-call token/cost ledger — feeds the cost alert)
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
| `auto_purchase_cap_usd` | `DECIMAL(10,2)` | `50.00` | **Reserved** for a future auto-buy tier — not enforced today (every cart requires approval) |
| `monthly_budget_usd` | `DECIMAL(10,2)` | null | Informational — not enforced yet |
| `lead_time_days` | `FLOAT` | `2.0` | Days of delivery lead time to account for |
| `buffer_days` | `FLOAT` | `1.0` | Safety buffer on top of lead time |
| `updated_at` | `TIMESTAMPTZ` | | |

`lead_time_days` + `buffer_days` set how early the predictor flags an item. There is no auto-purchase path today: every run ends at an approval gate regardless of total.

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

Purchases automatically log a positive delta (restocked). When an in-transit order's
ETA passes, the arrival reconcile (`replenishment.reconcile_arrivals`) tops up the
pantry and logs a `'purchase'` event for the delivered qty.

---

### `pending_replenishments`
In-transit inventory — confirmed orders that haven't arrived yet (migration `009`).
The "on the way" half of the pantry. One row per confirmed cart line.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `cart_id` | `UUID → carts.id` | **ON DELETE SET NULL** — a cart cleanup never drops an in-transit record the pantry math depends on |
| `product` | `TEXT` | Canonical (normalized) product name |
| `qty` | `FLOAT` | Quantity ordered |
| `unit` | `TEXT` | |
| `ordered_at` | `TIMESTAMPTZ` | When the user confirmed the order |
| `eta` | `TIMESTAMPTZ` | Estimated arrival = `ordered_at + preferences.lead_time_days` |
| `status` | `TEXT CHECK` | `'in_transit'` \| `'arrived'` \| `'cancelled'` |
| `arrived_at` | `TIMESTAMPTZ` | Set when the arrival reconcile lands the row |
| `created_at` | `TIMESTAMPTZ` | |

**Lifecycle:**
```
in_transit  ──(eta passes → reconcile_arrivals)──►  arrived   (pantry topped up, 'purchase' event logged)
    │
    └────────(user: "it never came" / cancel)─────►  cancelled (stops counting as incoming)
```

While `in_transit`, the qty is summed into the predictor's `incoming_by_product` map
and added to on-hand stock, so a confirmed order is **never re-suggested** until it's
due to run out *after* the incoming order is accounted for.

**Idempotency:** a partial unique index on `(cart_id, product) WHERE status =
'in_transit'` means a double-confirm (button tap + text reply, or a webhook racing the
workflow signal) can't create a second batch of in-transit rows.

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
  └─► pending_approval        (briefing sent — ALWAYS; there is no auto-buy path)
        ├─► approved ─► checkout_ready   (Amazon cart staged, checkout link sent;
        │                 │               the user taps "Place order" themselves)
        │                 ├─► purchased   (user confirmed "I placed the order" →
        │                 │                items recorded as in-transit, pantry will
        │                 │                top up on arrival)
        │                 └─► rejected    (user said they didn't order it after all)
        ├─► rejected
        └─► expired           (no response in 24h for GroceryRun / 6h for QuickBuy)
  └─► failed                  (any unrecoverable error)
```
The agent still never *places* an order — the human completes checkout on Amazon. But
`checkout_ready` is no longer terminal: when the user confirms they placed the order,
the cart advances to `purchased` and its lines are recorded in
`pending_replenishments` (in-transit) so the pantry tops up when they arrive and the
agent stops re-suggesting them. See [SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md) §4.1.

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
| `requested_at` | `TIMESTAMPTZ` | When the approval briefing was sent |
| `decided_at` | `TIMESTAMPTZ` | When the user tapped Approve/Reject (null = pending/expired) |
| `decision` | `TEXT CHECK` | `'approved'` \| `'rejected'` \| `'expired'` |
| `channel` | `TEXT` | `'telegram'` |

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
| `status` | `TEXT CHECK` | `'pending'` \| `'checkout_ready'` \| `'completed'` \| `'failed'` (migration `005` added `checkout_ready`) |
| `error` | `TEXT` | Error message if `status = 'failed'` |
| `created_at` | `TIMESTAMPTZ` | |

**The `idempotency_key` UNIQUE constraint is the safety net.** Even if the Temporal activity retries, the `ON CONFLICT` on this column prevents a second purchase. The staging activity sets `checkout_ready` once the Amazon cart is staged; the record advances to `completed` only when the user **confirms they placed the order** (which also records the cart's lines as in-transit).

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

### `conversation_state`
Per-user Telegram chat-flow state. 1:1 with `users`. Lets a free-text reply be
interpreted in the right context (mid-onboarding, mid-import-review, relaying a 2FA
code, or idle).

| Column | Type | Notes |
|---|---|---|
| `user_id` | `UUID PK → users.id` | |
| `mode` | `TEXT` | `'idle'` \| `'onboarding'` \| `'import_review'` \| `'amazon_2fa'` |
| `messages` | `JSONB` | Rolling turn history for the active agent loop |
| `updated_at` | `TIMESTAMPTZ` | |

The webhook routes inbound free text by `mode`: `onboarding`/`import_review` continue
their agent loop; `amazon_2fa` captures the next message as the OTP code (written to
`amazon_auth_challenges`); `idle` falls through to fresh-request / briefing-reply parsing.

---

### `amazon_auth_challenges`
Mailbox that relays an Amazon 2FA one-time code from the webhook process (which
receives the user's Telegram reply) to the worker activity (which is holding the
browser open on the OTP page). Added in migration `008` for the self-healing login.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | |
| `kind` | `TEXT CHECK` | `'otp'` |
| `status` | `TEXT CHECK` | `'pending'` → `'answered'` → `'consumed'`, or `'expired'` |
| `code` | `TEXT` | The user's reply; written when `answered`, read once on `consumed` |
| `created_at` | `TIMESTAMPTZ` | |
| `answered_at` | `TIMESTAMPTZ` | When the user replied |

Flow: the login activity opens a `pending` challenge and asks for the code over
Telegram; the webhook's `submit_otp_code` flips it to `answered`; the activity's
`read_answered_code` atomically consumes it (`consumed`) and submits it to Amazon.
A timeout or a newer challenge `expires` it. See SYSTEM_REFERENCE §10.

---

### `prediction_snapshots`
Per-run record of **what the predictor decided**, so its accuracy can actually be
measured (migration `010`). The old eval compared "items in a purchased cart" against
"items in any cart" — a superset by construction, so recall was pinned at 1.0 and it
couldn't measure the predictor at all. Snapshotting the decision at predict-time lets
`evals.compute_prediction_accuracy` score real precision/recall against what was bought
in the following horizon.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID → users.id` | **ON DELETE CASCADE** |
| `workflow_id` | `TEXT` | The Temporal run that produced this prediction (one snapshot per run) |
| `run_trigger` | `TEXT` | `'schedule'` \| `'manual'` \| `'onboarding'` |
| `predicted` | `JSONB` | Every pantry item the predictor classified: `{product, flagged_low, bucket, days_remaining, effective_rate, qty, incoming}`. The `flagged_low` (bucket == `'low'`) subset is the predicted-low set the eval scores. |
| `lead_time_days` | `FLOAT` | Snapshot of the prefs at run time |
| `buffer_days` | `FLOAT` | |
| `created_at` | `TIMESTAMPTZ` | |

Written once per grocery run by `select_run_candidates_activity` (via
`tools/predictions.record_prediction_snapshot`). **Idempotent:** a partial unique index
on `workflow_id` means a retried select-candidates activity upserts its own snapshot
rather than writing a duplicate that would skew the micro-averaged metric.

---

### `llm_usage`
Append-only per-call **cost/usage ledger** — the authoritative cost record (migration
`011`). Before it, no call site read `response.usage` and the per-run cost alert was fed
a hardcoded `0.0` (so it could never fire). Now every Anthropic call (via
`grocery_buddy.llm`) writes one row with token counts and a computed USD cost; a run's
cost = `SUM(cost_usd)` for its `workflow_id`, which `evals.sum_run_cost()` feeds into
`check_cost_alert()`.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PK` | |
| `user_id` | `UUID` | **No FK** — an observability ledger should never block a write on a stray id |
| `workflow_id` | `TEXT` | `NULL` for calls outside a Temporal run (e.g. the webhook's intent parse) |
| `label` | `TEXT` | Call-site name: `parse_request`, `compose_briefing`, … |
| `model` | `TEXT NOT NULL` | |
| `input_tokens` | `INTEGER` | |
| `output_tokens` | `INTEGER` | |
| `cache_read_tokens` | `INTEGER` | Prompt-cache hits |
| `cache_write_tokens` | `INTEGER` | Prompt-cache writes |
| `cost_usd` | `DECIMAL(12,6)` | Computed at write time from per-model pricing |
| `created_at` | `TIMESTAMPTZ` | |

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
| `idx_amazon_auth_user_status` | `amazon_auth_challenges(user_id, status, created_at DESC)` | Find the latest pending 2FA challenge |
| `idx_pending_replen_user_status` | `pending_replenishments(user_id, status, eta)` | Incoming-stock map + due-arrival reconcile |
| `uq_pending_replen_cart_product` | `pending_replenishments(cart_id, product) WHERE status='in_transit'` | Idempotent confirm — one in-transit set per cart |
| `uq_prediction_snapshots_workflow` | `prediction_snapshots(workflow_id) WHERE workflow_id IS NOT NULL` | One snapshot per run (idempotent upsert) |
| `idx_prediction_snapshots_user_created` | `prediction_snapshots(user_id, created_at DESC)` | Recent snapshots for a user |
| `idx_llm_usage_workflow` | `llm_usage(workflow_id)` | Sum a run's cost for the cost alert |
| `idx_llm_usage_user_created` | `llm_usage(user_id, created_at DESC)` | Cost trend per user over time |

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

-- What's on the way right now (and what prediction treats as covered stock)
SELECT product, qty, unit, eta
FROM pending_replenishments
WHERE user_id = '<uuid>' AND status = 'in_transit'
ORDER BY eta;
```
