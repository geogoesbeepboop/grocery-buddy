# Operations Guide

## Running locally

Three processes need to be running for full functionality:

```bash
# Terminal 1 — Temporal server
docker compose up

# Terminal 2 — Temporal worker (processes workflows)
uv run grocery-buddy worker

# Terminal 3 — Webhook server (receives Telegram messages + approve/reject taps)
uv run grocery-buddy webhook --port 8080

# Terminal 4 (optional) — ngrok for local webhook exposure
ngrok http 8080
```

Use `make dev` to get a checklist of what to start.

---

## Common operations

### Trigger a grocery run manually
```bash
uv run grocery-buddy run --user-id <uuid>
```

### Update your pantry after shopping
Use the MCP server for interactive updates from Claude Code:
```bash
uv run grocery-buddy mcp
# Then ask Claude: "Set eggs to 18, and milk to 1 gallon"
```

Or update directly via SQL:
```sql
UPDATE inventory_items
SET qty = 18, updated_at = NOW()
WHERE user_id = '<uuid>' AND product = 'Eggs';
```

### Tune how early it flags low items
```sql
-- Larger lead_time/buffer = the agent suggests restocking sooner.
UPDATE preferences SET lead_time_days = 3, buffer_days = 1 WHERE user_id = '<uuid>';
```
(`auto_purchase_cap_usd` exists but is not enforced — every cart requires approval.)

### Change the daily schedule
```bash
uv run grocery-buddy schedule --user-id <uuid> --cron "0 13 * * *"
```

### Run evals manually
```bash
uv run grocery-buddy evals --user-id <uuid>
```

### View recent carts
```sql
SELECT c.id, c.status, c.total_usd, c.created_at,
       COUNT(ci.id) as items
FROM carts c
LEFT JOIN cart_items ci ON ci.cart_id = c.id
WHERE c.user_id = '<uuid>'
GROUP BY c.id
ORDER BY c.created_at DESC
LIMIT 10;
```

---

## Approval flow

Every cart goes through approval (there is no auto-buy path):

1. You receive a **Telegram briefing** — itemized cart, total, and inline **✅ Approve** / **❌ Reject** buttons
2. Tap one → Telegram delivers the callback to `POST /telegram` on your webhook server (`callback_data` is `approve:{workflow_id}` / `reject:{workflow_id}`)
3. The webhook signals Temporal (`handle.signal("approve" | "reject")`) → the durable workflow resumes from its 24-hour wait (6h for an ad-hoc QuickBuy)
4. If approved, `prepare_checkout_activity` stages the items in your Amazon cart, marks the cart `checkout_ready`, and sends you a **checkout link** — you tap "Place order" on Amazon yourself
5. You can also just reply in plain language ("yes", "no", "drop the donuts", "we still have eggs") instead of tapping

If you don't respond within the window, the workflow marks the cart `expired` and nothing is staged.

---

## Monitoring

### Temporal UI
[http://localhost:8088](http://localhost:8088) — view all workflows, their status, history, and signals.

Key things to watch:
- **Running** workflows → active grocery runs waiting for approval
- **Failed** workflows → check the event history for the error
- **Completed** workflows → successful runs

### Langfuse (if configured)
[cloud.langfuse.com](https://cloud.langfuse.com) — traces for every run with:
- Per-run token cost breakdown
- Model call latency
- `prediction_precision` and `prediction_recall` scores over time

### Database health check
```sql
-- Carts in last 7 days by status
SELECT status, COUNT(*), AVG(total_usd::float)
FROM carts
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY status;

-- Failed purchases (need investigation)
SELECT p.idempotency_key, p.error, p.created_at
FROM purchases p
WHERE p.status = 'failed'
ORDER BY p.created_at DESC;
```

---

## Deploying to Fly.io

### First-time setup

```bash
# Install flyctl
brew install flyctl
flyctl auth login

# Launch (uses fly.toml in repo root)
flyctl launch --no-deploy

# Set secrets (never commit these)
flyctl secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  DATABASE_URL=postgresql://... \
  LANGFUSE_PUBLIC_KEY=pk-lf-... \
  LANGFUSE_SECRET_KEY=sk-lf-... \
  TELEGRAM_BOT_TOKEN=<from-botfather> \
  TELEGRAM_CHAT_ID=<your-chat-id> \
  GROCERY_BUDDY_USER_ID=<your-user-uuid> \
  WEBHOOK_BASE_URL=https://<your-app>.fly.dev
```
After deploy, re-register the Telegram webhook against the production URL:
`curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://<your-app>.fly.dev/telegram"`

### Amazon session on Fly.io

The `.amazon-session/` directory needs to be a persistent volume so the login session survives deploys:

```bash
flyctl volumes create amazon_session --size 1 --region iad
```

The `fly.toml` mounts this volume at `/app/.amazon-session`.

**Initial session setup on Fly.io:**
You can't run the interactive session setup on a server. Options:
1. Run `scripts/setup_amazon_session.py` locally → upload `.amazon-session/` to the volume via `flyctl sftp`
2. Or SSH into the machine and run it with `--headful` (requires display — complex)

Recommended: run locally first, then:
```bash
flyctl sftp shell
# Inside sftp shell:
put -r .amazon-session /app/.amazon-session
```

### Deploy

```bash
flyctl deploy
```

The Fly app runs the **worker** by default (`CMD` in Dockerfile). The **webhook server** runs as a separate process via the `[processes]` section in `fly.toml`.

After deploy, your webhook URL is `https://<your-app>.fly.dev` — update `WEBHOOK_BASE_URL` secret.

---

## Temporal in production

For a single-user/personal setup, self-hosting Temporal on a cheap Fly.io machine is sufficient:

```bash
# Create a separate Fly app for Temporal
flyctl launch --name grocery-buddy-temporal --image temporalio/auto-setup:1.24
```

Or use **Temporal Cloud** (Essentials, ~$100/mo) for zero-ops durability at scale.

Update `TEMPORAL_HOST` to point to your production Temporal endpoint.

---

## Troubleshooting

### "No Amazon price found" for an item
- Amazon's grocery section UI changes; check `automation/amazon.py` selectors
- The item name might be too specific — try a more generic product name in inventory
- If the Amazon session expired, the agent self-heals on the next `/import` or run (fills `AMAZON_EMAIL`/`AMAZON_PASSWORD`, relays 2FA over Telegram). To re-seed it by hand: `AMAZON_HEADLESS=false uv run python scripts/setup_amazon_session.py`
- 2FA relay never completed? Check the latest `amazon_auth_challenges` row for the user — a `pending` that aged out means the code wasn't replied in `AMAZON_LOGIN_WAIT_SECONDS`

### Telegram messages / button taps not arriving
- Confirm `WEBHOOK_BASE_URL` is publicly reachable (ngrok / Fly.io URL) and the server is up: `curl https://your-url/health`
- Confirm the webhook is registered: `curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"` (check `url` and `last_error_message`)
- Re-register if needed: `curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=$WEBHOOK_BASE_URL/telegram"`
- Only messages from `TELEGRAM_CHAT_ID` are acted on — verify it matches your DM's chat id
- Check that `workflow_id` in the `carts` table matches the running Temporal workflow

### Workflow stuck in "pending_approval"
- The cart's `workflow_id` column holds the Temporal workflow ID
- In Temporal UI, find the workflow and check if it's waiting on `wait_condition`
- You can manually signal it: `flyctl ssh console` → Temporal CLI or via the UI's Signal button

### Checkout staging failed with "Failed to add X to cart"
- ASIN may have changed (Amazon updates ASINs occasionally)
- Re-run the workflow — it will re-scrape fresh ASINs
- `prepare_checkout_activity` runs with `maximum_attempts=1` (NO_RETRY) and is idempotent on `idempotency_key`, so re-staging never double-adds

### Cost spike alert fired
- Default threshold is `$1.00/run` in `evals.py`
- Check Langfuse for which model calls were expensive
- Amazon price lookup (many products + vision calls) is the main driver
- Adjust `COST_ALERT_THRESHOLD_USD` in `evals.py` to your comfort level
