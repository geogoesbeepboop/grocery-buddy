"""Outbound notifications — Telegram only.

All agent-to-user messages go through Telegram:
  - morning briefing with item list + inline approve/reject buttons
  - purchase confirmation
  - error alerts

The webhook server's /telegram route handles the reverse direction (user → agent).

Setup:
  1. Create a bot with @BotFather, copy TELEGRAM_BOT_TOKEN.
  2. DM the bot once, then read your TELEGRAM_CHAT_ID from:
       https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Register the webhook (once):
       curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WEBHOOK_BASE_URL>/telegram"
"""
from __future__ import annotations

import logging

import httpx

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)


def telegram_enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def send_telegram_message(
    text: str,
    buttons: list[dict] | None = None,
    parse_mode: str = "HTML",
) -> None:
    """Send a message to the configured Telegram chat.

    ``buttons`` is a list of inline-keyboard buttons laid out as a single row.
    Each is ``{"text": str, "callback_data": str}`` for a callback button or
    ``{"text": str, "url": str}`` for a link button. No-op when Telegram isn't
    configured.
    """
    if not telegram_enabled():
        logger.warning("Telegram not configured — message not sent: %s", text[:80])
        return

    payload: dict = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if buttons:
        row = []
        for b in buttons:
            btn: dict = {"text": b["text"]}
            if b.get("url"):
                btn["url"] = b["url"]
            elif b.get("callback_data"):
                btn["callback_data"] = b["callback_data"]
            row.append(btn)
        payload["reply_markup"] = {"inline_keyboard": [row]}

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if not resp.is_success:
                logger.warning("Telegram API error %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


async def send_briefing(
    cart_id: str,
    total_usd: float,
    workflow_id: str,
    items: list[dict],
    reason: str | None = None,
) -> None:
    """Send the morning (or ad-hoc) grocery briefing with a full item breakdown.

    The message includes inline ✅/❌ buttons for simple approve/reject, but the
    user can also reply in plain text and the /telegram handler will interpret it
    (e.g. "yes and add bread", "skip the milk").

    ``items`` — list of dicts with keys: product, qty, unit, price_usd, notes
    (``notes`` holds the actual chosen Amazon listing — the brand/variant we'd buy).
    """
    # Compose a natural, grounded message (Haiku) instead of a stiff templated list.
    from grocery_buddy.agents.assistant import compose_briefing

    text = await compose_briefing(items, total_usd, reason)

    # callback_data is capped at 64 bytes by Telegram — a full workflow_id
    # ("approve:grocery-run-<uuid>-<hex>") overflows it and triggers
    # BUTTON_DATA_INVALID. Use the shorter cart_id; the handler resolves the
    # workflow_id from it.
    await send_telegram_message(
        text,
        buttons=[
            {"text": "✅ Looks good", "callback_data": f"approve:{cart_id}"},
            {"text": "❌ Skip", "callback_data": f"reject:{cart_id}"},
        ],
    )


async def send_checkout_link(
    cart_id: str,
    total_usd: float,
    checkout_url: str | None = None,
) -> None:
    """Tell the user their Amazon cart is staged and give them a checkout link.

    The agent does NOT place the order — it adds the approved items to the user's
    Amazon cart and the user finishes checkout themselves via this link.
    """
    cart_url = checkout_url or "https://www.amazon.com/gp/cart/view.html"
    msg = (
        f"🛒 <b>Your cart's ready — ${total_usd:.2f}</b>\n\n"
        "I've added everything to your Amazon cart. "
        "<b>Nothing's been bought yet</b> — tap below to open your cart "
        "(it opens right in your Amazon app where you're already signed in) "
        "and finish checkout."
    )
    buttons = [{"text": "🧾 Open my Amazon cart", "url": cart_url}]
    await send_telegram_message(msg, buttons=buttons)


async def send_error_notification(message: str) -> None:
    await send_telegram_message(f"⚠️ <b>Agent error</b>\n{message}")
