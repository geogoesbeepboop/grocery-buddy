---
name: add-activity
description: Wire a new Temporal activity into grocery-buddy end-to-end — define it in activities.py, register it in worker.py, and call it by string name from a workflow. Use whenever adding a step that does I/O (DB, HTTP, Playwright, Claude calls) inside a Temporal workflow.
---

# Add a Temporal activity

Activities hold **all I/O**; workflows stay deterministic and call activities by
**string name**. The single most common bug is forgetting step 3 (registration),
which fails at runtime with `ActivityNotFound` / "not registered on worker".

## The 4 steps

### 1. Define the activity in `src/grocery_buddy/workflows/activities.py`

```python
@activity.defn
async def my_new_activity(payload: dict) -> dict:
    """One line: what it does. Activities own the I/O."""
    pool = await get_pool()            # db, httpx, playwright, the llm client — all fine here
    ...
    return {"ok": True}
```

Conventions in this file:
- Pass a **single `dict` payload** (or a simple scalar like `user_id: str`) and
  return a JSON-serializable `dict`/`list` — these cross the Temporal boundary.
- Heavy imports (`from grocery_buddy.automation.amazon import ...`,
  `anthropic`, etc.) go **inside** the function body, not at module top, mirroring
  the existing activities.
- The Temporal activity name is auto-derived from the function name. Keep it
  `snake_case`; the string you call must match exactly.
- **If it calls a model**, go through `grocery_buddy.llm` (never the Anthropic SDK)
  and wrap the body in `with run_scope(activity.info().workflow_id, user_id):` so the
  cost lands on the run's `llm_usage` ledger (see `docs/EVALS.md` §2).

### 2. Import it in `src/grocery_buddy/workflows/worker.py`

Add the name to the alphabetized import block from
`grocery_buddy.workflows.activities`.

### 3. Register it in the `Worker(...)` `activities=[...]` list

In the same `worker.py`, add the **function object** to the `activities=[]` list.
**If you skip this the worker can't find it.**

### 4. Call it from a workflow — by STRING name

In `grocery_run.py` / `quick_buy.py` / `import_history.py`:

```python
result = await workflow.execute_activity(
    "my_new_activity",                 # ← string, matches the function name
    payload,
    schedule_to_close_timeout=_ACTIVITY_TIMEOUT,   # always set a timeout
    retry_policy=_STANDARD_RETRY,                  # _STANDARD_RETRY or _NO_RETRY
)
```

Use `_NO_RETRY` for anything with side effects that aren't idempotent (e.g. the
purchase/checkout path). Use `_STANDARD_RETRY` for safe-to-retry reads.

## Guardrails (Temporal sandbox)

Inside the **workflow** module: no `from __future__ import annotations`; no
module-level non-stdlib imports except in `with workflow.unsafe.imports_passed_through():`
(only `config`/`models`); no `datetime.now()`/`asyncio.sleep`/random/db/file/network
— use `workflow.now()`, `workflow.sleep()`, `workflow.logger`. The activity is
where I/O belongs; the workflow only orchestrates.

## If this activity touches money or the cart

If the new activity stages a checkout, adds to cart, or moves toward a purchase,
it must run **only after** the approval gate (see `CLAUDE.md` invariant #2) and
should be reviewed by the **`money-path-security-reviewer`** subagent.

## Verify

1. `uv run pytest -q` — add a unit test under `tests/` if the activity has logic worth
   pinning (mock the model for anything LLM-driven; see `tests/test_runlist.py` for style).
2. Sanity-check registration: the activity name appears in **both** the import block and
   the `activities=[]` list in `worker.py`.
3. If behavior changed for the user, update `docs/SYSTEM_REFERENCE.md` (its activities list).

## Example dev workflow

> You're adding Kroger price lookups. You write `lookup_kroger_prices(payload)` in
> `activities.py` (httpx call inside the body), add it to the import block **and** the
> `activities=[]` list in `worker.py`, then call `await workflow.execute_activity(
> "lookup_kroger_prices", payload, …)` from `grocery_run.py`. Forgetting the `worker.py`
> registration is the classic `ActivityNotFound` at runtime.
