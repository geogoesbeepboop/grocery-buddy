---
name: run-evals
description: Run and extend grocery-buddy's eval suites — predictor precision/recall (DB snapshots), real-model prompt-regression suites (evals/), per-run cost, and the money-live gate. Use when checking LLM-output or predictor quality, before/after a prompt or predictor change, growing the eval datasets, or "are the evals passing".
---

# Run (and extend) evals

Full reference: **`docs/EVALS.md`**. The layer is two tiers, deliberately separate:

- **Tier 1 — `tests/`** — deterministic, model **mocked**, free, every commit (mapping,
  metric math, cost math, truncation). Run: `uv run pytest -q`.
- **Tier 2 — `evals/`** — **real** Haiku/Sonnet, scored/thresholded, costs cents, nightly
  (prompt-regression: routing + briefing/synthesis/onboarding quality).
  **Never put paid model calls in `tests/`** — that's the whole reason this tier exists.

## Commands

```bash
# Tier 2 — real-model prompt evals (needs ANTHROPIC_API_KEY; exits 0 + skips without it)
uv run python -m evals.run                            # all suites
uv run python -m evals.run --suite intents            # one suite
uv run python -m evals.run --threshold 0.8 --verbose  # gate — what nightly CI runs

# Predictor precision/recall for one user (reads prediction_snapshots + llm_usage ledger)
make evals USER_ID=<uuid>           # = uv run grocery-buddy evals --user-id <uuid>

# Operational signals (also money-live gate conditions, see docs/EVALS.md §4–5)
grocery-buddy scraper-health        # do Amazon selectors still extract price + ASIN?
grocery-buddy gate --user-id <uuid> # money_live_ready? (checkout_verified is a HARD STOP)
```

## Suites (`evals/run.py`; datasets in `evals/datasets/*.jsonl`)

| Suite | Tests | Gating |
|---|---|---|
| `intents` | `parse_request` / `parse_briefing_reply` routing (exact action match) | ✅ |
| `briefings` | `compose_briefing` groundedness (deterministic) | ✅ |
| `briefing_quality` | `compose_briefing` tone (LLM-as-judge, `evals/judges.py`) | report-only |
| `synthesis` | `synthesize_grocery_history` product recall + one-off exclusion | ✅ |
| `onboarding` | onboarding tool-call extraction recall | ✅ |

## Extending the evals (the usual task)

The datasets are the regression net — grow them as real messages reveal gaps:

1. Add labeled rows to `evals/datasets/<suite>.jsonl` (one JSON object per line; copy the
   shape of existing rows).
2. Re-run that suite: `uv run python -m evals.run --suite <name> --verbose`.
3. A new LLM surface → add a `suite_*` function in `evals/run.py` (and to `_GATING` if it
   should block nightly).
4. New *deterministic* checks (metric / cost / mapping) belong in `tests/` with the model
   mocked — never in `evals/`.

## Example dev workflow

> You tighten the intent-routing prompt in `agents/assistant.py`. Tier-1 `tests/` can't
> catch a prompt regression (model is mocked), so you run
> `uv run python -m evals.run --suite intents --verbose`, spot one message that now
> misroutes, add it as a labeled row to `intents.jsonl`, fix the prompt, and re-run until
> the suite clears `0.8`.

The eval layer was rebuilt June 2026 (real prediction snapshots, a cost ledger, model
suites) — the old tautological-recall and stubbed-cost bugs are fixed (`docs/EVALS.md`
"What this replaced"). Big changes (new suite + datasets + a metric) → the **`eval-writer`**
subagent.
