"""Self-healing Amazon sign-in for the persistent automation profile.

Two entry points, both operating on an already-launched persistent context (the
MAIN profile, so a successful login is saved for later runs):

  login_with_credentials(...)   Fill stored email/password, tick "keep me signed
                                in", handle a 2FA prompt via an injected callback,
                                step past passkey upsells, pick a household profile,
                                then verify we're actually in. Fully unattended.

  wait_for_interactive_login(...)  Open the sign-in page and poll until the user
                                completes login themselves (used when no credentials
                                are configured). Never touches the page the user is
                                typing into — it just watches for the signed-in state.

Amazon's auth UI churns, so every optional step is best-effort and wrapped; the
return value comes from a final, authoritative "are we signed in?" check rather
than from assuming any one click worked.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from playwright.async_api import BrowserContext, Page

from grocery_buddy.automation.amazon import _looks_signed_out, harden_page

logger = logging.getLogger(__name__)

# Landing on the orders page bounces a signed-out session to the sign-in form.
_ORDERS_URL = "https://www.amazon.com/gp/css/order-history"

# Optional steps Amazon may interleave after password/2FA — passkey or add-phone
# upsells with a "skip / not now / maybe later" escape. Best-effort, all optional.
_SKIP_UPSELL_SELECTORS = (
    "#ap-account-fixup-phone-skip-link",
    "a:has-text('Not now')",
    "a:has-text('Maybe later')",
    "button:has-text('Not now')",
    "input[aria-labelledby*='skip']",
    "#cvf-account-recovery-phone-skip-link",
)


async def _click_first(page: Page, selectors: list[str], timeout: int = 8_000) -> bool:
    """Click the first selector that resolves to a visible element. Best-effort."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def _check_if_present(page: Page, selector: str) -> None:
    """Tick a checkbox (keep-signed-in / remember-device) if it's there & unchecked."""
    try:
        box = page.locator(selector).first
        if await box.count() and not await box.is_checked():
            await box.check(timeout=3_000)
    except Exception:
        pass


async def _on_otp_step(page: Page) -> bool:
    """True if Amazon is currently asking for a one-time 2FA code."""
    try:
        if "/ap/mfa" in (page.url or "") or "ap/cvf" in (page.url or ""):
            return True
        otp = page.locator("#auth-mfa-otpcode, input[name='otpCode']").first
        return bool(await otp.count())
    except Exception:
        return False


async def _prefer_password_or_code(page: Page) -> None:
    """If Amazon shows a passkey-first chooser (no email/password box), steer to the
    one-time-code path — we can't satisfy a passkey, but we CAN relay a code.

    Best-effort: when the normal email/password fields are present we do nothing.
    """
    try:
        has_form = await page.locator(
            "#ap_email, #ap_email_login, #ap_password, input[name='email'], "
            "input[name='password']"
        ).first.count()
        if has_form:
            return
        clicked = await _click_first(page, [
            "a:has-text('Sign in with a code')",
            "input[aria-labelledby*='code']",
            "#ap-other-signin-button",
            "a:has-text('Other ways to sign in')",
        ], timeout=4_000)
        if clicked:
            await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass


async def _select_profile(page: Page, profile_name: str | None) -> None:
    """Click the household profile gate ("Who's shopping?"), if shown. Best-effort.

    Non-critical: the order scraper also narrows results by first name, so an
    un-selected profile doesn't break the import — this just lands us on the right
    one when the screen appears.
    """
    if not profile_name:
        return
    try:
        gate = page.locator(f"text={profile_name}").first
        if await gate.count() and await gate.is_visible():
            await gate.click(timeout=5_000)
            await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass


async def login_with_credentials(
    context: BrowserContext,
    *,
    email: str,
    password: str,
    get_otp: Callable[[], Awaitable[str | None]],
    profile_name: str | None = None,
) -> bool:
    """Sign the persistent ``context`` into Amazon with stored credentials.

    ``get_otp`` is awaited ONLY if Amazon asks for a 2FA code; it returns the code
    (e.g. relayed from the user over Telegram) or None to abort. Returns True iff a
    final check confirms we're signed in.
    """
    page = await context.new_page()
    try:
        # Suppress the native passkey/security-key dialog BEFORE navigating, so
        # Amazon falls back to the password + code flow we can drive.
        await harden_page(page)
        await page.goto(_ORDERS_URL, timeout=30_000)
        await page.wait_for_load_state("domcontentloaded")

        # Already in? (Session revived between the signed-out check and here.)
        if not await _looks_signed_out(page):
            return True

        # Wait for the auth UI to render — could be the email step, the password
        # step (email remembered), or a passkey-first chooser.
        try:
            await page.locator(
                "#ap_email, #ap_email_login, #ap_password, input[name='email'], "
                "input[name='password'], #ap-other-signin-button"
            ).first.wait_for(timeout=20_000)
        except Exception:
            pass
        await _prefer_password_or_code(page)

        # ── Email — only if Amazon is actually asking for it (it often remembers
        #    the email and jumps straight to the password page). ──
        pw_present = bool(await page.locator("#ap_password, input[name='password']").first.count())
        email_field = page.locator("#ap_email, #ap_email_login, input[name='email']").first
        email_visible = await email_field.count() and await email_field.is_visible()
        if email_visible and not pw_present:
            # Email-only step → fill and advance to the password page. Use the email
            # page's own Continue button (NOT a generic submit, which on a combined
            # page would be "Sign in" and fire before the password is entered).
            await email_field.fill(email)
            await _click_first(page, ["#continue", "input#continue", "#continue-announce"])
            await page.wait_for_load_state("domcontentloaded")
        elif email_visible:
            # Combined email+password page → just fill the email in place.
            try:
                await email_field.fill(email)
            except Exception:
                pass

        # ── Password (+ keep me signed in for a longer-lived session) ──
        pw_field = page.locator("#ap_password, input[name='password']").first
        if await pw_field.count():
            await pw_field.fill(password)
            await _check_if_present(page, "#rememberMe, input[name='rememberMe']")
            await _click_first(page, ["#signInSubmit", "input#signInSubmit", "#auth-signin-button",
                                      "input[type='submit']"])
            await page.wait_for_load_state("domcontentloaded")

        # ── Optional 2FA — relay the code from the user ──
        if await _on_otp_step(page):
            logger.info("Amazon requested a 2FA code — relaying to the user")
            code = await get_otp()
            if not code:
                logger.warning("No 2FA code provided in time — login aborted")
                return False
            await _check_if_present(page, "#auth-mfa-remember-device")
            otp_field = page.locator("#auth-mfa-otpcode, input[name='otpCode']").first
            await otp_field.fill(code)
            await _click_first(page, ["#auth-signin-button", "input#auth-signin-button",
                                      "input[type='submit']"])
            await page.wait_for_load_state("domcontentloaded")

        # ── Step past passkey / add-phone upsells, then any profile gate ──
        await _click_first(page, list(_SKIP_UPSELL_SELECTORS), timeout=4_000)
        await _select_profile(page, profile_name)

        # ── Authoritative verification ──
        await page.goto(_ORDERS_URL, timeout=30_000)
        await page.wait_for_load_state("domcontentloaded")
        ok = not await _looks_signed_out(page)
        logger.info("Credential login %s", "succeeded" if ok else "did not complete")
        return ok
    except Exception as exc:
        logger.warning("Credential login failed: %s", exc)
        return False
    finally:
        await page.close()


async def wait_for_interactive_login(
    context: BrowserContext,
    *,
    timeout_s: float = 240.0,
    poll_interval: float = 3.0,
    on_tick: Callable[[], None] | None = None,
) -> bool:
    """Open the sign-in page and wait for the user to log in themselves.

    Used when no credentials are configured: we surface a visible window, then poll
    the SAME page's signed-in state (without reloading, so we never disrupt the user
    mid-type). ``on_tick`` is called each poll for activity heartbeating. Returns
    True once signed in, False on timeout.
    """
    # Reuse the context's default tab so the user sees ONE window, not a stray
    # about:blank next to the sign-in tab.
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await harden_page(page)  # no passkey dialog blocking the user's sign-in
        await page.goto(_ORDERS_URL, timeout=30_000)
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass  # the user can still navigate/login; we just poll state

    waited = 0.0
    while waited < timeout_s:
        try:
            if not await _looks_signed_out(page):
                logger.info("Interactive login completed after %.0fs", waited)
                return True
        except Exception:
            pass  # page mid-navigation while the user clicks around — keep waiting
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        if on_tick is not None:
            try:
                on_tick()
            except Exception:
                pass
    logger.warning("Interactive login timed out after %.0fs", timeout_s)
    return False
