# grocery-buddy

Autonomous grocery agent: predict low stock → price on Amazon → **approval-gated** cart staging.
Temporal · Supabase · Claude · Playwright · Telegram. Map of the system:
**`docs/SYSTEM_REFERENCE.md`** — read it for non-trivial work, and **update it when you change behavior**.

## Invariants — do not break

- **Never place an order or spend money.** The agent stages an Amazon cart and returns a checkout
  link; the human taps "Place order." No code may drive Amazon checkout. The money-live spine is
  gated in `gating.py` (`checkout_verified` is a hard stop) — never loosen a gate or add an
  unattended buy path. Changes to the purchase/cart/gate path → run the **money-path-security-reviewer**.
- **Models go through `grocery_buddy.llm`** (it meters cost to the `llm_usage` ledger). Use
  `settings.model_fast` (Haiku) everywhere except `synthesize_grocery_history` → `settings.model_smart`
  (Sonnet). Never hardcode a model id; never call the Anthropic SDK directly.

## Temporal workflows (`workflows/*.py`)

Replay-deterministic. No I/O, clocks (`workflow.now()`), or randomness in workflow code — that all
lives in activities. No `from __future__ import annotations`; non-stdlib imports only inside
`workflow.unsafe.imports_passed_through()`. Call activities by **string name** and register them in
`worker.py`. Audit workflow changes with the **temporal-sandbox-reviewer**.

## Tests vs evals

`tests/` = deterministic, model **mocked**, runs every commit. `evals/` = real-model prompt
regression, nightly. **Never put paid model calls in `tests/`.** Details: `docs/EVALS.md`.

Dev loop: `./scripts/dev.sh up` then `./scripts/dev.sh restart`. Checks: `make test` (`make lint` for style).
