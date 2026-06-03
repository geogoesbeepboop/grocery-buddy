# Setup Guide

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker Desktop (for local Temporal server)
- An Amazon account with groceries enabled (Fresh or Whole Foods delivery)
- A free [Langfuse account](https://cloud.langfuse.com) (optional but recommended)
- A free [ntfy account / topic](https://ntfy.sh) + the ntfy app on your phone

---

## Step 1 — Clone and install

```bash
git clone <repo-url> grocery-buddy
cd grocery-buddy
uv sync
```

---

## Step 2 — Configure `.env`

Copy the example and fill in the values:

```bash
cp .env.example .env
```

| Variable | Where to get it | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys | ✅ |
| `SUPABASE_URL` | Pre-filled: `https://looimknbtjhvwxbpkbyc.supabase.co` | ✅ |
| `SUPABASE_ANON_KEY` | Pre-filled in `.env.example` | ✅ |
| `DATABASE_URL` | Supabase dashboard → Settings → Database → Reset password → Session pooler URL | ✅ |
| `NTFY_TOPIC` | Any unique string, e.g. `grocery-buddy-george42` | ✅ |
| `WEBHOOK_BASE_URL` | Your public webhook URL (see Step 6) | ✅ |
| `LANGFUSE_PUBLIC_KEY` | [cloud.langfuse.com](https://cloud.langfuse.com) → Project Settings | Optional |
| `LANGFUSE_SECRET_KEY` | Same | Optional |
| `AUTO_PURCHASE_CAP_USD` | Default `50.0` — adjust to taste | Optional |

**DATABASE_URL format:**
```
DATABASE_URL=postgresql://postgres.looimknbtjhvwxbpkbyc:<YOUR-DB-PASSWORD>@aws-0-us-east-1.pooler.supabase.com:5432/postgres
```
Get your DB password: [Supabase → Settings → Database](https://supabase.com/dashboard/project/looimknbtjhvwxbpkbyc/settings/database) → Reset database password.

---

## Step 3 — Start local Temporal server

```bash
docker compose up -d
```

Temporal UI is available at [http://localhost:8088](http://localhost:8088).

---

## Step 4 — Create your user record

The `users` table needs a row before you can do anything else.

```bash
uv run python scripts/seed_user.py --email you@example.com --name "George"
```

This prints your **User UUID** — save it, it's used in all subsequent commands.

Alternatively, run this SQL in the [Supabase SQL editor](https://supabase.com/dashboard/project/looimknbtjhvwxbpkbyc/sql):
```sql
INSERT INTO users (email, name) VALUES ('you@example.com', 'George') RETURNING id;
INSERT INTO preferences (user_id) VALUES ('<uuid-from-above>');
```

---

## Step 5 — Save your Amazon session

This runs a visible browser so you can log in once. After closing, the session is saved to `.amazon-session/` and used for all future headless runs.

```bash
AMAZON_HEADLESS=false uv run python scripts/setup_amazon_session.py
```

**Important:** Switch to the Amazon Prime profile you want the agent to use before closing the browser. The session captures whichever profile was active.

---

## Step 6 — Set up the webhook (ntfy approval gate)

The agent sends push notifications with Approve/Reject buttons. When you tap one, ntfy calls back to your webhook server. The webhook needs to be publicly reachable.

**For local dev (ngrok):**
```bash
# Terminal 1 — start the webhook server
uv run grocery-buddy webhook

# Terminal 2 — expose it with ngrok
ngrok http 8080
# Copy the https://xxxx.ngrok-free.app URL

# Add to .env:
WEBHOOK_BASE_URL=https://xxxx.ngrok-free.app
```

**For production (Fly.io):**
The webhook server runs as a service with a stable public URL — see [OPERATIONS.md](./OPERATIONS.md).

---

## Step 7 — Install the ntfy app

1. Install [ntfy](https://ntfy.sh) on your phone (iOS or Android, free)
2. Subscribe to your topic: tap **+** → enter your `NTFY_TOPIC` value
3. You'll now receive push notifications when a cart is ready for approval

---

## Step 8 — Onboard (seed inventory + habits)

```bash
uv run grocery-buddy onboard --user-id <your-uuid>
```

This runs a conversational intake — Claude asks about your pantry and consumption habits. Be specific: "I go through about a dozen eggs every 10 days" works well. The data is saved directly to `inventory_items` and `consumption_profile`.

---

## Step 9 — Start the worker

In a dedicated terminal (or as a background service):

```bash
uv run grocery-buddy worker
```

This connects to your local Temporal server and starts processing workflows.

---

## Step 10 — Test with a manual run

```bash
uv run grocery-buddy run --user-id <your-uuid>
```

Watch the Temporal UI at [localhost:8088](http://localhost:8088) to see the workflow progress. Check your phone — if the cart total exceeds your cap you'll get an approval push.

---

## Step 11 — Set up the daily schedule

```bash
uv run grocery-buddy schedule --user-id <your-uuid> --cron "0 8 * * *" --timezone "America/New_York"
```

The cron expression is in **UTC**. Common values:
- `0 8 * * *` = 8:00 AM UTC (4 AM ET, 1 AM PT)
- `0 13 * * *` = 1:00 PM UTC (9 AM ET, 6 AM PT)
- `0 12 * * *` = 12:00 PM UTC (8 AM ET, 5 AM PT)

---

## Verifying everything works

After a successful run you should see:

- A new row in `carts` with `status = 'purchased'` (or `'pending_approval'`)
- Rows in `cart_items` with Amazon prices and ASINs
- A row in `purchases` with `status = 'completed'` and a non-null `idempotency_key`
- A Langfuse trace (if configured) with per-run cost
- A confirmation push on your phone

```sql
-- Quick health check
SELECT c.status, c.total_usd, c.created_at, p.status as purchase_status
FROM carts c
LEFT JOIN purchases p ON p.cart_id = c.id
WHERE c.user_id = '<your-uuid>'
ORDER BY c.created_at DESC
LIMIT 5;
```
