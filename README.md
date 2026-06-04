# grocery-buddy

24/7 autonomous grocery agent — tracks your pantry, predicts what's running low, builds an Amazon cart, and purchases automatically (under your $ cap) or sends a push notification for approval.

## Stack

| Layer | Tool |
|---|---|
| Runtime / AI loop | Claude (Anthropic SDK, Sonnet 4.6 / Haiku 4.5) |
| Orchestration / durability | Temporal (self-hosted) |
| Database | Supabase Postgres |
| Observability | Langfuse |
| Amazon automation | Playwright (persistent session) |
| Push notifications | ntfy.sh |
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
# Paste each migrations/*.sql file (001 → 008, in order) into the Supabase SQL editor

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
| "run my briefing at 9am daily" | Change the schedule |
| "yes" / "no" / "buy milk and eggs" | Reply to a pending list (approve, skip, or build a fresh cart) |

These four commands also autocomplete in Telegram's "/" menu (registered via `setMyCommands` on startup).

**Onboarding from Amazon:** because the agent has your Amazon session, it can read your recent orders and draft a pantry for you — brands, quantities, and how often you reorder — synthesized with Sonnet. You review and edit it conversationally ("drop the donuts", "remove the unhealthy snacks, I'm on a diet") and nothing is saved until you confirm.

**Estimated vs. actual stock:** each scheduled checkup assumes you kept consuming at your usual rate and decays the *estimate*. When you correct an item, the estimate snaps back to what you actually have — one-off corrections never distort the long-run consumption rate.

## Architecture

```
cron/schedule  (or manual / "buy what I'm low on")
    └─► Temporal GroceryRunWorkflow
            ├── load_user_data (Postgres)
            ├── predict_low_items_activity (rule-based predictor)
            ├── lookup_amazon_prices (Playwright)
            ├── build_draft_cart (Postgres)
            ├── send_approval_notification (Telegram briefing)   ← always approval-gated
            ├── wait_condition (durable 24h timer)  ◄── Telegram Approve/Reject
            │                                            → webhook → Temporal signal
            └── [if approved] prepare_checkout_activity
                    (Playwright stages the Amazon cart, returns a checkout link —
                     the user taps "Place order"; we never buy on their behalf)
```

**Full map of every model, agentic loop, workflow, tool, and decision tree:
[docs/SYSTEM_REFERENCE.md](docs/SYSTEM_REFERENCE.md).**

## Amazon automation note

Amazon has no public consumer ordering API. This agent uses Playwright with a persistent authenticated browser profile. Run `scripts/setup_amazon_session.py` once to log in interactively; subsequent runs are headless. The agent requires your explicit approval for all purchases until the automation is proven stable for your account.

## Cost estimate (1–2 users)

- LLM tokens: ~$0.15–0.40 / run (Sonnet + caching)
- Temporal: free (self-hosted)
- Langfuse: free (Hobby, 50k units/mo)
- Hosting: ~$5–20/mo (Fly.io)
- **Total: ~$20–45/month**
