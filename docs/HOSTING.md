# Hosting Strategy

## The core insight: infrastructure is shared, agents are not

The right mental model for hosting multiple agents is **one infrastructure layer, many agent workers**:

```
┌──────────────────────────────────────────────────────────────────┐
│  SHARED INFRASTRUCTURE  (run once, used by every agent)          │
│                                                                  │
│  ┌─────────────────┐   ┌────────────────┐   ┌───────────────┐   │
│  │ Temporal Server │   │    Langfuse     │   │   Postgres    │   │
│  │ (one cluster)   │   │ (one account)  │   │  (Supabase)   │   │
│  │                 │   │                │   │               │   │
│  │  namespace:     │   │  project:      │   │  schema:      │   │
│  │    grocery      │   │    grocery     │   │    grocery    │   │
│  │    dj-agent     │   │    dj-agent    │   │    dj-agent   │   │
│  │    code-mig     │   │    code-mig    │   │    code-mig   │   │
│  └─────────────────┘   └────────────────┘   └───────────────┘   │
└──────────────────────────────────────────────────────────────────┘
         ▲ connect to                ▲ connect to
┌───────────────────┐   ┌───────────────────┐   ┌───────────────────┐
│  grocery-buddy    │   │    dj-agent       │   │  code-migration   │
│  worker + webhook │   │    worker         │   │  worker           │
│                   │   │                   │   │                   │
│  task-queue:      │   │  task-queue:      │   │  task-queue:      │
│  grocery-buddy    │   │  dj-agent         │   │  code-migration   │
└───────────────────┘   └───────────────────┘   └───────────────────┘
```

**Why this works:**
- Temporal is a stateful orchestration engine. Every agent's durable workflow state lives there. One cluster handles any number of namespaces.
- Workers are stateless compute. They pull tasks from their task queue, execute them, return results. Each agent deploys its own workers independently.
- Adding a new agent = deploy its workers. No changes to shared infra.
- A workflow in `grocery-buddy` cannot accidentally execute an activity from `dj-agent` because task queues are isolated.

---

## Multi-agent and sub-agent patterns

### Pattern 1: Independent agents (current state)
Each agent runs its own isolated workflows. No coordination. This is what grocery-buddy is today.

### Pattern 2: Agent spawning sub-agents (child workflows)
A "supervisor" workflow starts child workflows for specialized tasks:

```python
@workflow.defn
class SupervisorWorkflow:
    @workflow.run
    async def run(self, goal: str) -> str:
        # Spawn a specialized child workflow
        research_result = await workflow.execute_child_workflow(
            ResearchWorkflow.run,
            goal,
            id=f"research-{workflow.info().workflow_id}",
            task_queue="research-agent",          # different task queue = different worker pool
        )
        # Spawn another child with the research result
        action_result = await workflow.execute_child_workflow(
            ActionWorkflow.run,
            research_result,
            id=f"action-{workflow.info().workflow_id}",
            task_queue="action-agent",
        )
        return action_result
```

The key: each child runs on its own task queue (its own pool of workers), but the parent coordinates durably. If any child or the parent crashes, Temporal resumes everything from exactly where it stopped.

### Pattern 3: Fan-out / fan-in (parallel sub-agents)
```python
# Launch N sub-agents in parallel, wait for all
handles = await asyncio.gather(*[
    workflow.start_child_workflow(
        SubAgentWorkflow.run,
        item,
        id=f"sub-{i}",
        task_queue="sub-agent",
    )
    for i, item in enumerate(work_items)
])
results = await asyncio.gather(*[h.result() for h in handles])
```

### Pattern 4: Signal-based coordination (agents talking to each other)
```python
# Agent A signals Agent B
client = await Client.connect(temporal_host)
handle = client.get_workflow_handle("agent-b-workflow-id")
await handle.signal("new_data_available", payload)
```

All four patterns work on the same single Temporal cluster — no additional infra needed.

---

## Recommended hosting stack (by scale)

### Stage 1: Personal / 1-5 agents (~$30-80/mo total)

```
Fly.io VM ($6/mo, 1 shared CPU, 256MB)
  └── Temporal server + Temporal UI (docker-compose style, self-hosted)

Per-agent Fly.io apps ($3-5/mo each, shared CPU, 256MB)
  └── grocery-buddy: worker + webhook server
  └── future-agent-2: worker

Supabase free tier
  └── one project per agent (or shared with schemas)

Langfuse free tier (50k units/mo)
```

This runs all personal agents with a total infra cost around $30-50/mo. Temporal self-hosted on Fly is the biggest unlock — one small VM handles hundreds of workflow executions per day trivially.

### Stage 2: Small product / 5-20 agents (~$150-400/mo)

```
Temporal Cloud Essentials ($100/mo + ~$50/M actions)
  └── no VM to manage; handles any workflow volume

Fly.io per-agent workers ($5-15/mo each)
  └── each agent scales independently

Supabase Pro ($25/mo per project, or one shared Pro)
Langfuse Team ($59/mo) or self-hosted (~$100-200/mo infra)
```

### Stage 3: Scale / 20+ agents or high-frequency workflows

```
Temporal Cloud Business or self-hosted k8s cluster
Dedicated Fly.io organization with shared Postgres
Langfuse self-hosted (ClickHouse + Postgres)
```

---

## Concrete implementation: shared Temporal on Fly.io

The `infra/temporal/` directory contains the setup for a production Temporal server deployed to Fly.io that all your agents share.

### Files

- `infra/temporal/fly.toml` — Fly.io config for the Temporal server
- `infra/temporal/docker-compose.yml` — same as project root but intended as a shared local setup

### Deploy shared Temporal to Fly.io

```bash
cd infra/temporal

# Create a Postgres database on Fly.io for Temporal's state
flyctl postgres create --name temporal-db --region iad

# Launch the Temporal Fly app
flyctl launch --name temporal-cluster --region iad --no-deploy

# Set required config
flyctl secrets set \
  DB=postgres12 \
  POSTGRES_USER=<from-fly-postgres-output> \
  POSTGRES_PWD=<from-fly-postgres-output> \
  POSTGRES_SEEDS=temporal-db.internal \
  --app temporal-cluster

flyctl deploy --app temporal-cluster
```

### Connect any agent to the shared Temporal

In each agent's `.env`:
```
TEMPORAL_HOST=temporal-cluster.internal:7233    # internal Fly network
# or from outside Fly:
TEMPORAL_HOST=temporal-cluster.fly.dev:7233
```

---

## Grocery-buddy specific deployment

### What needs to run 24/7

| Process | Why 24/7 | Where |
|---|---|---|
| Temporal worker | polls for workflow tasks; must be up when scheduled runs fire | Fly.io `grocery-buddy` app, `worker` process |
| Webhook server | must be reachable 24/7 to receive ntfy approval taps | Fly.io `grocery-buddy` app, `webhook` process |
| Temporal server | holds all durable workflow state | Fly.io `temporal-cluster` app (shared) |

The Playwright browser automation runs **inside** the worker process on-demand — no always-running browser.

### Grocery-buddy Fly.io deploy

```bash
cd ~/dev/grocery-buddy

# Create volume for Amazon session (persists across deploys)
flyctl volumes create amazon_session --size 1 --region iad

# Deploy
flyctl launch --name grocery-buddy --region iad --no-deploy
flyctl secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  DATABASE_URL=postgresql://... \
  LANGFUSE_PUBLIC_KEY=pk-lf-... \
  LANGFUSE_SECRET_KEY=sk-lf-... \
  NTFY_TOPIC=grocery-buddy-<suffix> \
  TEMPORAL_HOST=temporal-cluster.internal:7233 \
  WEBHOOK_BASE_URL=https://grocery-buddy.fly.dev

# Upload Amazon session (run scripts/setup_amazon_session.py locally first)
flyctl sftp shell -a grocery-buddy
# Inside: put -r .amazon-session /app/.amazon-session

flyctl deploy
```

After deploy, confirm at `https://grocery-buddy.fly.dev/health`.

### Running Temporal locally for dev but production on Fly

The `TEMPORAL_HOST` env var controls which Temporal the worker connects to:
```bash
# Local dev
TEMPORAL_HOST=localhost:7233

# Production
TEMPORAL_HOST=temporal-cluster.internal:7233   # from within Fly
TEMPORAL_HOST=temporal-cluster.fly.dev:7233    # from outside Fly (slower)
```

All other config stays the same — your `.env` points to localhost for local dev.

---

## What "24/7" actually means operationally

### Crash recovery
- **Temporal server:** if it crashes, restart it. In-progress workflows are durable in Postgres — they resume exactly where they left off when Temporal comes back.
- **Worker:** if it crashes, Temporal detects the heartbeat loss and re-schedules the activity to any available worker. On Fly.io, the process auto-restarts.
- **Webhook server:** stateless; Fly.io restarts it in seconds. ntfy retries unacknowledged calls.

### What can actually go wrong 24/7
| Failure | Impact | Recovery |
|---|---|---|
| Worker crashes mid-activity | Activity retried by Temporal (unless `maximum_attempts=1`) | Automatic |
| Webhook server down during approval tap | ntfy shows delivery failure; user re-taps | User action |
| Amazon session expires | Price lookup fails; worker logs warning | Re-run `setup_amazon_session.py` |
| Temporal server down | Scheduled runs queue up; fire when Temporal recovers | Automatic |
| Supabase maintenance | Activities fail; retry policy handles it | Automatic |

### Monitoring checklist
- Temporal UI: check for workflows stuck in "Running" for >25h (missed approval or hung activity)
- Langfuse: watch cost-per-run trend; set alert in `evals.py` `COST_ALERT_THRESHOLD_USD`
- Fly.io dashboard: confirm worker + webhook processes are both `running`
- ntfy: test push delivery weekly (trigger a manual run above the cap)

---

## Why Fly.io (and why ntfy alone isn't enough)

These are two different things people conflate:

- **ntfy (the phone app)** is a *notification receiver*. It shows the morning
  briefing and the Approve/Reject buttons. It is the **output/UI**, and yes — it
  is enough for receiving pushes and tapping approve. You don't need anything
  else for that half.
- **Fly.io (or any always-on host)** is where the *agent itself* runs — the
  Temporal **worker** and the **webhook server**. These are long-running
  programs that must be awake 24/7:
    - The **worker** is what fires the 8am scheduled run, prices items, drives
      Playwright, and waits (durably) for your approval. If it's only running on
      your laptop, the agent stops the moment your laptop sleeps.
    - The **webhook server** is the thing your phone's Approve tap actually calls
      (`/approve/{workflow_id}`). ntfy → HTTP POST → webhook → signals Temporal.
      If nothing is listening at `WEBHOOK_BASE_URL`, the tap goes nowhere.

So: **ntfy = your phone receiving and answering. Fly.io = the brain that's always
on.** A "Fly app" is just a deployed container — `flyctl deploy` packages this
repo's Docker image and runs the `worker` and `webhook` processes on a small
always-on VM. Fly is not special; it's just a cheap, simple always-on host. Any
of these would also work: a $5 VPS (DigitalOcean/Hetzner), a Raspberry Pi at
home, a home server, or Railway/Render. The only hard requirement is **a machine
that never sleeps and is reachable from the internet** (for the webhook). If you
already have an always-on box, you can skip Fly entirely.

> For local dev you run the worker + webhook on your laptop and expose the
> webhook with `ngrok` (see `make dev`). That's fine for testing but not 24/7.

---

## Two-way conversational channel (Telegram — built)

The agent talks to you through **ntfy push + tappable buttons** and now also
through a **Telegram bot** for free-text, two-way chat. You can also drive it
locally with the CLI: `grocery-buddy ask "I need eggs early"`.

### How it fits together

```
You (Telegram)  ──text──▶  POST /telegram (webhook.py)
                              │  parse_request()  (agents/assistant.py, Haiku)
                              ▼
                         QuickBuyWorkflow ──prices brand-aware──▶ draft cart
                              │
                              ▼  approval prompt (ntfy + Telegram inline buttons)
You tap ✅/❌  ──callback──▶  /telegram (or /approve,/reject) ──signal──▶ workflow
                              ▼
                         execute purchase
```

- Free text like *"I need eggs earlier than expected"* → `parse_request()` →
  starts `QuickBuyWorkflow` for just those items, always approval-gated.
- Approval prompts are sent to **both** ntfy and Telegram (inline ✅/❌ buttons).
  Telegram button taps carry `callback_data` like `approve:{workflow_id}` and are
  signaled back to Temporal by the same `/telegram` route.
- Replies like *"actually buy me the oat milk too"* route through the same
  `parse_request()`.

### Setup

1. Create a bot with **@BotFather**, copy the token.
2. DM the bot once, then read your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Set in `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROCERY_BUDDY_USER_ID`.
4. Register the webhook (once):
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=$WEBHOOK_BASE_URL/telegram"
   ```
   `WEBHOOK_BASE_URL` must be the public URL of the webhook server (ngrok for
   local dev; the Fly app URL in production).

Only messages from `TELEGRAM_CHAT_ID` are acted on; anything else is ignored.

### Still open / future
- **Morning briefing over Telegram:** the daily run currently notifies via ntfy.
  Sending the briefing as a Telegram message (so the "actually buy me this" reply
  is in the same thread) is a small follow-up — wire the daily-run completion
  notification to `send_telegram_message`.
- **Multi-user:** inbound chat is single-user today (`GROCERY_BUDDY_USER_ID`). A
  chat-id → user mapping table would generalize it.
