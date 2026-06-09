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
import random
import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from grocery_buddy.automation import network as net
from grocery_buddy.automation import resilience as R
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

# A real-ish desktop UA so Amazon serves its standard (scrapeable) layout.
AMAZON_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Chromium launch args shared by every context we open. Three groups:
#   1. Automation basics (sandbox off, automation flag hidden).
#   2. Crash/session-restore suppressors — matter most for the scraper, which
#      launches a COPY of the auth profile that may have been snapshotted
#      mid-session (e.g. the `make amazon-setup` browser left open); without these
#      Chrome pops a "restore pages?" bubble or reopens old tabs, wedging the first
#      navigation with the opaque "'dict' object has no attribute '_object'" error.
#   3. Password-manager / keychain suppressors — keep Chrome's OWN popups ("save
#      password?", "protect passwords with your screen lock", macOS keychain
#      access) from interrupting an automated sign-in. These fire at the browser
#      level, independent of the Amazon page, so flags (+ profile prefs below) are
#      the only way to silence them.
CHROMIUM_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection,"
    "BiometricAuthenticationForPasswordFilling,AutofillEnableAccountStorage,"
    "OptimizationGuideModelDownloading",
]


def _disable_password_manager_prefs(profile_dir: Path) -> None:
    """Patch a profile's Preferences so Chrome's password manager stays quiet.

    Disabling the credentials service is the authoritative kill switch for the
    "save password?" and "protect passwords with your screen lock" popups that
    otherwise overlay Amazon's sign-in form. Settings-only — never touches cookies
    or the saved session. Best-effort and idempotent; runs before launch (when
    nothing holds the profile) so there's no write conflict.
    """
    import json

    prefs_path = profile_dir / "Default" / "Preferences"
    try:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if prefs_path.exists():
            try:
                data = json.loads(prefs_path.read_text() or "{}")
            except (ValueError, OSError):
                data = {}
        data["credentials_enable_service"] = False
        data["credentials_enable_autosignin"] = False
        profile = data.setdefault("profile", {})
        profile["password_manager_enabled"] = False
        prefs_path.write_text(json.dumps(data))
    except Exception as exc:
        logger.debug("Could not patch password-manager prefs (%s)", exc)


async def harden_page(page: Page) -> None:
    """Neutralize Amazon's passkey / security-key prompt for an automated page.

    Amazon's sign-in page (``?openid.pape.max_auth_age=0``) auto-invokes WebAuthn,
    which pops Chrome's NATIVE "insert your security key and touch it" dialog — a
    browser-chrome element Playwright can't dismiss, so the flow just hangs. Enabling
    Chrome's virtual WebAuthn environment replaces that native UI with a virtual
    authenticator holding no credentials: ``navigator.credentials.get()`` fails fast
    and Amazon falls back to the password + one-time-code flow we can actually drive.

    Must run BEFORE the page navigates to the sign-in URL. Best-effort and silent —
    if CDP isn't available the page still works, just without the suppression.
    """
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("WebAuthn.enable")
        await cdp.send(
            "WebAuthn.addVirtualAuthenticator",
            {
                "options": {
                    "protocol": "ctap2",
                    "transport": "internal",
                    "hasResidentKey": True,
                    "hasUserVerification": True,
                    "isUserVerified": False,
                    "automaticPresenceSimulation": True,
                }
            },
        )
    except Exception as exc:
        logger.debug("WebAuthn hardening skipped (%s)", exc)


async def get_browser_context(headless: bool | None = None) -> tuple:
    """Return (playwright, context) with the persistent Amazon session.

    ``headless`` overrides ``AMAZON_HEADLESS`` for this launch — the self-healing
    re-login forces a visible window when it needs the user to sign in by hand.
    """
    profile_dir = Path(settings.amazon_profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = settings.amazon_headless if headless is None else headless
    _disable_password_manager_prefs(profile_dir)

    logger.info(
        "Launching Amazon browser (headless=%s, profile=%s) — "
        "set AMAZON_HEADLESS=false to watch it work",
        headless,
        profile_dir,
    )
    p = await async_playwright().start()
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=CHROMIUM_LAUNCH_ARGS,
        user_agent=AMAZON_USER_AGENT,
    )
    return p, context


# Markers of Amazon's sign-in / passkey wall. When the saved session expires,
# navigating to the orders page bounces to /ap/signin (or shows a passkey prompt)
# and the scraper would otherwise sit there reading 0 orders. We detect this so
# /import can tell the user to re-authenticate instead of silently dead-ending.
_SIGNIN_URL_MARKERS = ("/ap/signin", "/ap/login", "/ap/mfa", "ap/cvf")
_SIGNIN_FORM_SELECTORS = (
    "#ap_email", "#ap_password", "input[name='email']", "form[name='signIn']",
    "#auth-fpp-link-bottom",  # passkey / "sign in another way" wall
)


async def _looks_signed_out(page: Page) -> bool:
    """True if the page is Amazon's sign-in/passkey wall or the signed-out home.

    Covers both failure modes: a redirect to /ap/signin (the orders page bounces
    here when the saved session has expired) and the "Hello, sign in" nav on a
    homepage that still rendered. Best-effort — never raises.
    """
    try:
        url = page.url or ""
        if any(m in url for m in _SIGNIN_URL_MARKERS):
            return True
        for sel in _SIGNIN_FORM_SELECTORS:
            if await page.locator(sel).first.count():
                return True
        if await page.locator(
            "#nav-link-accountList-nav-line-1:has-text('Sign in')"
        ).first.count():
            return True
    except Exception:
        return False
    return False


async def is_signed_out(context: BrowserContext) -> bool:
    """Positively detect that the saved Amazon session has expired.

    Loads the order-history page — the thing /import actually needs — and returns
    True ONLY when it clearly lands on the sign-in / passkey wall. On a navigation
    error (network blip, slow load) it returns False so the caller treats that as a
    normal, retryable hiccup rather than telling the user to re-authenticate.
    """
    page = await context.new_page()
    try:
        await harden_page(page)  # suppress the passkey dialog if it bounces to sign-in
        await page.goto("https://www.amazon.com/gp/css/order-history", timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")
        return await _looks_signed_out(page)
    except Exception as exc:
        logger.warning("Sign-in check inconclusive (%s) — assuming still signed in", exc)
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
    # Opportunistic JSON capture — a hedge for the day Amazon moves search off SSR
    # HTML onto a client-rendered JSON endpoint; meanwhile a no-cost health signal.
    collector = net.JsonResponseCollector(("/s/query", "/api/", "search-alias", "data/aod"))
    collector.attach(page)
    if settings.amazon_block_heavy_resources:
        await net.block_heavy_resources(page)
    try:
        url = AMAZON_GROCERY_SEARCH.format(query=product.replace(" ", "+"))
        await page.goto(url, timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        # Let the (server-rendered) result cards land before we resolve the anchor,
        # so a not-yet-rendered page can't spuriously trip the repair path.
        try:
            await page.wait_for_selector(
                '[data-component-type="s-search-result"], div.s-result-item[data-asin]',
                timeout=15_000,
            )
        except Exception:
            pass
        await collector.drain()

        # Resolve the result-card anchor through the resilient/self-healing path:
        # stable data-attributes first, ARIA role as a churn-resistant fallback, and
        # an LLM-over-accessibility-tree repair if every variant comes up empty.
        result_locator = await R.resolve(
            page, "search.results",
            [
                R.css('[data-component-type="s-search-result"]'),
                R.css("div.s-result-item[data-asin]"),
                R.css('[data-asin]:not([data-asin=""])'),
                R.role("listitem"),
            ],
            page=page,
            describe=(
                "A single product result card in an Amazon grocery search results "
                "list — it contains the product title link and the price."
            ),
            critical=True,
        )
        if result_locator is None:
            logger.warning("Amazon search for %r found no result cards", product)
            return []

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
                # types. ARIA heading role is the churn-resistant fallback. Fall back
                # to the generic product name rather than crashing.
                title = product
                title_loc = await R.first_matching(result, [
                    R.css("h2 a span"), R.css("h2 span"),
                    R.css("[data-cy='title-recipe'] span"),
                    R.css(".a-size-medium"), R.css(".a-size-base-plus"),
                    R.role("heading"),
                ], require_visible=True)
                if title_loc is not None:
                    raw = await title_loc.first.text_content(timeout=1_500)
                    if raw and raw.strip():
                        title = raw.strip()

                candidates.append({
                    "product": title,
                    "price_usd": price_usd,
                    "asin": asin,
                    "source": "amazon_scraped",
                })
            except Exception as exc:
                logger.debug("Skipping result %d for %r: %s", i, product, exc)
                continue

        # Health: cards rendered (count>0) but nothing priced/extracted (candidates==0)
        # is the signature of price/title selector drift — flag it as a degraded miss
        # even though the page "worked", so a partial redesign doesn't stay silent.
        R.observe(
            "search.extract", matched=len(candidates), critical=False,
            note=(f"{count} cards rendered but 0 priced — price/title selectors may have "
                  "drifted") if (count and not candidates) else "",
        )
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
    """Add an item to the Amazon cart by ASIN. Returns True if successful.

    Confirmation comes primarily from Amazon's own cart-mutation NETWORK RESPONSE,
    which fires regardless of which confirmation-overlay id the current layout uses —
    far more stable than waiting on one of seven overlay CSS ids. The overlay and the
    nav cart-count are kept as fallbacks. We deliberately do NOT resource-block or
    JSON-mine this money-path page; the response observation is passive.
    """
    page = await context.new_page()
    try:
        await page.goto(f"https://www.amazon.com/dp/{asin}", timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        # Deterministic ids first, then an accessible-name fallback ("Add to Cart").
        # We deliberately pass NO ``describe`` here, which disables LLM repair on this
        # intent: self-healing is safe for reads, but letting an LLM free-pick *what to
        # click* on a checkout page risks it choosing "Buy Now"/"Place order" and
        # bypassing the cart-URL human handoff. A genuine 0-match records a critical
        # miss and pages the user instead — the safe failure on the money path.
        add_btn = await R.resolve(
            page, "atc.button",
            [
                R.css("#add-to-cart-button"),
                R.css("input[name='submit.add-to-cart']"),
                R.css("#submit\\.add-to-cart-announce"),
                R.role("button", "add to cart"),
            ],
            critical=True,
            require_visible=True,
        )
        if add_btn is None:
            logger.warning("Add-to-cart for %s: no add-to-cart button found", asin)
            return False

        async def _click() -> None:
            await add_btn.first.click(timeout=10_000)

        # Click inside the network window so the cart-mutation response is our signal.
        if await net.confirm_add_to_cart(page, _click):
            R.observe("atc.confirm", matched=1, critical=True, note="network")
            return True

        # Network inconclusive — fall back to the visual confirmation overlay, then
        # the nav cart-count. (Do NOT wait for networkidle: ad/telemetry keeps these
        # pages from ever going idle even when the add succeeded.)
        try:
            confirm = await R.first_matching(page, [R.css(s) for s in _ATC_CONFIRM_SELECTORS])
            if confirm is not None:
                await confirm.first.wait_for(state="visible", timeout=8_000)
                R.observe("atc.confirm", matched=1, critical=True, note="overlay")
                return True
        except Exception:
            pass

        try:
            count_loc = await R.first_matching(
                page, [R.css("#nav-cart-count"), R.css("#nav-cart-count-container")]
            )
            count_txt = (await count_loc.first.text_content(timeout=2_000)) if count_loc else "0"
            if count_txt and re.search(r"[1-9]", count_txt):
                R.observe("atc.confirm", matched=1, critical=True, note="cart-count")
                return True
        except Exception:
            pass

        R.observe("atc.confirm", matched=0, critical=True,
                  note="clicked but no network/overlay/count confirmation")
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

        subtotal = await R.resolve(
            page, "cart.subtotal",
            [
                R.css("#sc-subtotal-amount-activecart"),
                R.css("#sc-subtotal-label-activecart"),
                R.css("#sc-subtotal-amount-buybox"),
                R.css("[data-name='Subtotal'] .a-price .a-offscreen"),
            ],
            page=page,
            describe=("The cart subtotal amount on the Amazon shopping-cart page — a "
                      "dollar figure labeled 'Subtotal'."),
            critical=False,
        )
        if subtotal is None:
            logger.warning("get_cart_total: subtotal element not found")
            return None
        text = (await subtotal.first.text_content(timeout=5_000) or "").strip()
        m = re.search(r"\$([\d,]+\.?\d*)", text)
        return float(m.group(1).replace(",", "")) if m else None
    except Exception as exc:
        logger.warning("get_cart_total failed: %s", exc)
        return None
    finally:
        await page.close()


# Active-cart line items and their per-item Delete controls. Amazon's cart markup
# has churned across redesigns, so we union the stable variants. _CART_ITEM_CSS is
# kept to plain CSS (no Playwright :has-text/:not pseudo-classes) so the exact same
# string can be reused inside a querySelectorAll wait below.
_CART_ITEM_CSS = (
    "#sc-active-cart div.sc-list-item, "
    "form#activeCartViewForm div.sc-list-item, "
    "div[data-name='Active Cart'] div.sc-list-item, "
    "div.sc-list-item[data-asin]"
)
# Delete control INSIDE a line item — targeted specifically so we never click
# "Save for later" (which only moves an item, leaving it to resurface later).
_CART_DELETE_SELECTORS = (
    "[data-feature-id='item-delete-button'] input",
    "input[data-feature-id='item-delete-button']",
    "span.sc-action-delete input",
    "input[value='Delete']",
    "input[name^='submit.delete']",
    "[aria-label='Delete']",
)
# Safety cap on the delete loop so an unexpected layout can never spin forever.
_CLEAR_CART_MAX_ITERATIONS = 40
_CLEAR_CART_DELETE_WAIT_MS = 8_000


async def _count_cart_items(page: Page) -> int:
    """Count line items in the ACTIVE Amazon cart (0 means empty)."""
    try:
        return await page.locator(_CART_ITEM_CSS).count()
    except Exception:
        return 0


async def clear_cart(context: BrowserContext) -> bool:
    """Empty the live Amazon cart, removing every active line item.

    The Amazon cart is real, persistent, account-scoped state: it survives across
    staged checkouts and is shared with the user's own shopping. Checkout staging
    must start from an empty cart so the staged cart is EXACTLY this run's approved
    items — otherwise a previously staged-but-unbought run, or leftovers from a
    crashed earlier attempt, stack on top and the user can over-order.

    Removes items one at a time (clicking each row's Delete control and waiting for
    the cart to shrink) until none remain. Returns True iff the cart is empty when
    we finish — including the already-empty case — and False if items still remain
    after a best effort, so a caller can refuse to stage onto an uncertain cart.

    NOTE: this clears the WHOLE active cart, including anything the user added
    manually — the agent treats the Amazon cart as its own staging area.
    """
    page = await context.new_page()
    try:
        await page.goto(AMAZON_CART_URL, timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")

        remaining = await _count_cart_items(page)
        if remaining == 0:
            return True  # already empty — nothing to clear, no health event

        for _ in range(_CLEAR_CART_MAX_ITERATIONS):
            if remaining == 0:
                R.observe("cart.clear", matched=1, critical=True, note="emptied")
                return True

            # Scope the Delete control to the first active item so we can't pick up
            # a Delete that belongs to "Saved for later" or some other section. We
            # resolve deterministically and pass NO describe — like add-to-cart we
            # never let an LLM free-pick what to click on the money path.
            item = page.locator(_CART_ITEM_CSS).first
            delete_btn = await R.first_matching(
                item, [R.css(s) for s in _CART_DELETE_SELECTORS]
            )
            if delete_btn is None:
                R.observe("cart.clear", matched=0, critical=True,
                          note=f"{remaining} item(s) but no Delete control")
                logger.warning(
                    "clear_cart: %d item(s) but no Delete control found — stopping",
                    remaining,
                )
                return False
            try:
                await delete_btn.first.click(timeout=10_000)
            except Exception as exc:
                R.observe("cart.clear", matched=0, critical=True, note="delete click failed")
                logger.warning("clear_cart: delete click failed (%s)", exc)
                return False

            # Removing an item either reloads the cart (classic layout) or reflows
            # it in place (smart cart). Wait for the line-item count to actually
            # drop — handles both, and avoids the networkidle wait that never
            # settles on Amazon's telemetry-heavy pages.
            try:
                await page.wait_for_function(
                    "({n, sel}) => document.querySelectorAll(sel).length < n",
                    arg={"n": remaining, "sel": _CART_ITEM_CSS},
                    timeout=_CLEAR_CART_DELETE_WAIT_MS,
                )
            except Exception:
                pass  # fall through; the no-progress guard below decides

            new_count = await _count_cart_items(page)
            if new_count >= remaining:
                R.observe("cart.clear", matched=0, critical=True,
                          note=f"no progress ({new_count} remain)")
                logger.warning(
                    "clear_cart: no progress (%d item(s) remain) — stopping", new_count
                )
                return False
            remaining = new_count

        # Exhausted the iteration cap — report the final state honestly.
        empty = await _count_cart_items(page) == 0
        R.observe("cart.clear", matched=1 if empty else 0, critical=True,
                  note="hit iteration cap; " + ("empty" if empty else "items remain"))
        return empty
    except Exception as exc:
        logger.warning("clear_cart failed: %s", exc)
        return False
    finally:
        await page.close()


# Note: we intentionally do not drive Amazon's "Proceed to checkout" flow. That
# produces a /gp/buy/spc/ URL bound to this Playwright session, which forces the
# user to re-authenticate when opened on their own device. Instead the checkout
# activity hands back AMAZON_CART_URL, which resolves against the user's own
# signed-in Amazon (web or app) where the staged items already live.


# ── Order-history scraping (onboarding import) ────────────────────────────────
#
# We scrape the ORDERS SEARCH listing (your-orders/search?search=<first name>)
# rather than visiting each order-detail page. Two reasons:
#   1. Profile scoping — an Amazon account can host several household profiles, so
#      the raw orders list mixes everyone's purchases. Typing the user's first name
#      into the orders search box narrows it to their own orders.
#   2. Robustness + speed — the search listing already shows each order's date and
#      its items inline, so we read them straight off the cards. Navigating into
#      each order-detail page instead was slow (one page-load per order) and tended
#      to fall through to Amazon's "related products" carousels, scraping random
#      recommendations with no order date (the source of the earlier null-date noise).

# Listing URLs, paginated by startIndex (Amazon pages orders in tens). We seed the
# first page with startIndex; for subsequent pages we follow Amazon's own
# pagination link when present and only fall back to constructing startIndex.
# opt=ab matches the search box's "all orders" mode.
_ORDERS_SEARCH_URL = (
    "https://www.amazon.com/your-orders/search?opt=ab&search={query}&startIndex={start}"
)
_ORDERS_LIST_URL = "https://www.amazon.com/your-orders/orders?startIndex={start}"
_ORDERS_PAGE_SIZE = 10

# Captcha / throttle markers. If Amazon decides we're a bot mid-pagination, bail
# out gracefully and keep whatever we already scraped rather than hammering on.
_THROTTLE_MARKERS = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "sorry, we just need to make sure you're not a robot",
    "api-services-support@amazon.com",
)

# Button / CTA text that appears inside product cards with /dp/ links but is NOT
# a product title. Checked case-insensitively against the anchor's trimmed text.
_NON_TITLE_ANCHORS = frozenset({
    "buy it again", "view order details", "view or edit order", "track package",
    "get product support", "write a product review", "leave seller feedback",
    "return or replace items", "return items", "problem with order", "get help",
    "share", "view return/refund status", "ask product question",
    "leave delivery feedback", "archive order", "view invoice", "order details",
    "add to cart", "add to list", "buy again", "get it again", "view return status",
    "track shipment", "leave a review", "write a review", "reorder",
})

_ORDER_DATE_RE = re.compile(
    r"(?:Order placed|Placed on|Ordered on)?\s*"
    r"((?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.I,
)

# Per-item quantity, when Amazon renders it as text on the card ("Qty: 2").
_QTY_RE = re.compile(r"(?:Qty|Quantity)\s*:?\s*(\d{1,3})", re.I)


def _parse_order_date(text: str) -> str | None:
    m = _ORDER_DATE_RE.search(text or "")
    return m.group(1).strip() if m else None


async def get_scraper_context() -> tuple:
    """Return (playwright, context, temp_dir) for read-only order-history scraping.

    The import scraper needs the auth cookies from the main session, but cannot
    share the same profile directory — Chrome refuses to open a profile already
    locked by another instance (``BrowserType.launch_persistent_context: Opening
    in existing browser session``). This function:

      1. Copies the main session profile to a fresh temp directory, skipping
         large cache directories so the copy is fast.
      2. Removes Chrome's SingletonLock / Default/LOCK files from the copy so
         the new instance can open it.
      3. Launches a persistent context against the temp copy.

    The caller MUST clean up after itself:
        await context.close(); await p.stop(); shutil.rmtree(str(temp_dir))
    """
    import shutil
    import tempfile

    source_dir = Path(settings.amazon_profile_dir).resolve()
    temp_dir = Path(tempfile.mkdtemp(prefix="gb-scraper-"))

    if source_dir.exists():
        try:
            shutil.copytree(
                str(source_dir),
                str(temp_dir),
                ignore=shutil.ignore_patterns(
                    "Cache", "Code Cache", "GPUCache", "ShaderCache",
                    "DawnCache", "GrShaderCache", "BrowserMetrics*", "Crashpad",
                    # Singleton + session-restore state. If the source profile is
                    # snapshotted mid-session (the setup browser still open), these
                    # files point at a live instance / make the copy try to restore
                    # the old tabs — which wedges navigation with the opaque
                    # "'dict' object has no attribute '_object'" Playwright error.
                    # Auth cookies live elsewhere, so dropping these is safe.
                    "Singleton*", "Sessions", "Session Storage",
                    "Current Session", "Current Tabs", "Last Session", "Last Tabs",
                ),
                dirs_exist_ok=True,
            )
            # Belt-and-suspenders: remove any lock/session files that slipped through
            # so the copy always opens cleanly even if the source was mid-write.
            for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie",
                              "lockfile", "Default/LOCK", "Default/Current Session",
                              "Default/Current Tabs"):
                lock_path = temp_dir / lock_name
                try:
                    if lock_path.exists() or lock_path.is_symlink():
                        lock_path.unlink()
                except OSError:
                    pass
        except Exception as exc:
            logger.warning("Profile copy incomplete (%s) — scraper may need login", exc)

    _disable_password_manager_prefs(temp_dir)
    logger.info(
        "Launching scraper browser (headless=%s, temp_profile=%s)",
        settings.amazon_headless, temp_dir,
    )
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(temp_dir),
        headless=settings.amazon_headless,
        args=CHROMIUM_LAUNCH_ARGS,
        user_agent=AMAZON_USER_AGENT,
    )
    return pw, ctx, temp_dir


def _orders_page_url(search_name: str | None, start: int) -> str:
    """Build the orders-listing URL for the given pagination offset.

    With a first name we use the search endpoint (profile-scoped); without one we
    fall back to the full unfiltered orders list.
    """
    if search_name:
        return _ORDERS_SEARCH_URL.format(query=search_name.strip().replace(" ", "+"), start=start)
    return _ORDERS_LIST_URL.format(start=start)


async def _get_order_cards(page: Page):
    """Return a locator over the order cards on the current listing page.

    Amazon's order card class has churned across redesigns; try the variants in
    order of specificity and return the first that matches anything.
    """
    for sel in (
        "div.order-card.js-order-card",
        "li.order-card",
        "div.js-order-card",
        "div.order-card",
        "div.order",                       # classic layout
        "div.a-box-group.order",
    ):
        loc = page.locator(sel)
        if await loc.count():
            return loc
    return None


async def _extract_items_from_card(card) -> list[dict]:
    """Pull the ordered products out of one order card.

    Each product is a /dp/ (or /gp/product/) anchor inside the card. We skip the
    card's action links ("Buy it again", "Track package", …) and read a per-item
    quantity when Amazon prints one ("Qty: 2"), defaulting to 1.
    """
    items: list[dict] = []
    seen: set[str] = set()

    links = card.locator("a[href*='/dp/'], a[href*='/gp/product/']")
    for i in range(await links.count()):
        link = links.nth(i)
        try:
            title = (await link.text_content(timeout=1_500) or "").strip()
            href = (await link.get_attribute("href") or "").strip()
        except Exception:
            continue

        if not title or len(title) < 5:
            continue  # icon/thumbnail links have empty or tiny text
        if title.lower() in _NON_TITLE_ANCHORS:
            continue

        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href)
        asin = m.group(1) if m else None
        key = asin or title[:60].lower()
        if key in seen:
            continue
        seen.add(key)

        # Quantity, when Amazon renders it as text near the item. The visual
        # multiplier badge on the thumbnail isn't reliable text, so we read the
        # explicit "Qty: N" label if present and otherwise assume a single unit.
        qty = 1
        try:
            container = link.locator(
                "xpath=ancestor::*[contains(@class,'a-fixed-left-grid') "
                "or contains(@class,'item-box') or contains(@class,'shipment')][1]"
            ).first
            ctext = (await container.text_content(timeout=1_000) or "") if await container.count() else ""
            qm = _QTY_RE.search(ctext)
            if qm:
                qty = max(1, int(qm.group(1)))
        except Exception:
            pass

        items.append({"title": title, "asin": asin, "qty": qty})

    return items


async def _extract_qty(scope) -> int:
    """Best-effort per-item quantity from a row/card scope (defaults to 1).

    Tries the explicit "Qty: N" label first, then the small numeric badge Amazon
    overlays on a multi-unit item's thumbnail.
    """
    try:
        txt = (await scope.text_content(timeout=1_000) or "")
    except Exception:
        txt = ""
    m = _QTY_RE.search(txt)
    if m:
        return max(1, int(m.group(1)))
    for sel in ("[class*='qty']", "[class*='Qty']", "[class*='quantity']",
                "[class*='Quantity']", "span.a-badge-text"):
        try:
            el = scope.locator(sel).first
            if await el.count():
                t = (await el.text_content(timeout=800) or "").strip()
                if t.isdigit() and 1 <= int(t) <= 99:
                    return int(t)
        except Exception:
            continue
    return 1


async def _scrape_item_rows(page: Page) -> list[dict]:
    """Scrape the your-orders/search results, which list one item per row.

    Each result row shows "Ordered on <date>", the product link, a "Buy it again"
    button, and a quantity badge. We anchor on the product links (robust to
    Amazon's churning container classes) and read the date + qty from each link's
    closest ancestor that carries an "Ordered on"/"Order placed" label — which
    also filters out any /dp/ links that aren't real order results (recommendation
    carousels, nav, etc., have no such ancestor).
    """
    orders: list[dict] = []
    seen: set[str] = set()

    links = page.locator("a[href*='/dp/'], a[href*='/gp/product/']")
    for i in range(await links.count()):
        link = links.nth(i)
        try:
            title = (await link.text_content(timeout=1_500) or "").strip()
            href = (await link.get_attribute("href") or "").strip()
        except Exception:
            continue

        if not title or len(title) < 5:
            continue  # image/thumbnail anchors have empty text
        if title.lower() in _NON_TITLE_ANCHORS:
            continue

        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href)
        asin = m.group(1) if m else None
        base = asin or title[:60].lower()

        # Closest ancestor that carries the order-date label = this item's row.
        # If there isn't one, this link isn't a real order result → skip it.
        row = link.locator(
            "xpath=ancestor::*[contains(., 'Ordered on') or contains(., 'Order placed')][1]"
        ).first
        try:
            if not await row.count():
                continue
            row_text = (await row.text_content(timeout=1_500) or "")
        except Exception:
            continue
        # A genuine single-item row is small; a huge blob means we climbed past the
        # row into a shared container, so we can't trust the date/qty — skip.
        if len(row_text) > 2_500 or not _ORDER_DATE_RE.search(row_text):
            continue
        # Structural guard: if the matched ancestor wraps more than one distinct
        # product, we climbed past this item's row into a shared container (e.g.
        # the page body, which also contains a date) — can't attribute the date, so
        # skip. This is what keeps recommendation carousels and the like out.
        row_asins: set[str] = set()
        prod_links = row.locator("a[href*='/dp/'], a[href*='/gp/product/']")
        for j in range(await prod_links.count()):
            h = (await prod_links.nth(j).get_attribute("href") or "")
            mm = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", h)
            if mm:
                row_asins.add(mm.group(1))
        if len(row_asins) > 1:
            continue

        # Dedupe by product+date, NOT product alone: the same staple bought on
        # several dates is the strongest reorder signal, so we must keep each
        # occurrence. We only collapse a true duplicate (same product, same date),
        # which is just the image+title anchors of one row or a paging echo.
        order_date = _parse_order_date(row_text)
        key = f"{base}@{order_date or '?'}"
        if key in seen:
            continue

        # The qty badge lives on the thumbnail, in a sibling column of the date —
        # so read it from the full item grid (token-exact 'a-fixed-left-grid', not
        # its '-col'/'-inner' variants) which wraps both columns. Fall back to the
        # date row if that layout isn't present.
        qty_scope = link.locator(
            "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '),"
            " ' a-fixed-left-grid ')][1]"
        ).first
        qty = await _extract_qty(qty_scope if await qty_scope.count() else row)

        seen.add(key)
        orders.append({
            "order_date": order_date,
            "items": [{"title": title, "asin": asin, "qty": qty}],
        })

    return orders


async def _scrape_orders_on_page(page: Page) -> list[dict]:
    """Extract every order (date + items) visible on the current listing page.

    Handles two layouts: the multi-item order cards (orders list) and the
    one-item-per-row search results. Tries cards first, falls back to rows.
    """
    cards = await _get_order_cards(page)
    if cards is not None:
        orders: list[dict] = []
        for i in range(await cards.count()):
            card = cards.nth(i)
            try:
                card_text = (await card.text_content(timeout=2_000) or "")[:600]
            except Exception:
                card_text = ""
            order_date = _parse_order_date(card_text)
            items = await _extract_items_from_card(card)
            if items:
                orders.append({"order_date": order_date, "items": items})
        if orders:
            R.observe("orders.page", matched=len(orders), critical=False)
            return orders

    # Search-results layout (one item per row) — the /import default.
    rows = await _scrape_item_rows(page)
    R.observe("orders.page", matched=len(rows), critical=False)
    return rows


async def _get_next_page_url(page: Page) -> str | None:
    """Return the URL of the pagination 'Next' control, or None if it's the last page."""
    for sel in (
        "ul.a-pagination li.a-last:not(.a-disabled) a",
        ".a-pagination .a-last:not(.a-disabled) a",
        "li.a-last:not(.a-disabled) a",
        "a.a-last",
    ):
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                href = (await el.get_attribute("href") or "").strip()
                if href:
                    return href if href.startswith("http") else "https://www.amazon.com" + href
        except Exception:
            continue

    # ARIA fallback: a link whose accessible name is "Next" — survives the
    # a-pagination class churn.
    try:
        el = page.get_by_role("link", name=re.compile(r"\bnext\b", re.I)).first
        if await el.count() and await el.is_visible():
            href = (await el.get_attribute("href") or "").strip()
            if href:
                return href if href.startswith("http") else "https://www.amazon.com" + href
    except Exception:
        pass
    return None


async def _looks_throttled(page: Page) -> bool:
    try:
        text = (await page.locator("body").text_content(timeout=3_000) or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in _THROTTLE_MARKERS)


async def scrape_order_history(
    context: BrowserContext,
    search_name: str | None = None,
    max_pages: int = 20,
    max_orders: int = 200,
    page_delay_range: tuple[float, float] = (2.0, 4.5),
) -> list[dict]:
    """Scrape the user's Amazon order history off the orders SEARCH listing.

    When ``search_name`` is given we query ``your-orders/search?search=<name>`` so
    results are scoped to that household profile; otherwise we read the full orders
    list. Items and dates are read straight off each order card — no per-order
    navigation — and we paginate by ``startIndex``.

    To stay polite across long histories (100+ orders) we sleep a randomized
    ``page_delay_range`` seconds between pages and stop early if Amazon shows a
    captcha/throttle page. Returns at most ``max_orders`` orders, each::

        {"order_date": str | None,
         "items": [{"title": str, "asin": str | None, "qty": int}]}

    Best-effort — returns whatever it managed to read; never raises.
    """
    page = await context.new_page()
    # Read-only path: kill the images/ads/telemetry that otherwise keep these pages
    # loading forever, so each of (potentially) 20 pages settles faster.
    if settings.amazon_block_heavy_resources:
        await net.block_heavy_resources(page)
    # Opportunistic JSON capture — a hedge for the day Amazon moves order history off
    # SSR HTML; also a health signal for whether any JSON endpoint fired.
    collector = net.JsonResponseCollector(("/your-orders/", "/order-history", "/api/", "css/order"))
    collector.attach(page)
    orders: list[dict] = []
    seen_keys: set[str] = set()  # dedupe items across pages; also detects looping

    url = _orders_page_url(search_name, 0)
    try:
        for page_num in range(max_pages):
            await page.goto(url, timeout=30_000)
            await page.wait_for_load_state("domcontentloaded")
            # Wait for actual order content to render before scraping. The fixed
            # short sleep alone raced the page load, which is why the first attempt
            # sometimes scraped 0 orders and only the Temporal retry succeeded.
            try:
                await page.wait_for_selector(
                    "a[href*='/dp/'], a[href*='/gp/product/'], "
                    "div.order-card, div.js-order-card",
                    timeout=15_000,
                )
            except Exception:
                pass  # genuinely empty results, or a different layout — scrape decides
            await page.wait_for_timeout(800)
            await collector.drain()

            if await _looks_throttled(page):
                logger.warning(
                    "Amazon showed a captcha/throttle on page %d — stopping with %d orders",
                    page_num + 1, len(orders),
                )
                break

            page_orders = await _scrape_orders_on_page(page)
            # First-page emptiness is the make-or-break signal (login expired, or the
            # order-row anchor/XPath stopped matching after a redesign) — record it as
            # a critical miss so the activity layer pages the user instead of the old
            # silent empty import.
            if page_num == 0:
                R.observe(
                    "orders.firstpage", matched=len(page_orders), critical=True,
                    note=("first listing page had no orders — login expired or the order "
                          "selectors drifted") if not page_orders else "",
                )
            if not page_orders:
                # First page empty usually means we never reached the order list
                # (login expired, or the search matched nothing). Later empty pages
                # just mean we've run past the end of the history.
                if page_num == 0:
                    logger.warning(
                        "No orders on the first listing page — check login state%s",
                        f" or that the name {search_name!r} matches a profile" if search_name else "",
                    )
                else:
                    logger.info("No more orders after page %d — stopping", page_num)
                break

            # Keep only (product, date) pairs we haven't captured on an earlier
            # page. Keying on product+date preserves a staple bought repeatedly on
            # different dates (the reorder signal) while still detecting a page that
            # echoes an earlier one — if a page adds nothing new we've looped back or
            # reached the end, so we stop.
            new_items = 0
            for order in page_orders:
                date = order["order_date"]
                fresh = [
                    it for it in order["items"]
                    if f"{it['asin'] or it['title'][:60].lower()}@{date or '?'}" not in seen_keys
                ]
                for it in fresh:
                    seen_keys.add(f"{it['asin'] or it['title'][:60].lower()}@{date or '?'}")
                if fresh:
                    orders.append({"order_date": date, "items": fresh})
                    new_items += len(fresh)
                if len(orders) >= max_orders:
                    break

            logger.debug(
                "Page %d: %d new items (running total %d orders)",
                page_num + 1, new_items, len(orders),
            )
            if new_items == 0:
                logger.info("Page %d added nothing new — reached the end, stopping", page_num + 1)
                break
            if len(orders) >= max_orders:
                break

            # Advance to the next page. Prefer Amazon's own pagination link
            # ("page 1, 2, 3…"); fall back to constructing the next startIndex if the
            # page looked full but exposed no link.
            next_url = await _get_next_page_url(page)
            if not next_url:
                if len(page_orders) >= _ORDERS_PAGE_SIZE:
                    next_url = _orders_page_url(search_name, (page_num + 1) * _ORDERS_PAGE_SIZE)
                else:
                    logger.info("No further pages after page %d — stopping", page_num + 1)
                    break
            url = next_url

            # Polite, jittered pause before the next page so we don't look like a
            # tight scraping loop. The agent taking its time here is fine.
            await page.wait_for_timeout(int(random.uniform(*page_delay_range) * 1000))

        logger.info(
            "Scraped %d orders from Amazon history%s",
            len(orders), f" (search={search_name!r})" if search_name else "",
        )
    except Exception as exc:
        logger.warning("Order history scrape stopped early: %s", exc)

    await page.close()
    return orders[:max_orders]
