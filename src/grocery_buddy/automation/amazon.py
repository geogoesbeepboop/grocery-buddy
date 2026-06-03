"""Amazon grocery automation via Playwright (persistent authenticated session).

IMPORTANT: No public Amazon consumer ordering API exists. This module drives the
real Amazon website using a saved browser profile. Before first use, run:
  uv run python scripts/setup_amazon_session.py
to log in interactively and save the session.

Risks: brittle to UI changes, ToS-sensitive. Always confirm purchases before
executing checkout until the automation is proven reliable for your account.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)

# Search the regular "Grocery & Gourmet Food" department (i=grocery) — Prime/
# shipped listings the user can buy with their normal account. We deliberately do
# NOT use i=amazonfresh: the Amazon Fresh storefront requires a Fresh subscription
# and quotes higher Fresh-only prices (e.g. milk at $4.55 vs the $3.48 regular
# listing), so sourcing from it both overcharges and points at items the user
# can't actually check out.
AMAZON_GROCERY_SEARCH = "https://www.amazon.com/s?i=grocery&k={query}"

# Canonical, account-scoped cart URL. We hand this back as the "checkout link"
# instead of a session-bound /gp/buy/spc/ checkout URL: the SPC URL belongs to the
# Playwright browser session and forces a fresh sign-in (the double-auth) when
# opened on the user's phone, whereas the cart URL resolves against the user's own
# logged-in Amazon (web or app) where the items already sit.
AMAZON_CART_URL = "https://www.amazon.com/gp/cart/view.html"

# Amazon Fresh / Whole Foods listings can still surface inside a grocery search as
# store-specific offers. These need a Fresh/WF subscription and quote store-only
# prices, so we drop them — substrings checked case-insensitively against a
# result's full text.
_FRESH_BADGES = ("amazon fresh", "whole foods")


async def get_browser_context() -> tuple:
    """Return (playwright, context) with the persistent Amazon session."""
    profile_dir = Path(settings.amazon_profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Launching Amazon browser (headless=%s, profile=%s) — "
        "set AMAZON_HEADLESS=false to watch it work",
        settings.amazon_headless,
        profile_dir,
    )
    p = await async_playwright().start()
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=settings.amazon_headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    return p, context


async def is_logged_in(context: BrowserContext) -> bool:
    page = await context.new_page()
    try:
        await page.goto("https://www.amazon.com", timeout=15_000)
        await page.wait_for_load_state("domcontentloaded")
        # Logged-in state shows the account name; signed-out shows "Hello, sign in"
        sign_in_visible = await page.locator("#nav-link-accountList-nav-line-1:has-text('Sign in')").is_visible()
        return not sign_in_visible
    except Exception as exc:
        logger.warning("Login check failed: %s", exc)
        return False
    finally:
        await page.close()


_PRICE_RE = re.compile(r"[\d,]+\.\d{2}")


async def _extract_result_price(result) -> float | None:
    """Read the current price for a single search result.

    Reads the atomic ``.a-offscreen`` value ("$3.48") from the first price block
    that is NOT a struck-through list price, so we never splice the whole part of
    one price with the fraction of another. Falls back to whole+fraction taken
    from the SAME block if the offscreen text is missing.
    """
    # Atomic full-price text from a non-strikethrough price block.
    spans = result.locator("span.a-price:not(.a-text-price) span.a-offscreen")
    for j in range(min(await spans.count(), 4)):
        raw = (await spans.nth(j).text_content(timeout=1_500)) or ""
        m = _PRICE_RE.search(raw)
        if m:
            return float(m.group(0).replace(",", ""))

    # Fallback: whole + fraction, but from one block so they belong together.
    block = result.locator("span.a-price:not(.a-text-price)").first
    if await block.count():
        whole = (await block.locator(".a-price-whole").first.text_content(timeout=1_500)) or ""
        whole = whole.strip().replace(",", "").rstrip(".")
        if whole:
            frac = (await block.locator(".a-price-fraction").first.text_content(timeout=1_500)) or "00"
            frac = frac.strip() or "00"
            try:
                return float(f"{whole}.{frac}")
            except ValueError:
                return None
    return None


async def search_grocery_price(
    product: str, context: BrowserContext, max_results: int = 5
) -> list[dict]:
    """Search Amazon Grocery for a product and return the top candidate results.

    Returns up to ``max_results`` priced candidates (title + price + ASIN), each
    a dict shaped like::

        {"product": str, "price_usd": float, "asin": str | None, "source": "amazon_scraped"}

    Returns an empty list if nothing could be found or priced. Brand-aware
    selection among these candidates happens upstream in the activity layer.
    """
    page = await context.new_page()
    try:
        url = AMAZON_GROCERY_SEARCH.format(query=product.replace(" ", "+"))
        await page.goto(url, timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        result_locator = page.locator('[data-component-type="s-search-result"]')
        await result_locator.first.wait_for(timeout=15_000)

        count = await result_locator.count()
        candidates: list[dict] = []

        for i in range(count):
            if len(candidates) >= max_results:
                break
            result = result_locator.nth(i)

            try:
                # Drop Amazon Fresh / Whole Foods store offers that slip into the
                # grocery department — they need a Fresh/WF subscription and quote
                # store-only (usually higher) prices the user can't check out with.
                badge_text = (await result.text_content(timeout=1_500) or "").lower()
                if any(b in badge_text for b in _FRESH_BADGES):
                    logger.debug("Skipping Fresh/Whole Foods result %d for %r", i, product)
                    continue

                # Price. A single result often renders MULTIPLE price blocks: the
                # current price, a struck-through "list price" (.a-text-price), and a
                # per-unit price ("$0.03/fl oz"). Naively combining the first
                # .a-price-whole with the first .a-price-fraction can splice values
                # from different blocks and invent a wrong price. Instead, read one
                # atomic screen-reader value ("$3.48") from the first NON-struck price.
                price_usd = await _extract_result_price(result)
                if price_usd is None:
                    continue  # no real price (sponsored banner, "see options", etc.)

                # ASIN — the result element carries it as a data attribute, which is
                # far more reliable than parsing the title link's href (Amazon Fresh
                # result types vary). Fall back to the /dp/ href if needed. We can
                # only add items to the cart by ASIN, so skip anything without one.
                asin = (await result.get_attribute("data-asin") or "").strip() or None
                if not asin:
                    link_el = result.locator("a[href*='/dp/']").first
                    if await link_el.count():
                        href = await link_el.get_attribute("href") or ""
                        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href)
                        asin = m.group(1) if m else None
                if not asin:
                    continue

                # Title — try multiple selectors; Amazon Fresh varies across result
                # types. Fall back to the generic product name rather than crashing.
                title = product
                for title_sel in ("h2 a span", "h2 span", "[data-cy='title-recipe'] span",
                                  ".a-size-medium", ".a-size-base-plus"):
                    el = result.locator(title_sel).first
                    if await el.count() and await el.is_visible():
                        raw = await el.text_content(timeout=1_500)
                        if raw and raw.strip():
                            title = raw.strip()
                            break

                candidates.append({
                    "product": title,
                    "price_usd": price_usd,
                    "asin": asin,
                    "source": "amazon_scraped",
                })
            except Exception as exc:
                logger.debug("Skipping result %d for %r: %s", i, product, exc)
                continue

        logger.info("Amazon search for %r → %d candidates", product, len(candidates))
        return candidates

    except Exception as exc:
        logger.warning("Amazon search failed for %r: %s", product, exc)
        return []
    finally:
        await page.close()


# Confirmation signals shown after a successful add-to-cart, across Amazon's
# various layouts (interstitial page, side-sheet, smart-wagon overlay).
_ATC_CONFIRM_SELECTORS = (
    "#attach-added-to-cart-message",
    "#sw-atc-details-single-container",
    "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
    "#huc-v2-order-row-confirm-text",
    "#sw-atc-confirmation",
    "#attachDisplayAddBaseAlert",
    "#add-to-cart-confirmation",
)


async def add_to_cart_by_asin(asin: str, context: BrowserContext) -> bool:
    """Add an item to the Amazon cart by ASIN. Returns True if successful."""
    page = await context.new_page()
    try:
        await page.goto(f"https://www.amazon.com/dp/{asin}", timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        add_btn = page.locator(
            "#add-to-cart-button, input[name='submit.add-to-cart'], #submit\\.add-to-cart-announce"
        ).first
        await add_btn.click(timeout=10_000)

        # Do NOT wait for networkidle — Amazon pages keep loading ads/telemetry and
        # never go idle, so that wait always times out even when the add succeeded.
        # Instead wait for any confirmation signal, then fall back to the cart count.
        try:
            await page.locator(", ".join(_ATC_CONFIRM_SELECTORS)).first.wait_for(
                state="visible", timeout=8_000
            )
            return True
        except Exception:
            pass

        # Fallback: the nav cart-count badge went above zero.
        try:
            count_txt = (
                await page.locator("#nav-cart-count").first.text_content(timeout=2_000)
            ) or "0"
            if re.search(r"[1-9]", count_txt):
                return True
        except Exception:
            pass

        logger.warning("Add-to-cart for %s: clicked but no confirmation detected", asin)
        return False
    except Exception as exc:
        logger.warning("add_to_cart failed for ASIN %s: %s", asin, exc)
        return False
    finally:
        await page.close()


async def get_cart_total(context: BrowserContext) -> float | None:
    """Navigate to cart and read the current subtotal."""
    page = await context.new_page()
    try:
        await page.goto("https://www.amazon.com/gp/cart/view.html", timeout=15_000)
        await page.wait_for_load_state("domcontentloaded")

        subtotal_el = page.locator("#sc-subtotal-amount-activecart, #sc-subtotal-label-activecart").first
        text = (await subtotal_el.text_content(timeout=5_000) or "").strip()
        m = re.search(r"\$([\d,]+\.?\d*)", text)
        return float(m.group(1).replace(",", "")) if m else None
    except Exception as exc:
        logger.warning("get_cart_total failed: %s", exc)
        return None
    finally:
        await page.close()


# Note: we intentionally do not drive Amazon's "Proceed to checkout" flow. That
# produces a /gp/buy/spc/ URL bound to this Playwright session, which forces the
# user to re-authenticate when opened on their own device. Instead the checkout
# activity hands back AMAZON_CART_URL, which resolves against the user's own
# signed-in Amazon (web or app) where the staged items already live.
