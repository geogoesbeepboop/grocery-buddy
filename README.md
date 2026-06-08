# grocery-buddy

24/7 autonomous grocery agent — tracks your pantry, predicts what's running low, builds and prices an Amazon cart, and sends you a Telegram briefing to approve. On approval it stages the cart and hands back a checkout link; it never places the order itself. Once you confirm you placed the order, it tracks the delivery as **in-transit** stock (so it won't re-suggest what you just bought) and tops up your pantry when it arrives.

## Stack

| Layer | Tool |
|---|---|
| Runtime / AI loop | Claude (Anthropic SDK, Sonnet 4.6 / Haiku 4.5) |
| Orchestration / durability | Temporal (self-hosted) |
| Database | Supabase Postgres (asyncpg) |
| Observability | Langfuse |
| Amazon automation | Playwright (persistent session, self-healing login) |
| Chat & notifications | Telegram bot (sole channel) |
| Hosting | Fly.io / Docker |

## Quick start

```bash
# 1. Clone and install
cd grocery-buddy
uv sync

# 2. Copy and fill in secrets
cp .env.example .env

# 3. Start Temporal locally
docker compose up -d

# 4. Apply DB migrations (after Supabase project is created)
# Paste each migrations/*.sql file (001 → 009, in order) into the Supabase SQL editor

# 5. Save your Amazon session (run once, interactive)
# Optional: set AMAZON_EMAIL / AMAZON_PASSWORD in .env first and the agent will
# re-login on its own when the session expires (otherwise it opens a window for you).
uv run python scripts/setup_amazon_session.py

# 6. Onboard a user
uv run grocery-buddy onboard --user-id <your-user-uuid>

# 7. Start the worker (in another terminal)
uv run grocery-buddy worker

# 8. Start the webhook server (in another terminal, exposed via ngrok)
uv run grocery-buddy webhook
# ngrok http 8080  → copy the URL to WEBHOOK_BASE_URL in .env

# 9. Trigger a test run
uv run grocery-buddy run --user-id <your-user-uuid>

# 10. Set up the daily schedule
uv run grocery-buddy schedule --user-id <your-user-uuid> --cron "0 8 * * *"
```

## CLI commands

| Command | Description |
|---|---|
| `grocery-buddy onboard --user-id <id>` | Conversational intake (seeds inventory + habits) |
| `grocery-buddy worker` | Start the Temporal worker |
| `grocery-buddy run --user-id <id>` | Trigger one grocery run |
| `grocery-buddy webhook [--port 8080]` | Start approval webhook server |
| `grocery-buddy schedule --user-id <id> --cron "0 8 * * *"` | Set daily schedule |
| `grocery-buddy mcp` | Start MCP server (for Claude Code local dev) |

## Telegram chat

Day-to-day, you talk to the bot in plain language. A few things it understands:

| You say | What happens |
|---|---|
| `/import` | Bootstrap your pantry from your Amazon order history (review before it saves) |
| `/start` | (Re)run the pantry interview |
| `/status` | Show your pantry, any pending list, and your schedule |
| `/help` | What the bot can do |
| "grab some coffee" | Ad-hoc, approval-gated order |
| "buy what I'm low on" | Restock everything that's running low |
| "we still have plenty of eggs, and the milk's gone" | Corrects on-hand quantities on the fly (one or many items) |
| "ordered" / tap **✅ I placed the order** | Confirm you checked out → items tracked as on-the-way, pantry tops up on arrival |
| "the milk never came" | Cancel an in-transit order so it counts as needed again |
| "run my briefing at 9am daily" | Change the schedule |
| "yes" / "no" / "buy milk and eggs" | Reply to a pending list (approve, skip, or build a fresh cart) |

These four commands also autocomplete in Telegram's "/" menu (registered via `setMyCommands` on startup).

**Onboarding from Amazon:** because the agent has your Amazon session, it can read your recent orders and draft a pantry for you — brands, quantities, and how often you reorder — synthesized with Sonnet. You review and edit it conversationally ("drop the donuts", "remove the unhealthy snacks, I'm on a diet") and nothing is saved until you confirm.

**Estimated vs. actual stock:** each scheduled checkup assumes you kept consuming at your usual rate and decays the *estimate*. When you correct an item, the estimate snaps back to what you actually have — one-off corrections never distort the long-run consumption rate.

**On-hand + on-the-way:** when you confirm you placed an order, its items become *in-transit* replenishments with an estimated arrival (`ordered_at + your lead time`). Prediction counts them as covered stock, so the next run won't re-suggest eggs you just bought; when the ETA passes, a reconcile step (plus a durable per-order delivery timer) tops up your pantry and nudges you. See [docs/FEATURES_AND_ROADMAP.md](docs/FEATURES_AND_ROADMAP.md) for what's next and [docs/PROCUREMENT_CONVERGENCE.md](docs/PROCUREMENT_CONVERGENCE.md) for how this connects to the procurement-agent.

## Architecture

```
cron/schedule  (or manual / "buy what I'm low on")
    └─► Temporal GroceryRunWorkflow
            ├── reconcile_arrivals_activity (land in-transit orders past their ETA → restock)
            ├── apply_estimated_depletion_activity (decay estimates)
            ├── load_user_data (Postgres, incl. in-transit "incoming") + guardrails
            ├── select_run_candidates_activity (predictor → must-buy + free-ship fillers;
            │                                    in-transit items count as covered stock)
            ├── lookup_amazon_prices (Playwright, brand-aware)
            ├── assemble_run_cart_activity (must-buys + just enough fillers)
            ├── build_draft_cart (Postgres)
            ├── send_approval_notification (Telegram briefing)   ← always approval-gated
            ├── wait_condition (durable 24h timer)  ◄── Telegram Approve/Reject
            │                                            → /telegram webhook → Temporal signal
            ├── [if approved] prepare_checkout_activity
            │       (Playwright stages the Amazon cart, returns a checkout link —
            │        the user taps "Place order"; we never buy on their behalf)
            └── _await_purchase_confirmation  ◄── "I placed the order"
                    (record items as in-transit → durable sleep until ETA →
                     reconcile_arrivals tops up the pantry on delivery)
```

**Full map of every model, agentic loop, workflow, tool, and decision tree:
[docs/SYSTEM_REFERENCE.md](docs/SYSTEM_REFERENCE.md).**

## Amazon automation note

Amazon has no public consumer ordering API. This agent uses Playwright with a persistent authenticated browser profile. Run `scripts/setup_amazon_session.py` once to log in interactively; subsequent runs are headless. When the session later expires, the agent **re-logs-in on its own** — filling `AMAZON_EMAIL`/`AMAZON_PASSWORD` unattended (relaying any 2FA code to you over Telegram) or, if no credentials are set, opening a window for a one-time manual sign-in. The agent requires your explicit approval for every cart and never completes the purchase itself.

## Cost estimate (1–2 users)

- LLM tokens: ~$0.15–0.40 / run (Sonnet + caching)
- Temporal: free (self-hosted)
- Langfuse: free (Hobby, 50k units/mo)
- Hosting: ~$5–20/mo (Fly.io)
- **Total: ~$20–45/month**
