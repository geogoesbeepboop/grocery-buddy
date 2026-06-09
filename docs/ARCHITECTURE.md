# Architecture

> This file covers **why** the stack is shaped the way it is. For the exhaustive
> **what** — every model, agentic loop, workflow, tool, data model, and decision
> tree — see **[SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md)** (kept current).

## What grocery-buddy is

A 24/7 autonomous agent that tracks your pantry, predicts what's running low, and
builds and prices an Amazon grocery cart. Every run ends at an **approval gate**: you
review an itemized briefing over Telegram, and on approval the agent stages the cart
and hands back a checkout link you complete yourself. It **never places an order** —
there is no auto-purchase path today (`auto_purchase_cap_usd` is reserved for a future
auto-buy tier). All notification and chat runs over **Telegram**.

## Layer map

```
┌─────────────────────────────────────────────────────────────────┐
│  RUNTIME LAYER — how one agent turn executes                     │
│  Anthropic SDK (claude-sonnet-4-6 / claude-haiku-4-5)            │
│  Intent parsing, onboarding/import chat, synthesis, brand pick   │
└─────────────────────────────────────────────────────────────────┘
         ▲ called from activities / the webhook
┌─────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION LAYER — durable, crash-safe                       │
│  Temporal (self-hosted via Docker; Temporal Cloud later)         │
│  Owns: per-user scheduling, retry policies, the 24h approval     │
│        timer, approve/reject signals, idempotent checkout        │
└─────────────────────────────────────────────────────────────────┘
         ▲ reads/writes
┌─────────────────────────────────────────────────────────────────┐
│  DATA LAYER                                                      │
│  Supabase Postgres (asyncpg, direct connection)                  │
│  18 tables: users, inventory, consumption (profile + events),    │
│  carts, cart_items, approvals, purchases, price_snapshots,       │
│  schedules, import_proposals, conversation_state, amazon_profiles,│
│  amazon_auth_challenges, pending_replenishments,                 │
│  prediction_snapshots, llm_usage                                 │
└─────────────────────────────────────────────────────────────────┘
         ▲ observes
┌─────────────────────────────────────────────────────────────────┐
│  OBSERVABILITY LAYER                                             │
│  Langfuse + Postgres ledgers — traces, per-run cost (llm_usage),  │
│  prediction precision/recall, scraper-health probe → money-live   │
│  gate (gating.py); see EVALS.md                                  │
└─────────────────────────────────────────────────────────────────┘
         ▲ all chat + notifications
┌─────────────────────────────────────────────────────────────────┐
│  INTERFACE LAYER                                                 │
│  Telegram bot — sole channel: briefings, approvals (inline       │
│  ✅/❌ buttons), free-text chat, 2FA-code relay                   │
└─────────────────────────────────────────────────────────────────┘
```

## Full workflow diagram (GroceryRunWorkflow)

```
Temporal Schedule (per user, configurable cron)  — or manual / "buy what I'm low on"
    │
    ▼
GroceryRunWorkflow.run(user_id, trigger)
    │
    ├─► apply_estimated_depletion_activity
    │       decay each item's estimated qty by (rate × days elapsed) so prediction
    │       works off a fresh estimate; the user's confirmed actual_qty is untouched
    │
    ├─► load_user_data            inventory, consumption profiles, events, prefs
    │     └─ guardrails (scheduled runs only): skip if a cart is already
    │        pending_approval, or if another run fired within run_cooldown_minutes
    │
    ├─► select_run_candidates_activity
    │       must-buy = rule-based predictor (days_left = qty / effective_daily_rate,
    │         flagged when ≤ lead_time + buffer; rate blends declared habit with
    │         observed events, capped weight)
    │       fillers  = soonest-due "medium" items, to round the order up to free
    │         shipping. No must-buy → notify "well stocked" and stop (never runs
    │         on fillers alone).
    │       also snapshots the full prediction (prediction_snapshots) so the eval
    │         can score real precision/recall against later purchases
    │
    ├─► lookup_amazon_prices      Playwright drives the persistent Amazon session;
    │                             searches the grocery dept, extracts price + ASIN,
    │                             and picks the listing by brand preference (Haiku)
    │
    ├─► assemble_run_cart_activity
    │       keep every must-buy; add fillers only until the free-shipping threshold
    │       is cleared. `reason` explains any extras shown in the briefing.
    │
    ├─► build_draft_cart          writes carts + cart_items; stamps the Temporal
    │                             workflow_id on the cart so the webhook can signal back
    │
    ├─► send_approval_notification    Telegram briefing (Haiku-composed) with inline
    │                                 ✅ Approve / ❌ Reject buttons
    │       update_cart_status → 'pending_approval'
    │
    │       workflow.wait_condition(decision != None, timeout=24h)   ← durable timer
    │           ◄──── Telegram button tap (callback_data approve:{wf} / reject:{wf})
    │                   → POST /telegram  (FastAPI webhook)
    │                   → Temporal handle.signal("approve" | "reject")
    │                   └─► resumes the workflow from the durable timer
    │
    ├─► [if approved]   update_cart_status → 'approved'
    │       prepare_checkout_activity   (NO_RETRY)
    │           Playwright clears the existing cart (so stale cross-run items never
    │           accumulate), adds each ASIN, marks the cart 'checkout_ready', and
    │           returns the account cart URL — the user taps "Place order" themselves.
    │           We NEVER complete the purchase.
    │           idempotency_key = 'purchase-{cart_id}' (UNIQUE) guards re-staging.
    │
    └─► run_evals_activity
            prediction precision/recall from prediction_snapshots → Langfuse scores;
            cost alert if the run's summed llm_usage cost exceeds the threshold
```

`QuickBuyWorkflow` (ad-hoc "buy X now") is the same shape minus prediction (items are
given) with a tighter **6h** approval timeout. `ImportHistoryWorkflow` runs
`ensure_amazon_login_activity` → scrape → Sonnet synthesis → staged proposal. See
[SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md) §4.

## Component inventory

| File | Responsibility |
|---|---|
| `workflows/grocery_run.py` | Scheduled/manual restock workflow; approve/reject signals; 24h approval timer |
| `workflows/quick_buy.py` | Ad-hoc "buy X now" workflow (6h approval timer) |
| `workflows/import_history.py` | Order-history import: ensure-login → scrape → synthesize → stage proposal |
| `workflows/activities.py` | All 20 activities — every I/O/side-effect: DB, Playwright, Telegram, evals |
| `workflows/worker.py` | Temporal worker — registers workflows + activities, runs forever |
| `automation/amazon.py` | Playwright Amazon automation: session, price search, add-to-cart (clears cart before staging), order-history scrape |
| `automation/amazon_auth.py` | Self-healing sign-in: verified credential fill (state machine) + interactive fallback |
| `automation/resilience.py` | Self-healing, observable element resolution: strategy descriptors, deterministic `first_matching`, instrumented `resolve` (LLM a11y/vision repair on a 0-match, with a selector cache) + per-run health report |
| `automation/network.py` | Network-level hardening: `block_heavy_resources` (read paths only), `confirm_add_to_cart` (cart-mutation XHR as success signal), `JsonResponseCollector` |
| `agents/assistant.py` | Intent parsing (fresh request / briefing reply), briefing composition (Haiku) |
| `agents/onboarding.py` | Conversational intake: seeds inventory + consumption habits |
| `agents/order_history.py` | Sonnet synthesis of scraped orders + Haiku import-review edit loop |
| `predictor.py` / `stock.py` / `depletion.py` | Pure-Python prediction, stock bucketing, estimated depletion (unit-tested) |
| `runlist.py` / `products.py` | Candidate/filler selection for a run; product-name normalization |
| `tools/*.py` | Async data-access modules (inventory, consumption, conversation, imports, schedule, auth, predictions, reset) shared by activities, agents, and MCP |
| `llm.py` | Single entry point for every Anthropic call: process-wide shared client, token/cost telemetry → the `llm_usage` ledger, prompt-cache helpers, and `run_scope` run attribution |
| `mcp_server.py` | FastMCP server — exposes the same tools for local dev with Claude Code |
| `notifications.py` | Telegram helper: `send_telegram_message`, `send_briefing`, `send_checkout_link`, scraper-health alert |
| `webhook.py` | FastAPI server — `/telegram` (messages + button callbacks → Temporal signals), `/health` |
| `evals.py` | Prediction precision/recall from `prediction_snapshots`; per-run cost from the `llm_usage` ledger + cost alert |
| `monitoring.py` | Synthetic scraper-health probe (`check_scraper_health`) — catches silent Amazon-selector breakage; precondition of the money-live gate |
| `gating.py` | Money-live readiness gate (`money_live_ready`) — `checkout_verified` is a hard stop today (see EVALS.md) |
| `tracing.py` | Langfuse context manager (no-ops gracefully if unconfigured) |
| `config.py` | Pydantic-settings; all config from `.env` |
| `db.py` | asyncpg connection pool singleton |
| `cli.py` | Click CLI: onboard, worker, run, ask, webhook, schedule, mcp, evals, scraper-health, gate |

## Key design decisions

### Why Temporal (not LangGraph, not the SQLite queue from agent-core)

This agent has three hard requirements that need a real durable-execution engine:

1. **Long human-approval wait** (up to 24 hours): the workflow must survive restarts and crashes while waiting for the user's phone tap. Temporal's durable timers handle this natively.
2. **Exactly-once money movement**: Temporal's activity retry semantics + the app-level `idempotency_key` guard together ensure a purchase is never executed twice, even if the worker crashes mid-activity.
3. **Per-user scheduling**: Temporal Schedules own the cron per user; schedule IDs are stored in `schedules.temporal_schedule_id` for lifecycle management.

### Why Claude Agent SDK / Anthropic SDK (not LangGraph as the runtime)

Claude Agent SDK and LangGraph compete on the same layer (driving the model loop). Stacking them adds zero value. Anthropic SDK is used directly for:
- The onboarding conversational intake (tool-use loop)
- Any future agent that needs Claude as the reasoning core inside a Temporal activity

LangGraph is not used; Temporal owns orchestration.

### Why browser automation for Amazon

Amazon has no public consumer ordering API, so Playwright drives a persistent
authenticated browser profile (saved to `.amazon-session/`). When that session
expires the agent **re-authenticates itself** (`automation/amazon_auth.py`): with
`AMAZON_EMAIL`/`AMAZON_PASSWORD` set it fills the sign-in form unattended — a verified,
state-machine fill that survives Amazon's churning multi-step auth UI — and relays any
2FA code to you over Telegram; without credentials it opens a window for a one-time
manual sign-in. The agent **stages** the cart and hands back a checkout link; it never
completes a purchase on your behalf (see SYSTEM_REFERENCE §10).

### Why asyncpg (not Supabase JS client or REST API)

asyncpg gives direct Postgres access from Python — no REST round-trips, transactional upserts (critical for the inventory + consumption event write), and significantly lower latency in the Temporal activity context. The Supabase anon key is available for client-side apps if needed; backend always uses asyncpg.

### Why Langfuse (not self-rolled tracing)

Langfuse is free at the Hobby tier (50k units/mo), open-source, and self-hostable. The tracing layer (`tracing.py`) no-ops gracefully when keys are absent — the agent runs without it, Langfuse just makes costs and accuracy visible.

## Cost model

One morning run ≈ a few Sonnet 4.6 calls (reasoning + cart building) + Haiku 4.5 calls (mechanical matching) + browser-automation screenshot/DOM tokens:

| Component | Cost per run | Monthly (1 user, daily) |
|---|---|---|
| LLM tokens (Sonnet + caching) | ~$0.15–0.40 | ~$5–12 |
| Temporal (self-hosted) | $0 | $0 |
| Langfuse (Hobby) | $0 | $0 |
| Supabase (free tier) | $0 | $0 |
| Hosting (Fly.io small VM) | — | ~$5–20 |
| **Total** | | **~$10–32/mo** |

At product scale: tokens scale linearly (~$5–12/user/mo); Temporal Cloud starts at ~$100/mo + $50/M actions; Langfuse self-host ~$100–400/mo infra.
