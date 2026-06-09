---
name: fix-amazon-selector
description: Debug and repair a broken Amazon Playwright selector in grocery-buddy when scraping (prices, search results, order history, add-to-cart, login/2FA) stops finding elements after Amazon changes its DOM. Use for "the scraper returns nothing / wrong prices / can't log in / add-to-cart fails / scraper-health is red".
---

# Fix a broken Amazon selector

Amazon A/B-tests and reshuffles its DOM constantly. The automation lives in
`src/grocery_buddy/automation/`:

- `amazon.py` — search/price extraction, add-to-cart, cart subtotal, order-history
  scraping (selectors mostly **inline**, as comma-separated fallback variants).
- `amazon_auth.py` — login/2FA/profile selectors grouped near the top as `_EMAIL_SEL`,
  `_PASSWORD_SEL`, `_OTP_SEL`, `_CONTINUE_SEL`, `_SIGNIN_SEL`, …
- `resilience.py` — the selector-health layer: wrappers that record when a selector
  variant misses / self-heals so churn is observable, plus `summarize_health`.
- `network.py` — request hardening / passkey suppression.

A break is often **silent** (a fallback chain returns `[]`, fewer items, or a default).
So "returns nothing / fewer items than expected" is a selector break, not just empty data —
and that's exactly what the health layer + `scraper-health` exist to catch early.

## The debug loop

1. **First, confirm it's actually broken:**
   ```bash
   grocery-buddy scraper-health     # searches known staples; asserts price + ASIN extract
   ```
   This is the proactive probe (also a money-live gate condition, see `docs/EVALS.md` §5).
   Red here = a selector regressed.

2. **Reproduce headed**, watching the real browser:
   ```bash
   AMAZON_HEADLESS=false uv run python scripts/debug_order_scrape.py \
       --name "<FirstName>" --max-orders 30 --max-pages 3
   ```
   Drives the same code path and prints scraped orders as JSON. `AMAZON_HEADLESS=false`
   makes any flow watchable.

3. **See what's dropped.** Skipped results log at DEBUG (`logger.debug("Skipping result …")`)
   and `resilience.py` records selector misses — raise log verbosity to find where the chain
   falls through.

4. **Read Amazon's current DOM.** With the headed browser on the failing page, inspect the
   element (or `await page.content()` / `page.screenshot()` near the locator). The connected
   **Playwright MCP** (`mcp__playwright__*`: `browser_navigate`, `browser_snapshot`,
   `browser_evaluate`) confirms a candidate selector against the live page before you edit.

5. **Repair, preserving robustness.** Add the new selector as **another variant** beside the
   old ones (don't delete — Amazon serves multiple layouts at once); keep the
   comma-separated / fallback-chain idiom and route it through the `resilience.py` helpers so
   the health signal stays accurate. Prefer stable hooks (`#ids`, `[data-component-type]`,
   `[data-asin]`, structural XPath like `ancestor::*[contains(@class,'a-fixed-left-grid')]`)
   over brittle generated class names.

## Login / 2FA

`amazon_auth.py` is a state machine over the sign-in pages with a Telegram 2FA relay
(`get_otp`). Creds come from `AMAZON_EMAIL`/`AMAZON_PASSWORD`; the session persists in
`.amazon-session/` (never commit it). If sign-in selectors moved, fix the `_*_SEL` groups and
`make amazon-setup` (headed) to confirm a clean login.

## Verify

- `grocery-buddy scraper-health` is green again, and the headed `debug_order_scrape.py`
  returns correct items/prices.
- For cart/price paths, add-to-cart still detects its confirmation (`_ATC_CONFIRM_SELECTORS`)
  and the subtotal reads.
- `uv run pytest -q` (selector parsing is covered by `tests/test_order_scrape.py` and
  `tests/test_resilience.py` against synthetic HTML — extend them with the new layout).
- **Never** touch the checkout invariant in here: we add to cart and stop — we never drive
  "Place order" (`CLAUDE.md`).

## Example dev workflow

> Nightly `scraper-health` pages you: price extraction returns `None` on staples. You run
> the headed `debug_order_scrape.py`, see Amazon renamed `span.a-price` → a new class, snapshot
> it with the Playwright MCP, add the new variant to the price selectors in `amazon.py` (old
> one kept), extend `tests/test_resilience.py` with the new markup, and confirm `scraper-health`
> goes green.

The whole loop can be handed to the **`selector-fixer`** subagent.
