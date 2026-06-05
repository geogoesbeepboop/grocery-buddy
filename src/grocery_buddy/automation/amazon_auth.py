"""Self-healing Amazon sign-in for the persistent automation profile.

Two entry points, both operating on an already-launched persistent context (the
MAIN profile, so a successful login is saved for later runs):

  login_with_credentials(...)   Drive the sign-in form with stored email/password
                                as a small STATE MACHINE: each pass detects which
                                step Amazon is showing (email / password / 2FA /
                                passkey chooser / upsell / profile gate) and acts
                                on it, re-checking after every transition. Every
                                field write is VERIFIED (we read the value back and
                                retry, typing it char-by-char if a programmatic
                                fill doesn't stick — Amazon's JS sometimes clears
                                an autofilled field). Fully unattended; a 2FA code
                                is relayed via an injected callback.

  wait_for_interactive_login(...)  Open the sign-in page and poll until the user
                                completes login themselves (used when no credentials
                                are configured). Never touches the page the user is
                                typing into — it just watches for the signed-in state.

Amazon's auth UI churns and sequences its steps differently across A/B variants,
so we never assume a fixed order or that any one click worked: the loop re-detects
the live step each pass and the return value comes from a final, authoritative
"are we signed in?" check.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from playwright.async_api import BrowserContext, Locator, Page

from grocery_buddy.automation.amazon import _looks_signed_out, harden_page

logger = logging.getLogger(__name__)

# Landing on the orders page bounces a signed-out session to the sign-in form.
_ORDERS_URL = "https://www.amazon.com/gp/css/order-history"

# Field selectors, grouped by step. Amazon varies element ids across layouts, so
# each is a comma-joined set of the variants we've seen.
_EMAIL_SEL = "#ap_email, #ap_email_login, input[type='email'], input[name='email']"
_PASSWORD_SEL = "#ap_password, input[name='password']"
_OTP_SEL = "#auth-mfa-otpcode, input[name='otpCode']"

# Buttons that advance each step.
_CONTINUE_SEL = ("#continue", "input#continue", "#continue-announce",
                 "button#continue", "input[type='submit']")
_SIGNIN_SEL = ("#signInSubmit", "input#signInSubmit", "#auth-signin-button",
               "input#auth-signin-button", "input[type='submit']")

# Inline error/warning boxes Amazon renders when it rejects an email or password
# (wrong password, account locked, etc.). If one of these is showing while we're
# still parked on the same field, retrying won't help — we bail with the reason
# rather than resubmitting (which risks locking the account).
_AUTH_ERROR_SEL = (
    "#auth-error-message-box",
    "#auth-warning-message-box",
    "div.a-alert-error",
)

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


async def _click_first(page: Page, selectors, timeout: int = 8_000) -> bool:
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


async def _is_visible(locator: Locator) -> bool:
    """True if the locator resolves to a present, visible element. Never raises."""
    try:
        return bool(await locator.count()) and await locator.is_visible()
    except Exception:
        return False


async def _value_of(locator: Locator) -> str:
    """Current value of an input, or '' if it can't be read."""
    try:
        return await locator.input_value(timeout=3_000)
    except Exception:
        return ""


async def _fill_verified(page: Page, locator: Locator, value: str, label: str,
                         *, timeout: int = 10_000) -> bool:
    """Wait for a field to be editable, enter ``value``, and confirm it stuck.

    This is the crux of the robustness fix: a plain ``fill`` can silently no-op
    when the field isn't ready yet, or Amazon's JS can wipe an autofilled value
    right after. So we wait for visibility, fill, then READ THE VALUE BACK; if it
    doesn't match we retry, falling back to character-by-character typing (which
    fires the same input/keyboard events a human would and survives Amazon's
    autofill scrubbing). Returns True only once the field actually holds ``value``.
    """
    try:
        await locator.wait_for(state="visible", timeout=timeout)
    except Exception:
        logger.warning("Login: the %s field never became visible", label)
        return False

    for attempt in range(1, 4):
        # Pass 1: fast programmatic fill (clears any prefill first).
        try:
            await locator.fill(value, timeout=8_000)
        except Exception as exc:
            logger.debug("Login: %s fill attempt %d errored: %s", label, attempt, exc)
        if await _value_of(locator) == value:
            return True

        # Pass 2: clear and type it like a human — survives JS that rejects a
        # programmatic fill or re-clears an autofilled value.
        try:
            await locator.click(timeout=4_000)
            await locator.fill("", timeout=4_000)
            await locator.press_sequentially(value, delay=45, timeout=8_000)
        except Exception as exc:
            logger.debug("Login: %s type attempt %d errored: %s", label, attempt, exc)
        if await _value_of(locator) == value:
            return True

        await page.wait_for_timeout(300)

    logger.warning("Login: couldn't enter the %s after 3 attempts", label)
    return False


async def _click_and_settle(page: Page, selectors, label: str) -> bool:
    """Click the first matching advance button, then wait for the page to react.

    Amazon's email→password and password→next transitions can be a full
    navigation OR an in-place re-render, so we wait for ``domcontentloaded`` (best
    effort) plus a short settle so the next step's fields exist before the loop
    re-detects. Returns True if a button was actually clicked.
    """
    if not await _click_first(page, selectors):
        logger.debug("Login: no %s button found (%s)", label, selectors)
        return False
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(700)  # let the next step's JS render its fields
    return True


async def _check_if_present(page: Page, selector: str) -> None:
    """Tick a checkbox (keep-signed-in / remember-device) if it's there & unchecked."""
    try:
        box = page.locator(selector).first
        if await box.count() and not await box.is_checked():
            await box.check(timeout=3_000)
    except Exception:
        pass


async def _auth_error(page: Page) -> str | None:
    """Return Amazon's visible sign-in error text, if it's showing one."""
    for sel in _AUTH_ERROR_SEL:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                text = (await el.text_content(timeout=2_000) or "").strip()
                if text:
                    return " ".join(text.split())
        except Exception:
            continue
    return None


async def _on_otp_step(page: Page) -> bool:
    """True if Amazon is currently asking for a one-time 2FA code."""
    try:
        if "/ap/mfa" in (page.url or "") or "ap/cvf" in (page.url or ""):
            return True
        return bool(await page.locator(_OTP_SEL).first.count())
    except Exception:
        return False


async def _prefer_password_or_code(page: Page) -> bool:
    """If Amazon shows a passkey-first chooser (no email/password box), steer to the
    one-time-code path — we can't satisfy a passkey, but we CAN relay a code.

    Returns True if it clicked something. Best-effort: when the normal
    email/password fields are present we do nothing.
    """
    try:
        if await page.locator(f"{_EMAIL_SEL}, {_PASSWORD_SEL}").first.count():
            return False
        clicked = await _click_first(page, [
            "a:has-text('Sign in with a code')",
            "input[aria-labelledby*='code']",
            "#ap-other-signin-button",
            "a:has-text('Other ways to sign in')",
            "a:has-text('sign in another way')",
        ], timeout=4_000)
        if clicked:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            await page.wait_for_timeout(500)
        return clicked
    except Exception:
        return False


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


async def _handle_otp_step(page: Page, get_otp: Callable[[], Awaitable[str | None]]) -> bool:
    """Relay a 2FA code from the user and submit it. Returns True if a code was entered."""
    logger.info("Amazon requested a 2FA code — relaying to the user")
    code = await get_otp()
    if not code:
        logger.warning("No 2FA code provided in time — login aborted")
        return False
    await _check_if_present(page, "#auth-mfa-remember-device")
    otp_field = page.locator(_OTP_SEL).first
    if not await _fill_verified(page, otp_field, code, "2FA code", timeout=8_000):
        return False
    await _click_and_settle(page, _SIGNIN_SEL, "verify-code")
    return True


async def login_with_credentials(
    context: BrowserContext,
    *,
    email: str,
    password: str,
    get_otp: Callable[[], Awaitable[str | None]],
    profile_name: str | None = None,
) -> bool:
    """Sign the persistent ``context`` into Amazon with stored credentials.

    Drives the sign-in form as a bounded state machine: each pass detects the live
    step and acts, re-checking after every transition (Amazon sequences email →
    password → 2FA → passkey/phone upsell → profile gate differently across
    variants, and a field can be in the DOM before it's actually fillable). Every
    field write is verified. ``get_otp`` is awaited ONLY if Amazon asks for a 2FA
    code; it returns the code (e.g. relayed over Telegram) or None to abort.

    Returns True iff a final, authoritative check confirms we're signed in.
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

        # Wait for SOME auth UI to render — email, password, OTP, or a chooser.
        try:
            await page.locator(
                f"{_EMAIL_SEL}, {_PASSWORD_SEL}, {_OTP_SEL}, #ap-other-signin-button"
            ).first.wait_for(timeout=20_000)
        except Exception:
            pass

        otp_attempts = 0
        # Bounded so a stuck/looping page can never spin forever. Each iteration
        # advances at most one step; ~10 covers the deepest real flow (chooser →
        # email → password → 2FA → upsell → profile) with headroom.
        for step in range(12):
            signed_out = await _looks_signed_out(page)
            on_otp = await _on_otp_step(page)
            if not signed_out and not on_otp:
                break  # we're through the wall

            logger.debug("Login step %d: url=%s otp=%s", step, page.url, on_otp)

            # ── 2FA — relay a code from the user (Amazon may show this after the
            #    password, or directly if it remembers the password). ──
            if on_otp:
                if otp_attempts >= 2:
                    logger.warning("2FA still failing after %d attempts — aborting", otp_attempts)
                    return False
                otp_attempts += 1
                if not await _handle_otp_step(page, get_otp):
                    return False
                continue

            email_field = page.locator(_EMAIL_SEL).first
            password_field = page.locator(_PASSWORD_SEL).first
            email_vis = await _is_visible(email_field)
            password_vis = await _is_visible(password_field)

            # ── Password step (the gate that was silently skipped before). On a
            #    combined email+password page, make sure the email is set first. ──
            if password_vis:
                if email_vis and not await _value_of(email_field):
                    await _fill_verified(page, email_field, email, "email", timeout=4_000)
                if not await _fill_verified(page, password_field, password, "password"):
                    continue  # field vanished mid-write (page moved) — re-detect
                await _check_if_present(page, "#rememberMe, input[name='rememberMe']")
                await _click_and_settle(page, _SIGNIN_SEL, "sign-in")
                # Rejected outright? Don't resubmit a bad password in a loop.
                if await _is_visible(password_field) and (err := await _auth_error(page)):
                    logger.warning("Amazon rejected the password: %s", err)
                    return False
                continue

            # ── Email-only step → fill and advance to the password page. ──
            if email_vis:
                if not await _fill_verified(page, email_field, email, "email"):
                    continue
                await _click_and_settle(page, _CONTINUE_SEL, "continue")
                if await _is_visible(email_field) and (err := await _auth_error(page)):
                    logger.warning("Amazon rejected the email: %s", err)
                    return False
                continue

            # ── No form visible → passkey chooser, an upsell, a profile gate, or a
            #    still-loading interstitial. Steer/skip, then let the loop re-detect. ──
            steered = await _prefer_password_or_code(page)
            skipped = await _click_first(page, list(_SKIP_UPSELL_SELECTORS), timeout=3_000)
            await _select_profile(page, profile_name)
            if not (steered or skipped):
                await page.wait_for_timeout(1_500)

        # ── Step past any trailing upsell / profile gate, then verify. ──
        await _click_first(page, list(_SKIP_UPSELL_SELECTORS), timeout=3_000)
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
