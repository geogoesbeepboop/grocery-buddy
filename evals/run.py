"""Model-accuracy eval harness — runs labeled datasets against the REAL models.

This is the prompt-regression tier (see docs/EVALS.md). It makes paid,
nondeterministic API calls, so it runs on-demand / nightly, NOT per-commit.

    uv run python -m evals.run                          # all suites
    uv run python -m evals.run --suite intents
    uv run python -m evals.run --threshold 0.8          # exit 1 if any suite < 0.8

Suites:
    intents          parse_request / parse_briefing_reply routing accuracy (gating)
    briefings        compose_briefing groundedness — deterministic checks (gating)
    briefing_quality compose_briefing tone/groundedness — LLM-as-judge (report-only)
    synthesis        synthesize_grocery_history product-set recall + exclusion (gating)
    onboarding       onboarding extraction tool-call recall (gating)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from grocery_buddy import tracing
from grocery_buddy.config import settings
from grocery_buddy.products import normalize_product

_DATASETS = Path(__file__).parent / "datasets"

# Suites whose score should gate a nightly run (report-only suites are excluded).
_GATING = {"intents", "briefings", "synthesis", "onboarding"}


def _load(name: str) -> list[dict]:
    path = _DATASETS / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


# ── Suites ────────────────────────────────────────────────────────────────────


async def suite_intents() -> dict:
    """Routing accuracy of parse_request / parse_briefing_reply (exact action match)."""
    from grocery_buddy.agents.assistant import parse_briefing_reply, parse_request

    rows = _load("intents.jsonl")
    cases: list[dict] = []
    correct = 0
    for r in rows:
        if r.get("cart_pending"):
            res = await parse_briefing_reply(r["message"], r.get("cart_items", []))
        else:
            res = await parse_request(r["message"])
        got = res.get("action")
        ok = got == r["expected_action"]
        correct += int(ok)
        cases.append({"message": r["message"], "want": r["expected_action"], "got": got, "ok": ok})
    return {"suite": "intents", "score": _avg([float(c["ok"]) for c in cases]), "n": len(rows), "cases": cases}


async def suite_briefings() -> dict:
    """compose_briefing groundedness via deterministic checks (no judge)."""
    from grocery_buddy.agents.assistant import compose_briefing

    rows = _load("briefings.jsonl")
    cases: list[dict] = []
    scores: list[float] = []
    for r in rows:
        text = await compose_briefing(r["items"], r["total_usd"], r.get("reason"))
        low = text.lower()
        checks = [f"{r['total_usd']:.2f}" in text, len(text) <= 4096]
        for it in r["items"]:
            display = (it.get("notes") or it["product"]).split(",")[0]
            token = display.split()[0].lower()
            checks.append(token in low)
        passed = sum(checks)
        scores.append(passed / len(checks))
        cases.append({"total": r["total_usd"], "passed": f"{passed}/{len(checks)}"})
    return {"suite": "briefings", "score": _avg(scores), "n": len(rows), "cases": cases}


async def suite_briefing_quality() -> dict:
    """compose_briefing tone/groundedness via an LLM judge (report-only)."""
    from evals.judges import llm_judge
    from grocery_buddy.agents.assistant import compose_briefing

    rows = _load("briefings.jsonl")
    cases: list[dict] = []
    scores: list[float] = []
    for r in rows:
        text = await compose_briefing(r["items"], r["total_usd"], r.get("reason"))
        names = ", ".join((it.get("notes") or it["product"]) for it in r["items"])
        verdict = await llm_judge(
            criteria=(
                "A warm, concise grocery approval text for Telegram that lists EXACTLY "
                f"these items [{names}], states the exact total ${r['total_usd']:.2f}, "
                "invents/drops/repriced nothing, and reads like a friend not a receipt."
            ),
            content=text,
        )
        scores.append(verdict["score"])
        cases.append({"score": round(verdict["score"], 2), "reason": verdict["reason"]})
    return {"suite": "briefing_quality", "score": _avg(scores), "n": len(rows), "cases": cases}


async def suite_synthesis() -> dict:
    """synthesize_grocery_history: recall of expected products + exclusion of one-offs."""
    from grocery_buddy.agents.order_history import synthesize_grocery_history

    rows = _load("synthesis.jsonl")
    cases: list[dict] = []
    scores: list[float] = []
    for r in rows:
        proposal = await synthesize_grocery_history(r["orders"])
        blob = " | ".join(normalize_product(p.get("product", "")) for p in proposal).lower()
        want = r.get("expect_products_contain", [])
        hits = [w for w in want if w.lower() in blob]
        recall = len(hits) / len(want) if want else 1.0
        # Penalize if a one-off (cable/phone) leaked into the pantry.
        excluded = r.get("expect_excluded_contain", [])
        leaks = [e for e in excluded if e.lower() in blob]
        score = recall * (0.0 if leaks else 1.0)
        scores.append(score)
        cases.append({"found": len(proposal), "want": want, "hits": hits, "leaks": leaks})
    return {"suite": "synthesis", "score": _avg(scores), "n": len(rows), "cases": cases}


async def suite_onboarding() -> dict:
    """Onboarding extraction: do the save_* tool calls capture the expected products?

    Runs the REAL onboarding prompt + tools for one turn but does NOT persist (so no
    DB is needed) — we inspect the model's tool calls directly.
    """
    from grocery_buddy import llm
    from grocery_buddy.agents.onboarding import _SYSTEM, _TOOLS

    rows = _load("onboarding.jsonl")
    cases: list[dict] = []
    scores: list[float] = []
    for r in rows:
        resp = await llm.create_message(
            model=settings.model_fast,
            label="eval_onboarding",
            max_tokens=1024,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=[{"role": "user", "content": r["message"]}],
        )
        captured = [
            (b.input or {}).get("product", "")
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
            and b.name in ("save_inventory_item", "save_consumption_habit")
        ]
        blob = " | ".join(captured).lower()
        want = r["expect_products_contain"]
        hits = [w for w in want if w.lower() in blob]
        scores.append(len(hits) / len(want) if want else 1.0)
        cases.append({"want": want, "captured": captured, "hits": hits})
    return {"suite": "onboarding", "score": _avg(scores), "n": len(rows), "cases": cases}


_SUITES = {
    "intents": suite_intents,
    "briefings": suite_briefings,
    "briefing_quality": suite_briefing_quality,
    "synthesis": suite_synthesis,
    "onboarding": suite_onboarding,
}


# ── Runner ────────────────────────────────────────────────────────────────────


async def _run(selected: list[str], threshold: float | None, verbose: bool) -> int:
    if not settings.anthropic_api_key:
        print("No ANTHROPIC_API_KEY set — model evals skipped (this is expected in PR CI).")
        return 0

    results = []
    for name in selected:
        print(f"running suite: {name} …", flush=True)
        results.append(await _SUITES[name]())

    print("\n── Eval scorecard ─────────────────────────────")
    failed = False
    for res in results:
        score = res["score"]
        gate = res["suite"] in _GATING
        s = "n/a" if score is None else f"{score:.2f}"
        flag = ""
        if gate and threshold is not None and score is not None and score < threshold:
            flag = f"  ❌ below {threshold:.2f}"
            failed = True
        tag = "" if gate else "  (report-only)"
        print(f"  {res['suite']:<17} {s}  (n={res['n']}){tag}{flag}")
        if score is not None:
            tracing.record_score(name=f"eval_{res['suite']}", value=score, comment=f"n={res['n']}")
        if verbose:
            for c in res["cases"]:
                print(f"      {c}")
    print()
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="grocery-buddy model-accuracy evals")
    parser.add_argument("--suite", choices=list(_SUITES), help="Run one suite (default: all)")
    parser.add_argument("--threshold", type=float, default=None, help="Exit 1 if a gating suite < this")
    parser.add_argument("--verbose", action="store_true", help="Print per-case detail")
    args = parser.parse_args()
    selected = [args.suite] if args.suite else list(_SUITES)
    sys.exit(asyncio.run(_run(selected, args.threshold, args.verbose)))


if __name__ == "__main__":
    main()
