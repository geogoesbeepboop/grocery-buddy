"""Tests for checkout cart staging — the fix for cart accumulation across runs.

Two layers:

  1. ``clear_cart`` (real Playwright against synthetic cart HTML served via a
     route): it empties an active cart, reports the already-empty case as success,
     and — crucially — fails CLOSED (returns False) when deletes don't actually
     take effect, so the activity can refuse to stage onto an uncertain cart.

  2. ``prepare_checkout_activity`` (fake DB pool + a stateful fake Amazon cart):
     staging cart B after an un-bought cart A leaves ONLY B's items, and
     re-staging a crashed 'pending' cart doesn't double its items. These are the
     regressions the no-clear bug caused.

The Playwright layer skips automatically if chromium isn't installed.
"""
from __future__ import annotations

import uuid

import pytest

# ── Layer 1: clear_cart against synthetic cart HTML ───────────────────────────

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

from grocery_buddy.automation import amazon  # noqa: E402

# An active cart with two line items. A capture-phase listener makes each "Delete"
# button remove its own line item — a local stand-in for Amazon's AJAX smart-cart
# removal, enough to exercise clear_cart's find→click→re-check→stop loop.
_TWO_ITEM_CART_HTML = """
<html><body>
<div id="sc-active-cart">
  <div class="sc-list-item" data-asin="B0MILK0001">
    <span>Organic Whole Milk</span>
    <span class="sc-action-delete"><input type="submit" value="Delete"></span>
  </div>
  <div class="sc-list-item" data-asin="B0EGGS0002">
    <span>Large Brown Eggs</span>
    <span class="sc-action-delete"><input type="submit" value="Delete"></span>
  </div>
</div>
<script>
  document.addEventListener('click', function (e) {
    var el = e.target;
    if (el && el.tagName === 'INPUT' && el.value === 'Delete') {
      e.preventDefault();
      var row = el.closest('.sc-list-item');
      if (row) row.remove();
    }
  }, true);
</script>
</body></html>
"""

# Same markup, but with NO script — clicking Delete does nothing, so the cart can
# never empty. clear_cart must detect the lack of progress and bail (return False).
_STUCK_CART_HTML = _TWO_ITEM_CART_HTML.replace("<script>", "<!--").replace("</script>", "-->")

_EMPTY_CART_HTML = """
<html><body>
<div id="sc-active-cart"></div>
<h1 class="sc-your-amazon-cart-is-empty">Your Amazon Cart is empty</h1>
</body></html>
"""


async def _cart_context(html: str):
    """Launch chromium and serve ``html`` for any cart-view navigation."""
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:  # chromium not installed in this environment
        pytest.skip(f"chromium unavailable: {exc}")
    context = await browser.new_context()
    await context.route(
        "**/gp/cart/view.html*",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    return pw, browser, context


async def test_clear_cart_already_empty_returns_true():
    pw, browser, context = await _cart_context(_EMPTY_CART_HTML)
    try:
        assert await amazon.clear_cart(context) is True
    finally:
        await context.close()
        await browser.close()
        await pw.stop()


async def test_clear_cart_removes_every_item():
    pw, browser, context = await _cart_context(_TWO_ITEM_CART_HTML)
    try:
        # Sanity: the page really does start with two items clear_cart must remove.
        probe = await context.new_page()
        await probe.goto(amazon.AMAZON_CART_URL)
        assert await amazon._count_cart_items(probe) == 2
        await probe.close()

        assert await amazon.clear_cart(context) is True
    finally:
        await context.close()
        await browser.close()
        await pw.stop()


async def test_clear_cart_fails_closed_when_delete_has_no_effect():
    # If Delete clicks don't remove anything (e.g. Amazon changed the control),
    # clear_cart must NOT hang and must NOT claim success — it returns False so the
    # caller refuses to stage onto a cart it couldn't empty.
    pw, browser, context = await _cart_context(_STUCK_CART_HTML)
    try:
        assert await amazon.clear_cart(context) is False
    finally:
        await context.close()
        await browser.close()
        await pw.stop()


# ── Layer 2: prepare_checkout_activity (clear-then-stage) ─────────────────────


class _FakeCart:
    """Stateful stand-in for the live Amazon cart: records adds and clears."""

    def __init__(self) -> None:
        self.asins: list[str] = []
        self.clears = 0

    async def add(self, asin: str, _context) -> bool:
        self.asins.append(asin)
        return True

    async def clear(self, _context) -> bool:
        self.clears += 1
        self.asins.clear()
        return True


class _Noop:
    """Browser/playwright handle with the async close()/stop() the activity awaits."""

    async def close(self) -> None: ...
    async def stop(self) -> None: ...


class _FakePool:
    """Minimal asyncpg-pool stand-in covering prepare_checkout_activity's queries.

    Matches on stable substrings of each statement; raises on anything unexpected
    so the test fails loudly if the activity's SQL drifts.
    """

    def __init__(self, carts: dict[str, dict]) -> None:
        # carts: {cart_id_str: {"items": [ {asin, product, ...} ], "total": float}}
        self.carts = carts
        self.purchases: dict[str, dict] = {}  # idempotency_key -> row

    async def fetchrow(self, sql: str, *args):
        s = " ".join(sql.split())
        if "FROM purchases WHERE idempotency_key" in s:
            return self.purchases.get(args[0])
        if "INSERT INTO purchases" in s:
            cart_id, key = str(args[0]), args[1]
            row = self.purchases.get(key)
            if row is None:
                row = {
                    "id": uuid.uuid4(), "cart_id": cart_id, "idempotency_key": key,
                    "status": "pending", "total_usd": None,
                    "retailer_order_ref": None, "error": None,
                }
                self.purchases[key] = row
            else:
                row["status"] = "pending"  # ON CONFLICT DO UPDATE SET status='pending'
            return {"id": row["id"]}
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def fetch(self, sql: str, *args):
        s = " ".join(sql.split())
        if "FROM cart_items WHERE cart_id" in s:
            return list(self.carts[str(args[0])]["items"])
        raise AssertionError(f"unexpected fetch: {s}")

    async def fetchval(self, sql: str, *args):
        s = " ".join(sql.split())
        if "total_usd FROM carts" in s:
            return self.carts[str(args[0])]["total"]
        raise AssertionError(f"unexpected fetchval: {s}")

    async def execute(self, sql: str, *args):
        s = " ".join(sql.split())
        if "UPDATE purchases" in s and "checkout_ready" in s:
            self._purchase_by_id(args[0]).update(
                status="checkout_ready", retailer_order_ref=args[1], total_usd=args[2]
            )
        elif "UPDATE purchases" in s and "failed" in s:
            self._purchase_by_id(args[0]).update(status="failed", error=args[1])
        elif "UPDATE carts" in s:
            pass  # cart status not asserted here
        else:
            raise AssertionError(f"unexpected execute: {s}")
        return "UPDATE 1"

    def _purchase_by_id(self, pid) -> dict:
        return next(r for r in self.purchases.values() if str(r["id"]) == str(pid))


def _patch_staging(monkeypatch, pool: _FakePool, cart: _FakeCart) -> None:
    async def _get_pool():
        return pool

    async def _anoop(*_a, **_k):
        return None

    async def _fake_context():
        handle = _Noop()
        return handle, handle

    monkeypatch.setattr("grocery_buddy.workflows.activities.get_pool", _get_pool)
    monkeypatch.setattr("grocery_buddy.workflows.activities.send_checkout_link", _anoop)
    monkeypatch.setattr("grocery_buddy.automation.amazon.get_browser_context", _fake_context)
    monkeypatch.setattr("grocery_buddy.automation.amazon.add_to_cart_by_asin", cart.add)
    monkeypatch.setattr("grocery_buddy.automation.amazon.clear_cart", cart.clear)


async def _stage(cart_id: str):
    from grocery_buddy.workflows.activities import prepare_checkout_activity

    return await prepare_checkout_activity(
        {"cart_id": cart_id, "user_id": str(uuid.uuid4()), "idempotency_key": f"purchase-{cart_id}"}
    )


async def test_staging_cart_b_after_cart_a_yields_only_b(monkeypatch):
    cart_a, cart_b = str(uuid.uuid4()), str(uuid.uuid4())
    pool = _FakePool({
        cart_a: {"items": [{"asin": "A1", "product": "Milk"},
                           {"asin": "A2", "product": "Eggs"}], "total": 10.0},
        cart_b: {"items": [{"asin": "B1", "product": "Bread"}], "total": 4.0},
    })
    live = _FakeCart()
    _patch_staging(monkeypatch, pool, live)

    # Stage cart A — but the user never places the order, so A's items sit in the
    # real Amazon cart.
    await _stage(cart_a)
    assert live.asins == ["A1", "A2"]

    # Later, cart B is staged. The cart must be cleared first so it reflects ONLY
    # B — not A's leftovers stacked underneath (the over-order bug).
    await _stage(cart_b)
    assert live.asins == ["B1"]


async def test_restage_after_crashed_pending_does_not_double(monkeypatch):
    cart_a = str(uuid.uuid4())
    pool = _FakePool({
        cart_a: {"items": [{"asin": "A1", "product": "Milk"},
                           {"asin": "A2", "product": "Eggs"}], "total": 10.0},
    })
    live = _FakeCart()
    _patch_staging(monkeypatch, pool, live)

    await _stage(cart_a)
    assert live.asins == ["A1", "A2"]

    # Simulate a crash mid-staging: this NO_RETRY activity left the purchases row
    # at 'pending' (not 'checkout_ready'), and A's items still sit in the cart. A
    # re-run must reconcile by clear-then-restage, not blindly re-add → no doubling.
    pool.purchases[f"purchase-{cart_a}"]["status"] = "pending"
    await _stage(cart_a)
    assert live.asins == ["A1", "A2"]
    assert live.clears == 2  # cleared on both the first stage and the re-stage


async def test_checkout_ready_short_circuit_does_not_touch_cart(monkeypatch):
    # When a cart is already 'checkout_ready', re-running must just re-send the
    # link and leave the live cart untouched (the user may be mid-checkout).
    cart_a = str(uuid.uuid4())
    pool = _FakePool({
        cart_a: {"items": [{"asin": "A1", "product": "Milk"}], "total": 3.0},
    })
    live = _FakeCart()
    _patch_staging(monkeypatch, pool, live)

    await _stage(cart_a)
    assert live.asins == ["A1"] and live.clears == 1

    # Re-run with the row already 'checkout_ready' (its state after a successful stage).
    result = await _stage(cart_a)
    assert result.get("already_done") is True
    assert live.asins == ["A1"]  # unchanged
    assert live.clears == 1      # clear_cart NOT called again


async def test_clear_failure_aborts_staging(monkeypatch):
    # If the cart can't be emptied, the activity must refuse to stage (raise) and
    # never add onto an uncertain cart, rather than risk mixing in stale items.
    cart_a = str(uuid.uuid4())
    pool = _FakePool({
        cart_a: {"items": [{"asin": "A1", "product": "Milk"}], "total": 3.0},
    })
    live = _FakeCart()
    _patch_staging(monkeypatch, pool, live)

    async def _failing_clear(_context):
        return False

    monkeypatch.setattr("grocery_buddy.automation.amazon.clear_cart", _failing_clear)

    with pytest.raises(RuntimeError, match="empty the existing Amazon cart"):
        await _stage(cart_a)
    assert live.asins == []  # nothing was added
    assert pool.purchases[f"purchase-{cart_a}"]["status"] == "failed"
