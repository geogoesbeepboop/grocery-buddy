"""Tests for the network-interception helpers (cart-mutation detection, JSON capture).

These exercise the pure decision logic with lightweight fakes, plus one chromium-gated
check that resource-blocking actually aborts an image request. No real Amazon traffic.
"""
from __future__ import annotations

import pytest

from grocery_buddy.automation import network as net


class _FakeResponse:
    def __init__(self, url: str, status: int = 200, content_type: str = "application/json"):
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}


def test_cart_mutation_matches_amazon_cart_endpoints():
    assert net._looks_like_cart_mutation(_FakeResponse("https://www.amazon.com/gp/add-to-cart/json"))
    assert net._looks_like_cart_mutation(_FakeResponse("https://www.amazon.com/cart/add"))
    assert net._looks_like_cart_mutation(_FakeResponse("https://www.amazon.com/hz/cart/ajax"))


def test_cart_mutation_rejects_unrelated_or_errored():
    # Unrelated URL.
    assert not net._looks_like_cart_mutation(_FakeResponse("https://www.amazon.com/dp/B001"))
    # Right endpoint but a 4xx/5xx is not a success signal.
    err = _FakeResponse("https://www.amazon.com/cart/add", status=500)
    assert not net._looks_like_cart_mutation(err)


async def test_json_collector_filters_by_url_substring():
    collector = net.JsonResponseCollector(("/your-orders/",))

    # Non-matching URL is ignored entirely.
    collector._on_response(_FakeResponse("https://www.amazon.com/dp/B001"))
    assert not collector.saw_any

    # Matching but non-JSON → recorded as metadata (saw it fire) with no body.
    collector._on_response(
        _FakeResponse("https://www.amazon.com/your-orders/orders", content_type="text/html")
    )
    assert collector.saw_any
    assert collector.payloads() == []


class _FakeExpect:
    """Async-CM double for page.expect_response that simulates a timeout on exit."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        raise TimeoutError("Timeout 10ms exceeded waiting for expect_response")


class _FakePage:
    def expect_response(self, predicate, timeout):
        return _FakeExpect()


async def test_confirm_add_to_cart_returns_none_when_no_cart_response():
    # No matching response → expect_response "times out", but the click still ran.
    # confirm_add_to_cart must report None (inconclusive) rather than raise, so the
    # caller falls back to overlay/cart-count instead of treating it as a hard fail.
    clicked = {"count": 0}

    async def _click():
        clicked["count"] += 1

    result = await net.confirm_add_to_cart(_FakePage(), _click, timeout_ms=10)
    assert result is None
    assert clicked["count"] == 1  # the click DID fire inside the window


# ── Resource blocking against a real browser ──────────────────────────────────

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402


async def test_block_heavy_resources_aborts_images():
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    aborted: list[str] = []
    try:
        page = await browser.new_page()
        await net.block_heavy_resources(page)
        # Observe routing decisions: an image request should be aborted, the document not.
        page.on("requestfailed", lambda r: aborted.append(r.resource_type))
        await page.set_content(
            '<html><body><img src="https://example.com/cat.png">'
            "<p>hello</p></body></html>"
        )
        await page.wait_for_timeout(300)
        assert "image" in aborted
    finally:
        await browser.close()
        await pw.stop()
