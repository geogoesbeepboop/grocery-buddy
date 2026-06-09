---
name: add-migration
description: Author and apply the next numbered Supabase/Postgres migration for grocery-buddy. Use when adding or altering a table, column, index, or constraint. Handles the NNN_ numbering, the house style (why-first header, idempotent DDL), and the apply path (Supabase SQL editor / MCP).
---

# Add a database migration

Migrations live in `migrations/` as `NNN_short_description.sql`, applied **in
order** against the Supabase Postgres. They are zero-padded and sequential.

## 1. Pick the next number

List `migrations/` and take the highest `NNN` + 1 (highest is currently
`011_llm_usage.sql` → new file is `012_short_description.sql`). Never reuse or
renumber an existing file.

## 2. Write it in the house style

Open with a **multi-line comment explaining the *why*** (the behavior/business
reason), then idempotent DDL. Model it on `migrations/009_pending_replenishments.sql`:

```sql
-- 012_short_description.sql
--
-- <Why this exists: the behavior it enables or the bug it closes. Explain the
--  data lifecycle and how it's read, not just "add a table".>

CREATE TABLE IF NOT EXISTS thing (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'done')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Comment each index with the access pattern it serves.
CREATE INDEX IF NOT EXISTS idx_thing_user_status ON thing (user_id, status);
```

House rules observed across `001`–`011`:
- **Idempotent**: `CREATE TABLE/INDEX IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`.
  Assume a migration may be re-run.
- **No transaction wrapper, no RLS, no GRANTs** — Supabase applies each file
  transactionally and the app connects as a service account.
- **FKs**: `ON DELETE CASCADE` for ownership; `ON DELETE SET NULL` when the child
  row has independent meaning the pantry math still depends on (see how `009`
  treats `cart_id`).
- **Enums** via `CHECK (col IN (...))`, not Postgres enum types.
- **Comment every index** with the query it serves.

## 3. Apply it

There is **no auto-runner** — the app does not apply migrations on startup. Apply
the new file's SQL to the remote Supabase project (ref `looimknbtjhvwxbpkbyc`) via
either:
- The **Supabase MCP**: `mcp__supabase__apply_migration` (preferred from here), or
- The Supabase SQL editor:
  `https://supabase.com/dashboard/project/looimknbtjhvwxbpkbyc/sql`.

Apply only the new file; earlier migrations are already applied.

## 4. Reflect it in code + docs

- If the new shape is read/written by an activity, wire the queries in
  `src/grocery_buddy/tools/` or `workflows/activities.py` (see `tools/inventory.py`
  for the asyncpg pattern). New status values must match every place they're read.
- Update **`docs/DATABASE.md`** (schema detail) and, if behavior changed,
  **`docs/SYSTEM_REFERENCE.md`**.
- `uv run pytest -q`.

## Example dev workflow

> You're adding per-user spend caps. You create `012_user_spend_caps.sql` (highest is
> `011`) with a why-first header and idempotent DDL, apply it via
> `mcp__supabase__apply_migration`, then read/write the column in `tools/inventory.py`
> and surface it in `docs/DATABASE.md` — recent migrations `010_prediction_snapshots`
> and `011_llm_usage` are good shape references.
