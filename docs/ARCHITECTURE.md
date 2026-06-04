# Architecture

> This file covers **why** the stack is shaped the way it is. For the exhaustive
> **what** — every model, agentic loop, workflow, tool, data model, and decision
> tree — see **[SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md)** (kept current).
>
> Two facts that supersede older descriptions below: notifications run over
> **Telegram** (not ntfy.sh), and the agent **never places an order** — every run
> ends at an approval gate, then stages an Amazon cart and hands back a checkout
> link the user completes themselves (there is no auto-purchase path today).

## What grocery-buddy is

A 24/7 autonomous agent that tracks your pantry, predicts what's running low, builds and prices a grocery cart, and executes the purchase — either automatically (under a configurable spend cap) or after you approve it on your phone.

## Layer map

```
┌─────────────────────────────────────────────────────────────────┐
│  RUNTIME LAYER — how one agent turn executes                    │
│  Anthropic SDK (claude-sonnet-4-6 / claude-haiku-4-5)           │
│  Tool use: inventory CRUD, consumption events, price lookup      │
└─────────────────────────────────────────────────────────────────┘
         ▲ called from activities
┌─────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION LAYER — durable, crash-safe, exactly-once        │
│  Temporal (self-hosted via Docker; Temporal Cloud later)         │
│  Owns: scheduling, retry policies, the approval-gate timer,     │
│        approve/reject signals, idempotent purchase execution     │
└─────────────────────────────────────────────────────────────────┘
         ▲ reads/writes
┌─────────────────────────────────────────────────────────────────┐
│  DATA LAYER                                                     │
│  Supabase Postgres (asyncpg for direct connection)              │
│  13 tables: users, inventory, consumption habits, carts,        │
│             purchases, approvals, price snapshots               │
└─────────────────────────────────────────────────────────────────┘
         ▲ observes
┌─────────────────────────────────────────────────────────────────┐
│  OBSERVABILITY LAYER                                            │
│  Langfuse — traces, per-run cost, prediction accuracy evals     │
└─────────────────────────────────────────────────────────────────┘
```

## Full workflow diagram

```
Temporal Schedule (per user, configurable cron)
    │
    ▼
GroceryRunWorkflow.run(user_id)
    │
    ├─► load_user_data            reads inventory, consumption profiles,
    │                             events, preferences from Postgres
    │
    ├─► predict_low_items_activity
    │       rule-based predictor: days_left = qty / effective_daily_rate
    │       effective_daily_rate blends declared habit (prior) with
    │       observed consumption events (posterior, max 80% weight)
    │       returns items where days_left ≤ lead_time + buffer
    │
    ├─► lookup_amazon_prices      Playwright drives a persistent
    │                             authenticated Amazon browser session;
    │                             searches grocery department, extracts
    │                             price + ASIN for each low item
    │
    ├─► [optional] lookup_kroger_prices   Kroger public Products API;
    │                                     surfaces cheaper alternative
    │
    ├─► build_draft_cart          writes carts + cart_items to Postgres;
    │                             stores Temporal workflow_id on the cart
    │                             so the webhook can signal back
    │
    ├─► [if total_usd > auto_purchase_cap]
    │       send_approval_notification
    │           POST to ntfy.sh topic → phone push with
    │           "✅ Approve" and "❌ Reject" action buttons
    │
    │       update_cart_status → 'pending_approval'
    │
    │       workflow.wait_condition(decision != None, timeout=24h)
    │           ◄──── ntfy button tap
    │                   → POST /approve/{workflow_id} or /reject/{workflow_id}
    │                   → FastAPI webhook server
    │                   → Temporal client.get_workflow_handle.signal("approve")
    │                   └─► resumes workflow from durable timer
    │
    ├─► [if approved or auto-purchase]
    │       execute_purchase_activity
    │           idempotency guard: check purchases table for this key
    │           Playwright: add each ASIN to cart, proceed to checkout
    │           records purchase with idempotency_key (UNIQUE constraint)
    │           updates cart status → 'purchased'
    │           sends purchase confirmation push
    │
    └─► run_evals_activity
            precision/recall vs. purchase history → Langfuse scores
            cost alert if run_cost_usd > $1.00 threshold
```

## Component inventory

| File | Responsibility |
|---|---|
| `workflows/grocery_run.py` | Temporal workflow definition; signal handlers (approve/reject); approval-gate timer |
| `workflows/activities.py` | All I/O and side-effects: DB reads/writes, Playwright calls, ntfy pushes, evals |
| `workflows/worker.py` | Temporal worker — registers workflows + activities, runs forever |
| `automation/amazon.py` | Playwright Amazon automation: session management, price search, add-to-cart, checkout |
| `agents/onboarding.py` | Anthropic SDK conversational agent: seeds inventory + consumption habits interactively |
| `predictor.py` | Pure-Python rule-based predictor (no external deps, fully unit-tested) |
| `tools/inventory.py` | Inventory CRUD (asyncpg); shared by MCP server and workflow activities |
| `tools/consumption.py` | Consumption profile + events CRUD; shared by MCP server and workflow activities |
| `mcp_server.py` | FastMCP server — exposes the same tools for interactive local development with Claude Code |
| `notifications.py` | ntfy.sh push helper (approval, confirmation, error) |
| `webhook.py` | FastAPI server — converts ntfy button taps into Temporal signals |
| `evals.py` | Prediction precision/recall against purchase history; cost alert |
| `tracing.py` | Langfuse context manager (no-ops gracefully if unconfigured) |
| `config.py` | Pydantic-settings; all config from `.env` |
| `db.py` | asyncpg connection pool singleton |
| `cli.py` | Click CLI: onboard, worker, run, webhook, schedule, evals, mcp |

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

Amazon has no public consumer ordering API. PA-API (product advertising) was deprecated in May 2026. Playwright drives a persistent authenticated browser profile (saved to `.amazon-session/`). The agent never completes a purchase without either your explicit approval or the auto-purchase cap being under your configured limit.

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
