"""Interactive script to save an Amazon login session for the automation module.

Run once on your local machine (headless=False so you can see and interact):
  uv run python scripts/setup_amazon_session.py

Logs in, handles 2FA if needed, then saves the session profile to
.amazon-session/ so subsequent Playwright runs are pre-authenticated.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = Path(".amazon-session").resolve()
AMAZON_URL = "https://www.amazon.com"


async def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Saving session to: {PROFILE_DIR}")
    print("A browser window will open. Log in to Amazon, then close the browser.")

    p = await async_playwright().start()
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        slow_mo=200,
    )
    page = await context.new_page()
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
