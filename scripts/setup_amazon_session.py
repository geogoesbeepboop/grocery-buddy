"""Interactive script to save an Amazon login session for the automation module.

Run once on your local machine (headless=False so you can see and interact):
  uv run python scripts/setup_amazon_session.py

Logs in, handles 2FA if needed, then saves the session profile to
.amazon-session/ so subsequent Playwright runs are pre-authenticated.

If a browser is already open on this profile (e.g. a previous setup window you
left running), Chrome refuses to open it twice and Playwright surfaces an opaque
"'dict' object has no attribute '_object'" crash. We detect that up front and
tell you to close the other window instead of dumping a traceback.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from grocery_buddy.automation.amazon import (
    AMAZON_USER_AGENT,
    CHROMIUM_LAUNCH_ARGS,
    harden_page,
)

PROFILE_DIR = Path(".amazon-session").resolve()
AMAZON_URL = "https://www.amazon.com"


def _looks_already_open() -> bool:
    """Heuristic: a live Chrome on this profile leaves a SingletonLock symlink."""
    lock = PROFILE_DIR / "SingletonLock"
    return lock.exists() or lock.is_symlink()


async def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Saving session to: {PROFILE_DIR}")

    if _looks_already_open():
        print(
            "\n⚠️  A browser may already be open on this profile "
            f"({PROFILE_DIR}).\n"
            "    Chrome won't open the same profile twice — close any existing\n"
            "    Amazon setup window, then re-run `make amazon-setup`.\n"
            "    (If you're sure none is open, the lock is stale; delete\n"
            f"    {PROFILE_DIR / 'SingletonLock'} and try again.)\n"
        )

    print("A browser window will open. Log in to Amazon, then close the browser.")

    p = await async_playwright().start()
    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            slow_mo=200,
            args=CHROMIUM_LAUNCH_ARGS,
            user_agent=AMAZON_USER_AGENT,
        )
    except Exception as exc:
        await p.stop()
        msg = str(exc)
        if "_object" in msg or "existing browser session" in msg.lower():
            print(
                "\n❌ Couldn't open the login browser — a Chrome window for this\n"
                "   profile is already open. Close it and re-run `make amazon-setup`.",
                file=sys.stderr,
            )
        else:
            print(f"\n❌ Couldn't launch the login browser: {exc}", file=sys.stderr)
        sys.exit(1)

    page = await context.new_page()
    await harden_page(page)  # no passkey/security-key dialog blocking the login
    await page.goto(AMAZON_URL)

    print("\n👉  Log in to Amazon now. Close the browser when done.\n")

    # Wait until the browser is closed
    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass

    await context.close()
    await p.stop()
    print("Session saved. You can now run the agent with AMAZON_HEADLESS=true")


if __name__ == "__main__":
    asyncio.run(main())
