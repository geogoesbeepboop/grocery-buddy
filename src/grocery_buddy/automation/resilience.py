"""Self-healing, observable element resolution for the Amazon automation.

The automation is pinned to Amazon's internal CSS ids. When Amazon renames a class
the old code returned ``[]``/``False`` silently — "couldn't pull from Amazon" with
no signal pointing at the broken selector. This module turns that silent failure
into a fast deterministic path, a self-healing fallback, and an alert:

  Strategy + build_locator   A small descriptor (css / role / text / label / …) that
                             builds a Playwright locator. Lets a single "intent"
                             carry a chain of CSS ids AND role/ARIA fallbacks, since
                             accessible names churn far less than ids.

  first_matching             Try a chain of strategies, return the first that
                             matches anything. The deterministic fast path.

  resolve                    first_matching for a *critical* intent, but instrumented:
                             record hit/miss to the run's health report, and on a
                             total 0-match fall back to a11y/vision REPAIR — ask an
                             LLM over the accessibility tree (vision as last resort)
                             which element matches the intent, validate the answer,
                             CACHE the rediscovered descriptor, and use it. Repair only
                             ever runs on a 0-match, so the hot path stays deterministic.

  health report              Per-run record of which intents matched / missed /
                             self-healed. ``summarize_health`` flags a critical 0-match
                             so the activity layer can page the user and score Langfuse.

Everything degrades gracefully: no cache file, no LLM key, no active health report —
each becomes a no-op, never an error.
"""
from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)


# ── Strategy: a serializable locator descriptor ───────────────────────────────


@dataclasses.dataclass(frozen=True)
class Strategy:
    """One way to find an element. ``kind`` selects the Playwright builder.

    ``css``   — raw selector (may be a comma-joined variant chain).
    ``role``  — ARIA role + optional accessible ``name`` (regex by default, since
                names vary in casing/whitespace).
    ``text`` / ``label`` / ``placeholder`` / ``testid`` — the corresponding
                ``get_by_*`` helpers, matched on ``name``.
    """

    kind: str
    css: str | None = None
    role: str | None = None
    name: str | None = None
    name_is_regex: bool = True
    exact: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Strategy:
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})


def css(selector: str) -> Strategy:
    return Strategy(kind="css", css=selector)


def role(role_name: str, name: str | None = None, *, exact: bool = False,
         regex: bool = True) -> Strategy:
    return Strategy(kind="role", role=role_name, name=name, name_is_regex=regex, exact=exact)


def text(value: str, *, regex: bool = True, exact: bool = False) -> Strategy:
    return Strategy(kind="text", name=value, name_is_regex=regex, exact=exact)


def _name_arg(s: Strategy):
    if s.name is None:
        return None
    return re.compile(s.name, re.I) if s.name_is_regex else s.name


def build_locator(scope: Page | Locator, s: Strategy) -> Locator | None:
    """Build a locator for ``s`` under ``scope`` (a Page or a parent Locator)."""
    try:
        if s.kind == "css":
            return scope.locator(s.css) if s.css else None
        name = _name_arg(s)
        if s.kind == "role":
            if not s.role:
                return None
            if name is not None:
                return scope.get_by_role(s.role, name=name, exact=s.exact)  # type: ignore[arg-type]
            return scope.get_by_role(s.role)  # type: ignore[arg-type]
        if s.kind == "text" and name is not None:
            return scope.get_by_text(name, exact=s.exact)
        if s.kind == "label" and name is not None:
            return scope.get_by_label(name, exact=s.exact)
        if s.kind == "placeholder" and name is not None:
            return scope.get_by_placeholder(name, exact=s.exact)
        if s.kind == "testid" and s.name is not None:
            return scope.get_by_test_id(s.name)
    except Exception as exc:
        logger.debug("build_locator failed for %s (%s)", s, exc)
    return None


# ── Selector cache: rediscovered descriptors persist across runs ──────────────


class _SelectorCache:
    """A tiny JSON map of ``intent -> Strategy`` for selectors repair rediscovered.

    Lives OUTSIDE the browser profile (which the scraper copies/wipes) so a healed
    selector survives. Loaded once, written atomically. All operations best-effort.
    """

    def __init__(self) -> None:
        self._path = Path(settings.selector_cache_path)
        self._data: dict[str, dict] = {}
        self._loaded = False

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                self._data = json.loads(self._path.read_text() or "{}")
        except Exception as exc:
            logger.debug("Selector cache unreadable (%s) — starting empty", exc)
            self._data = {}

    def get(self, intent: str) -> Strategy | None:
        self._ensure()
        raw = self._data.get(intent)
        if not raw:
            return None
        try:
            return Strategy.from_dict(raw)
        except Exception:
            return None

    def put(self, intent: str, s: Strategy) -> None:
        self._ensure()
        self._data[intent] = s.to_dict()
        try:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self._path)
        except Exception as exc:
            logger.debug("Selector cache not written (%s)", exc)


_cache = _SelectorCache()


# ── Health report: what matched, what missed, what self-healed ────────────────


@dataclasses.dataclass
class _IntentOutcome:
    intent: str
    matched: int = 0
    attempts: int = 0
    repaired: bool = False
    critical: bool = False
    note: str = ""


class SelectorHealthReport:
    """Accumulates per-intent resolution outcomes over one run (one browser job)."""

    def __init__(self, context: str = "") -> None:
        self.context = context
        self.outcomes: dict[str, _IntentOutcome] = {}

    def record(self, intent: str, *, matched: int, repaired: bool = False,
               critical: bool = False, note: str = "") -> None:
        o = self.outcomes.get(intent) or _IntentOutcome(intent=intent)
        o.attempts += 1
        o.matched += max(0, matched)
        o.repaired = o.repaired or repaired
        o.critical = o.critical or critical
        if note:
            o.note = note
        self.outcomes[intent] = o

    def critical_misses(self) -> list[_IntentOutcome]:
        """Intents that, across the whole run, never matched and are make-or-break."""
        return [o for o in self.outcomes.values() if o.critical and o.matched == 0]

    def healed(self) -> list[_IntentOutcome]:
        return [o for o in self.outcomes.values() if o.repaired]


_active_report: contextvars.ContextVar[SelectorHealthReport | None] = contextvars.ContextVar(
    "_active_selector_report", default=None
)


@contextmanager
def health_run(context: str = "") -> Iterator[SelectorHealthReport]:
    """Make a fresh health report current for the duration of a browser job."""
    report = SelectorHealthReport(context=context)
    token = _active_report.set(report)
    try:
        yield report
    finally:
        _active_report.reset(token)


def current_report() -> SelectorHealthReport | None:
    return _active_report.get()


def _record(intent: str, *, matched: int, repaired: bool = False,
            critical: bool = False, note: str = "") -> None:
    report = _active_report.get()
    if report is not None:
        report.record(intent, matched=matched, repaired=repaired, critical=critical, note=note)


def observe(intent: str, *, matched: int, repaired: bool = False,
            critical: bool = False, note: str = "") -> None:
    """Public hook for code that resolves elements its own way (not via ``resolve``)
    but still wants its hit/miss on the run's health report. No-op without a report."""
    _record(intent, matched=matched, repaired=repaired, critical=critical, note=note)


# ── Resolution: deterministic chain, then repair on a 0-match ─────────────────


async def _count(loc: Locator | None, *, require_visible: bool) -> int:
    if loc is None:
        return 0
    try:
        n = await loc.count()
        if n and require_visible:
            return 1 if await loc.first.is_visible() else 0
        return n
    except Exception:
        return 0


async def first_matching(
    scope: Page | Locator,
    strategies: list[Strategy],
    *,
    require_visible: bool = False,
) -> Locator | None:
    """Return the locator for the first strategy that matches ≥1 element. No health."""
    for s in strategies:
        loc = build_locator(scope, s)
        if await _count(loc, require_visible=require_visible):
            return loc
    return None


async def resolve(
    scope: Page | Locator,
    intent: str,
    strategies: list[Strategy],
    *,
    page: Page | None = None,
    describe: str | None = None,
    critical: bool = True,
    require_visible: bool = False,
) -> Locator | None:
    """Resolve ``intent`` and record its health.

    Order: a cached (repair-rediscovered) descriptor first, then the deterministic
    ``strategies``. On a total 0-match, if repair is enabled and we have a ``page``
    and a ``describe`` string, run a11y/vision repair, validate the result, cache it,
    and use it. Records the outcome on the active health report.
    """
    chain = list(strategies)
    cached = _cache.get(intent)
    if cached is not None:
        chain = [cached, *chain]

    loc = await first_matching(scope, chain, require_visible=require_visible)
    if loc is not None:
        _record(intent, matched=1, repaired=False, critical=critical)
        return loc

    # Deterministic chain came up empty → the silent-failure case. Try to self-heal.
    repair_page = page if page is not None else (scope if isinstance(scope, Page) else None)
    if settings.selector_repair_enabled and repair_page is not None and describe:
        healed = await _repair(repair_page, scope, intent, describe,
                               require_visible=require_visible)
        if healed is not None:
            built = build_locator(scope, healed)
            if await _count(built, require_visible=require_visible):
                # Only persist a descriptor we just confirmed still matches, so a dud
                # can never poison the cache for the next run.
                _cache.put(intent, healed)
                logger.info("Selector repair healed %r via %s", intent, healed.kind)
                _record(intent, matched=1, repaired=True, critical=critical,
                        note=f"healed→{healed.kind}")
                return built

    _record(intent, matched=0, repaired=False, critical=critical,
            note="0 matches; repair failed" if settings.selector_repair_enabled else "0 matches")
    return None


# ── Repair: LLM over the accessibility tree, vision as last resort ─────────────

_REPAIR_SYSTEM = (
    "You are a web-automation repair tool. A deterministic selector for a known UI "
    "element stopped matching after a site redesign. Given a description of the target "
    "and the page's accessibility tree (and optionally a screenshot), identify the single "
    "best element and return how to locate it. "
    "Respond ONLY with a compact JSON object, no prose, shaped exactly like: "
    '{"kind": "role"|"css"|"text", "role": "<aria role or null>", '
    '"name": "<accessible name / visible text / null>", "css": "<css selector or null>"}. '
    "Strongly prefer kind=role with an accessible name (most stable). Use css only when "
    "no role/name uniquely identifies it. name may be a substring; it is matched "
    "case-insensitively."
)


async def _repair(
    page: Page,
    scope: Page | Locator,
    intent: str,
    describe: str,
    *,
    require_visible: bool,
) -> Strategy | None:
    """Ask an LLM to relocate the element, validate the answer, return a Strategy."""
    try:
        import anthropic  # lazy: only loaded on the rare repair path
    except Exception:
        return None
    if not settings.anthropic_api_key:
        logger.warning("Selector repair wanted for %r but ANTHROPIC_API_KEY is unset", intent)
        return None

    tree = await _aria_snapshot(page)
    if not tree:
        return None

    user_blocks: list[dict] = [{
        "type": "text",
        "text": (
            f"Target description: {describe}\n"
            f"(internal intent id: {intent})\n\n"
            f"Accessibility tree of the current page:\n{tree}"
        ),
    }]

    # Vision is a last resort (cost + latency); only attach a screenshot when enabled.
    if settings.selector_repair_vision:
        shot_b64 = await _screenshot_b64(page)
        if shot_b64:
            user_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": shot_b64},
            })

    model = settings.selector_repair_model or settings.model_smart
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            system=_REPAIR_SYSTEM,
            messages=[{"role": "user", "content": user_blocks}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    except Exception as exc:
        logger.warning("Selector repair LLM call failed for %r: %s", intent, exc)
        return None

    descriptor = _parse_descriptor(raw)
    if descriptor is None:
        logger.warning("Selector repair returned unparseable answer for %r: %s", intent, raw[:200])
        return None

    candidate = await first_matching(scope, [descriptor], require_visible=require_visible)
    if candidate is None:
        # The proposed locator didn't actually match — don't cache a dud.
        logger.warning("Selector repair proposal for %r matched nothing", intent)
        return None
    return descriptor


def _parse_descriptor(raw: str) -> Strategy | None:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    kind = str(d.get("kind") or "").strip().lower()
    if kind == "role" and d.get("role"):
        return Strategy(kind="role", role=str(d["role"]).strip(),
                        name=(str(d["name"]).strip() if d.get("name") else None),
                        name_is_regex=True)
    if kind == "css" and d.get("css"):
        return Strategy(kind="css", css=str(d["css"]).strip())
    if kind == "text" and d.get("name"):
        return Strategy(kind="text", name=str(d["name"]).strip(), name_is_regex=True)
    # Tolerate a partial answer: a role or css present without a correct "kind".
    if d.get("role"):
        return Strategy(kind="role", role=str(d["role"]).strip(),
                        name=(str(d["name"]).strip() if d.get("name") else None))
    if d.get("css"):
        return Strategy(kind="css", css=str(d["css"]).strip())
    return None


async def _aria_snapshot(page: Page, *, max_chars: int = 6_000) -> str | None:
    """Compact ARIA tree (role + name pairs) for the page body, capped in size."""
    try:
        snap = await page.locator("body").aria_snapshot()
    except Exception as exc:
        logger.debug("aria_snapshot failed (%s)", exc)
        return None
    if not snap:
        return None
    return snap if len(snap) <= max_chars else snap[:max_chars] + "\n… (truncated)"


async def _screenshot_b64(page: Page) -> str | None:
    import base64
    try:
        png = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(png).decode("ascii")
    except Exception as exc:
        logger.debug("Repair screenshot failed (%s)", exc)
        return None


# ── Summary for alerting (read by the activity layer) ─────────────────────────


def summarize_health(report: SelectorHealthReport | None) -> dict | None:
    """Return an alert summary if the run had a critical 0-match, else None.

    Also surfaces selectors that self-healed (drifted but were repaired) so the user
    knows a redesign is underway even when nothing user-visible broke. Emits the same
    facts to Langfuse as scores (no-op when Langfuse is unconfigured).
    """
    if report is None:
        return None

    crit = report.critical_misses()
    healed = report.healed()
    _emit_langfuse_scores(report, crit, healed)

    if not crit and not healed:
        return None
    return {
        "context": report.context,
        "broken": [{"intent": o.intent, "note": o.note} for o in crit],
        "healed": [{"intent": o.intent, "note": o.note} for o in healed],
    }


def _emit_langfuse_scores(report: SelectorHealthReport, crit: list, healed: list) -> None:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return
    try:
        from langfuse import Langfuse  # type: ignore[import]

        lf = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        # 1.0 = all critical intents resolved; drops as critical selectors break.
        total_critical = sum(1 for o in report.outcomes.values() if o.critical) or 1
        health = 1.0 - (len(crit) / total_critical)
        lf.score(
            name="selector_health",
            value=round(health, 3),
            comment=json.dumps({
                "context": report.context,
                "broken": [o.intent for o in crit],
                "healed": [o.intent for o in healed],
            }),
        )
        lf.flush()
    except Exception as exc:
        logger.debug("Selector-health Langfuse emit failed (%s)", exc)
