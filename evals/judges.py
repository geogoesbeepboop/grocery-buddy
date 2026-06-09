"""LLM-as-judge helper for the eval harness.

Used where a rubric beats an exact-match (e.g. briefing tone/groundedness). Returns
a 0..1 score + a short reason. Best-effort: a malformed/blocked judgement degrades to
a neutral 0.0 with the error as the reason rather than crashing the suite.
"""
from __future__ import annotations

import json
import re

from grocery_buddy import llm
from grocery_buddy.config import settings

_JUDGE_SYSTEM = (
    "You are a strict evaluator. Given CRITERIA and CONTENT, score how well the "
    "content meets the criteria from 0.0 (fails) to 1.0 (fully meets). Respond ONLY "
    'with a compact JSON object: {"score": <float 0..1>, "reason": "<short phrase>"}.'
)


async def llm_judge(criteria: str, content: str) -> dict:
    """Score ``content`` against ``criteria`` in [0, 1] via a Haiku judge."""
    try:
        resp = await llm.create_message(
            model=settings.model_fast,
            label="llm_judge",
            max_tokens=200,
            system=_JUDGE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"CRITERIA:\n{criteria}\n\nCONTENT:\n{content}",
                }
            ],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        score = float(data.get("score", 0.0))
        return {"score": max(0.0, min(1.0, score)), "reason": str(data.get("reason", ""))}
    except Exception as exc:  # never crash a suite over a judgement
        return {"score": 0.0, "reason": f"judge error: {exc}"}
