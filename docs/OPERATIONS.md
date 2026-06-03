# Operations Guide

## Running locally

Three processes need to be running for full functionality:

```bash
# Terminal 1 — Temporal server
docker compose up

# Terminal 2 — Temporal worker (processes workflows)
uv run grocery-buddy worker

# Terminal 3 — Webhook server (receives ntfy approve/reject taps)
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

### Change the auto-purchase cap
```sql
UPDATE preferences SET auto_purchase_cap_usd = 75.00 WHERE user_id = '<uuid>';
```

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

When a cart exceeds your `auto_purchase_cap_usd`:

1. Your phone receives a push notification with **✅ Approve** and **❌ Reject** buttons
2. Tap one → ntfy calls `POST /approve/{workflow_id}` or `/reject/{workflow_id}` on your webhook server
3. The webhook signals Temporal → the durable workflow resumes from its 24-hour wait
4. If approved, Playwright adds items to your Amazon cart and proceeds to checkout
5. You receive a confirmation push with the order reference

If you don't respond within 24 hours, the workflow marks the cart as `expired` and no purchase is made.

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
  NTFY_TOPIC=grocery-buddy-<your-suffix> \
  WEBHOOK_BASE_URL=https://<your-app>.fly.dev
```

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
- Check if your Amazon session has expired: `AMAZON_HEADLESS=false uv run python scripts/setup_amazon_session.py`

### Webhook not receiving signals
- Confirm `WEBHOOK_BASE_URL` is publicly reachable (ngrok / Fly.io URL)
- Test: `curl -X POST https://your-url/approve/test-workflow-id`
- Check that `workflow_id` in the `carts` table matches the running Temporal workflow

### Workflow stuck in "pending_approval"
- The cart's `workflow_id` column holds the Temporal workflow ID
- In Temporal UI, find the workflow and check if it's waiting on `wait_condition`
- You can manually signal it: `flyctl ssh console` → Temporal CLI or via the UI's Signal button

### Purchase failed with "Failed to add X to cart"
- ASIN may have changed (Amazon updates ASINs occasionally)
- Re-run the workflow — it will re-scrape fresh ASINs
- The idempotency key prevents double-purchasing even if retried

### Cost spike alert fired
- Default threshold is `$1.00/run` in `evals.py`
- Check Langfuse for which model calls were expensive
- Amazon price lookup (many products + vision calls) is the main driver
- Adjust `COST_ALERT_THRESHOLD_USD` in `evals.py` to your comfort level
