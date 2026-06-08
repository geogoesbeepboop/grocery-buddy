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


# Telegram rejects any message body over 4096 chars with 400 "message is too long".
# We send rich, sometimes-long lists (a whole pantry, an import proposal), so rather
# than letting the whole thing fail silently we split oversized text on natural
# boundaries and send it as a sequence of messages. Leave headroom under the limit.
TELEGRAM_MAX_CHARS = 4096
_CHUNK_LIMIT = 3900


def _split_for_telegram(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Split ``text`` into ``<=limit``-char chunks on line boundaries.

    Each line is kept intact so balanced inline HTML (our renderers always open and
    close a tag within one line) is never cut mid-tag. A single line longer than the
    limit is hard-split as a last resort. Always returns at least one chunk.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            # A single monster line — flush what we have, then hard-split it.
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _button_row(buttons: list[dict]) -> dict:
    """Build a single-row inline-keyboard reply_markup from button dicts."""
    row = []
    for b in buttons:
        btn: dict = {"text": b["text"]}
        if b.get("url"):
            btn["url"] = b["url"]
        elif b.get("callback_data"):
            btn["callback_data"] = b["callback_data"]
        row.append(btn)
    return {"inline_keyboard": [row]}


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

    Long messages are split into several sends (Telegram caps a message at 4096
    chars); any buttons attach to the final chunk so they sit beneath the whole
    message.
    """
    if not telegram_enabled():
        logger.warning("Telegram not configured — message not sent: %s", text[:80])
        return

    chunks = _split_for_telegram(text)
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for idx, chunk in enumerate(chunks):
                payload: dict = {
                    "chat_id": settings.telegram_chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                }
                if buttons and idx == len(chunks) - 1:
                    payload["reply_markup"] = _button_row(buttons)
                resp = await client.post(url, json=payload)
                if not resp.is_success:
                    logger.warning("Telegram API error %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


# The commands Telegram should autocomplete when the user types "/". Keep this in
# sync with the routes handled in webhook.py (hidden ops commands are omitted).
_BOT_COMMANDS = [
    {"command": "import", "description": "Build your pantry from recent Amazon orders"},
    {"command": "status", "description": "See your pantry, waiting list, and schedule"},
    {"command": "start", "description": "Set up your pantry by hand"},
    {"command": "help", "description": "What I can do"},
]


async def register_bot_commands() -> None:
    """Publish the slash-command menu so commands autocomplete in Telegram.

    Best-effort and idempotent — safe to call on every startup. No-op when
    Telegram isn't configured.
    """
    if not telegram_enabled():
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setMyCommands"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"commands": _BOT_COMMANDS})
            if not resp.is_success:
                logger.warning("setMyCommands error %s: %s", resp.status_code, resp.text[:200])
            else:
                logger.info("Registered %d Telegram bot commands", len(_BOT_COMMANDS))
    except Exception as exc:
        logger.warning("setMyCommands failed: %s", exc)


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
    Amazon cart and the user finishes checkout themselves via this link. The
    "✅ I placed the order" button (and a plain-text "ordered"/"done" reply) closes
    the loop: it tells us the items are on the way so we update the pantry, mark them
    in-transit, and stop re-suggesting them until they arrive.
    """
    cart_url = checkout_url or "https://www.amazon.com/gp/cart/view.html"
    msg = (
        f"🛒 <b>Your cart's ready — ${total_usd:.2f}</b>\n\n"
        "I've added everything to your Amazon cart. "
        "<b>Nothing's been bought yet</b> — tap <b>Open my Amazon cart</b> "
        "(it opens right in your Amazon app where you're already signed in) "
        "and finish checkout.\n\n"
        "Once you've placed the order, tap <b>I placed the order</b> (or just reply "
        "<i>\"ordered\"</i>) and I'll add these to your pantry and stop suggesting them "
        "while they're on the way."
    )
    buttons = [
        {"text": "🧾 Open my Amazon cart", "url": cart_url},
        {"text": "✅ I placed the order", "callback_data": f"confirmed:{cart_id}"},
    ]
    await send_telegram_message(msg, buttons=buttons)


async def send_arrival_notification(landed: list[dict]) -> None:
    """Tell the user an in-transit order arrived and the pantry was topped up.

    ``landed`` — the lines just reconciled, each ``{product, qty, unit}``.
    """
    if not landed:
        return
    lines = ["📦 <b>Looks like your order landed — pantry topped up</b>"]
    for it in landed:
        name = (it.get("product") or "item").strip()
        qty = float(it.get("qty") or 1)
        unit = (it.get("unit") or "").strip()
        qty_str = f" (+{qty:g} {unit})".rstrip() if (qty != 1 or unit) else ""
        lines.append(f"• {name}{qty_str}")
    lines.append(
        "If anything didn't actually arrive, just tell me (e.g. <i>\"the milk never came\"</i>) "
        "and I'll fix it."
    )
    await send_telegram_message("\n".join(lines))


async def send_error_notification(message: str) -> None:
    await send_telegram_message(f"⚠️ <b>Agent error</b>\n{message}")
