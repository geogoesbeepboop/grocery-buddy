"""Regression tests for Amazon order-history scraping (the /import scraper).

These drive the real parsing helpers against synthetic HTML that mirrors the
your-orders/search results layout (one item per row: image+qty badge in the left
column, "Ordered on <date>" + title + "Buy it again" in the right column). They
guard the things that broke in practice:
  - the search-results (item-row) layout is scraped at all,
  - the order date is read (not null),
  - the per-item quantity badge is picked up,
  - recommendation carousels / non-order /dp/ links are NOT scraped,
  - pagination's "Next" link is discovered.

Skips automatically if Playwright's chromium binary isn't installed.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

from grocery_buddy.automation import amazon  # noqa: E402

# Two real-looking order rows + a recommendation carousel (must be ignored) + a
# pagination control. The recommendation's only "Ordered on" ancestor is <body>,
# which also wraps multiple products — the structural guard must reject it.
_SEARCH_HTML = """
<html><body>
<div id="ordersContainer">
  <div class="a-fixed-left-grid a-spacing-base">
   <div class="a-fixed-left-grid-inner">
    <div class="a-fixed-left-grid-col a-col-left">
      <a class="a-link-normal" href="/gp/product/B0BYNJCW7P/ref=x"><img src="x"></a>
    </div>
    <div class="a-fixed-left-grid-col a-col-right">
      <div class="a-row"><a href="/gp/css/order-details?orderID=111-222">View order details</a> | Ordered on June 3, 2026</div>
      <div class="a-row"><a class="a-link-normal" href="/gp/product/B0BYNJCW7P/ref=y">Yiwafu 2 Pack Tongue Scraper, Stainless Steel Tongue Cleaners</a></div>
      <div class="a-row"><span class="a-button-text">Buy it again</span></div>
    </div>
   </div>
  </div>

  <div class="a-fixed-left-grid a-spacing-base">
   <div class="a-fixed-left-grid-inner">
    <div class="a-fixed-left-grid-col a-col-left">
      <a class="a-link-normal" href="/dp/B00GOYA001"><img src="x"></a>
      <span class="item-view-qty-info">2</span>
    </div>
    <div class="a-fixed-left-grid-col a-col-right">
      <div class="a-row"><a href="/gp/css/order-details?orderID=333">View order details</a> | Ordered on May 20, 2026</div>
      <div class="a-row"><a class="a-link-normal" href="/dp/B00GOYA001/ref=z">Goya Dry Lentils, 16 oz</a></div>
      <div class="a-row"><span>Buy it again</span></div>
    </div>
   </div>
  </div>
</div>

<div class="recommendations">
  <a class="a-link-normal" href="/dp/B0RECO0001">Random Recommended Dress You Never Ordered</a>
</div>

<ul class="a-pagination">
  <li class="a-normal"><a href="/your-orders/search?opt=ab&search=george&startIndex=0">1</a></li>
  <li class="a-last"><a href="/your-orders/search?opt=ab&search=george&startIndex=10">Next</a></li>
</ul>
</body></html>
"""


async def _scrape(html: str):
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:  # chromium not installed in this environment
        pytest.skip(f"chromium unavailable: {exc}")
    try:
        page = await browser.new_page()
        await page.set_content(html)
        orders = await amazon._scrape_orders_on_page(page)
        next_url = await amazon._get_next_page_url(page)
        return orders, next_url
    finally:
        await browser.close()
        await pw.stop()


async def test_search_results_layout_is_scraped():
    orders, next_url = await _scrape(_SEARCH_HTML)
    by_title = {o["items"][0]["title"]: o for o in orders}

    # Both real order items are found...
    tongue = next((o for t, o in by_title.items() if "Tongue Scraper" in t), None)
    goya = next((o for t, o in by_title.items() if "Goya Dry Lentils" in t), None)
    assert tongue is not None
    assert goya is not None

    # ...with their dates (not null) and ASINs...
    assert tongue["order_date"] == "June 3, 2026"
    assert tongue["items"][0]["asin"] == "B0BYNJCW7P"
    assert goya["order_date"] == "May 20, 2026"
    assert goya["items"][0]["asin"] == "B00GOYA001"

    # ...the multi-unit quantity badge is read...
    assert goya["items"][0]["qty"] == 2
    assert tongue["items"][0]["qty"] == 1

    # ...the recommendation carousel is NOT scraped...
    assert all("Dress" not in t for t in by_title)

    # ...and pagination's Next link is discovered.
    assert next_url and "startIndex=10" in next_url


# Same staple bought on two different dates (two rows) PLUS an exact same-date
# duplicate row. The repeats-on-different-dates must both survive (reorder signal);
# the same-product-same-date duplicate must collapse to one.
_REPEAT_HTML = """
<html><body>
<div id="ordersContainer">
""" + "".join(
    f"""
  <div class="a-fixed-left-grid a-spacing-base"><div class="a-fixed-left-grid-inner">
    <div class="a-fixed-left-grid-col a-col-left"><a href="/dp/B00MILK0001"><img></a></div>
    <div class="a-fixed-left-grid-col a-col-right">
      <div class="a-row"><a href="/gp/css/order-details?orderID={oid}">View order details</a> | Ordered on {date}</div>
      <div class="a-row"><a class="a-link-normal" href="/dp/B00MILK0001/ref={oid}">Oatly Oat Milk, 64 oz</a></div>
    </div>
  </div></div>
"""
    for oid, date in [("A1", "May 1, 2026"), ("A2", "May 15, 2026"), ("A3", "May 1, 2026")]
) + """
</div></body></html>
"""


async def test_repeat_purchases_on_different_dates_are_kept():
    orders, _ = await _scrape(_REPEAT_HTML)
    milk = [o for o in orders if "Oat Milk" in o["items"][0]["title"]]
    dates = sorted(o["order_date"] for o in milk)
    # Two distinct dates kept; the duplicate May 1 row collapsed.
    assert dates == ["May 1, 2026", "May 15, 2026"], dates


def test_orders_page_url_search_vs_unfiltered():
    assert "search=George" in amazon._orders_page_url("George", 0)
    assert "startIndex=20" in amazon._orders_page_url("George", 20)
    # No name → full unfiltered orders list (no search param).
    assert "search=" not in amazon._orders_page_url(None, 0)
