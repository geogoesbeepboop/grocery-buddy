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

AMAZON_FRESH_URL = "https://www.amazon.com/fmc/storefront"
AMAZON_GROCERY_SEARCH = "https://www.amazon.com/s?i=grocery&k={query}"


async def get_browser_context() -> tuple:
    """Return (playwright, context) with the persistent Amazon session."""
    profile_dir = Path(settings.amazon_profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

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
        await page.goto("https://www.amazon.com/gp/css/homepage.html", timeout=15_000)
        await page.wait_for_load_state("domcontentloaded")
        # If we see "Hello, sign in" the session is gone
        sign_in_visible = await page.locator("text=Hello, sign in").is_visible()
        return not sign_in_visible
    except Exception as exc:
        logger.warning("Login check failed: %s", exc)
        return False
    finally:
        await page.close()


async def search_grocery_price(product: str, context: BrowserContext) -> dict | None:
    """Search Amazon Grocery for a product and return price + ASIN.

    Returns None if the product could not be found or priced.
    """
    page = await context.new_page()
    try:
        url = AMAZON_GROCERY_SEARCH.format(query=product.replace(" ", "+"))
        await page.goto(url, timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        # Grab first result with a price
        result_locator = page.locator('[data-component-type="s-search-result"]').first
        await result_locator.wait_for(timeout=8_000)

        # Price
        price_whole = result_locator.locator(".a-price-whole").first
        price_frac = result_locator.locator(".a-price-fraction").first

        whole_text = (await price_whole.text_content(timeout=5_000) or "").strip().replace(",", "")
        frac_text = (await price_frac.text_content(timeout=5_000) or "00").strip()

        if not whole_text:
            return None

        price_usd = float(f"{whole_text}.{frac_text}")

        # Title
        title_el = result_locator.locator("h2 a span").first
        title = (await title_el.text_content(timeout=5_000) or product).strip()

        # ASIN from href
        link_el = result_locator.locator("h2 a").first
        href = await link_el.get_attribute("href") or ""
        asin = None
        m = re.search(r"/dp/([A-Z0-9]{10})", href)
        if m:
            asin = m.group(1)

        logger.info("Amazon price for %r: $%.2f (ASIN=%s)", product, price_usd, asin)
        return {"product": title, "price_usd": price_usd, "asin": asin, "source": "amazon_scraped"}

    except Exception as exc:
        logger.warning("Amazon price lookup failed for %r: %s", product, exc)
        return None
    finally:
        await page.close()


async def add_to_cart_by_asin(asin: str, context: BrowserContext) -> bool:
    """Add an item to the Amazon cart by ASIN. Returns True if successful."""
    page = await context.new_page()
    try:
        await page.goto(f"https://www.amazon.com/dp/{asin}", timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        add_btn = page.locator("#add-to-cart-button, #submit.a-button-input").first
        await add_btn.click(timeout=8_000)
        await page.wait_for_load_state("networkidle", timeout=10_000)

        # Confirm it was added
        confirmation = page.locator("text=Added to Cart, text=Added to cart").first
        success = await confirmation.is_visible(timeout=5_000)
        return success
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


async def proceed_to_checkout(context: BrowserContext) -> str | None:
    """Click 'Proceed to checkout' and return the current checkout URL.

    Does NOT complete the purchase — returns the URL for final human review
    or automated form submission (guarded by idempotency check in the activity).
    """
    page = await context.new_page()
    try:
        await page.goto("https://www.amazon.com/gp/cart/view.html", timeout=15_000)
        await page.wait_for_load_state("domcontentloaded")

        checkout_btn = page.locator("input[name='proceedToRetailCheckout'], [data-feature-id='proceed-to-checkout-action']").first
        await checkout_btn.click(timeout=8_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        return page.url
    except Exception as exc:
        logger.warning("proceed_to_checkout failed: %s", exc)
        return None
    finally:
        await page.close()
