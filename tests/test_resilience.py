"""Tests for the self-healing/observable element-resolution layer.

The pure-logic tests (Strategy round-trip, selector cache, health report,
summarize_health) run anywhere. The locator tests drive real Playwright against
synthetic HTML and skip automatically when chromium isn't installed.
"""
from __future__ import annotations

import pytest

from grocery_buddy.automation import resilience as R

# ── Pure logic (no browser) ───────────────────────────────────────────────────


def test_strategy_dict_round_trip():
    s = R.role("button", "Add to Cart", regex=True)
    again = R.Strategy.from_dict(s.to_dict())
    assert again == s
    # Unknown keys are ignored, not fatal.
    assert R.Strategy.from_dict({"kind": "css", "css": "#x", "bogus": 1}).css == "#x"


def test_selector_cache_round_trips_to_disk(tmp_path, monkeypatch):
    path = tmp_path / "sel-cache.json"
    monkeypatch.setattr(R.settings, "selector_cache_path", str(path))
    cache = R._SelectorCache()
    assert cache.get("atc.button") is None

    cache.put("atc.button", R.role("button", "add to cart"))
    assert path.exists()

    # A fresh instance reads what the first one wrote.
    reloaded = R._SelectorCache()
    got = reloaded.get("atc.button")
    assert got is not None and got.kind == "role" and got.role == "button"


def test_health_report_tracks_hits_misses_and_heals():
    report = R.SelectorHealthReport(context="pricing")
    report.record("search.results", matched=1, critical=True)
    report.record("atc.button", matched=0, critical=True, note="gone")
    report.record("cart.subtotal", matched=1, repaired=True, critical=False)

    misses = {o.intent for o in report.critical_misses()}
    assert misses == {"atc.button"}  # the matched critical one is NOT a miss
    assert {o.intent for o in report.healed()} == {"cart.subtotal"}


def test_summarize_health_flags_critical_miss_and_heal(monkeypatch):
    # Keep Langfuse a no-op regardless of the dev's real env.
    monkeypatch.setattr(R.settings, "langfuse_public_key", "")
    monkeypatch.setattr(R.settings, "langfuse_secret_key", "")

    healthy = R.SelectorHealthReport()
    healthy.record("search.results", matched=1, critical=True)
    assert R.summarize_health(healthy) is None  # nothing broke or healed → no alert

    broken = R.SelectorHealthReport(context="checkout")
    broken.record("atc.button", matched=0, critical=True, note="no button")
    broken.record("cart.subtotal", matched=1, repaired=True)
    summary = R.summarize_health(broken)
    assert summary is not None
    assert summary["broken"] == [{"intent": "atc.button", "note": "no button"}]
    assert summary["healed"] and summary["healed"][0]["intent"] == "cart.subtotal"


def test_summarize_health_none_report_is_safe():
    assert R.summarize_health(None) is None


def test_observe_is_noop_without_active_report():
    # Outside a health_run() there is no active report — must not raise.
    R.observe("whatever", matched=0, critical=True)


def test_health_run_sets_and_clears_active_report():
    assert R.current_report() is None
    with R.health_run("import") as report:
        assert R.current_report() is report
        R.observe("orders.firstpage", matched=0, critical=True)
        assert report.critical_misses()[0].intent == "orders.firstpage"
    assert R.current_report() is None  # reset on exit


def test_parse_descriptor_prefers_role_and_tolerates_noise():
    s = R._parse_descriptor(
        'here you go: {"kind":"role","role":"button","name":"Add to Cart","css":null}'
    )
    assert s and s.kind == "role" and s.role == "button" and s.name == "Add to Cart"
    # A css-only answer still parses.
    s2 = R._parse_descriptor('{"kind":"css","css":"#add-to-cart-button"}')
    assert s2 and s2.kind == "css" and s2.css == "#add-to-cart-button"
    # Garbage → None (never a dud strategy).
    assert R._parse_descriptor("no json here") is None


# ── Locator behaviour (needs chromium) ────────────────────────────────────────

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

_HTML = """
<html><body>
  <div id="real-id" data-asin="B001">First card</div>
  <button>Add to Cart</button>
</body></html>
"""


async def _page(html: str):
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    page = await browser.new_page()
    await page.set_content(html)
    return pw, browser, page


async def test_first_matching_picks_first_non_empty():
    pw, browser, page = await _page(_HTML)
    try:
        # The bogus selector matches nothing; resolution falls through to the real one.
        loc = await R.first_matching(page, [R.css("#does-not-exist"), R.css("#real-id")])
        assert loc is not None
        assert (await loc.first.text_content()).strip() == "First card"

        # Role fallback finds the button when no css is given.
        btn = await R.first_matching(page, [R.css("#nope"), R.role("button", "add to cart")])
        assert btn is not None and (await btn.count()) == 1
    finally:
        await browser.close()
        await pw.stop()


async def test_resolve_records_hit_and_returns_locator(monkeypatch, tmp_path):
    # Repair off → a clean miss path, no LLM involved. Point the module cache
    # singleton at an empty tmp file so the dev's real cache can't leak in.
    monkeypatch.setattr(R.settings, "selector_repair_enabled", False)
    monkeypatch.setattr(R.settings, "selector_cache_path", str(tmp_path / "c.json"))
    monkeypatch.setattr(R, "_cache", R._SelectorCache())
    pw, browser, page = await _page(_HTML)
    try:
        with R.health_run("test") as report:
            loc = await R.resolve(
                page, "search.results",
                [R.css("#real-id")],
                page=page, describe="the card", critical=True,
            )
            assert loc is not None
            assert not report.critical_misses()  # matched → not a miss

            miss = await R.resolve(
                page, "atc.button",
                [R.css("#totally-absent")],
                page=page, describe="a button that isn't there", critical=True,
            )
            assert miss is None
            assert {o.intent for o in report.critical_misses()} == {"atc.button"}
    finally:
        await browser.close()
        await pw.stop()
