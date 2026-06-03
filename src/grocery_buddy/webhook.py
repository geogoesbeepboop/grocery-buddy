"""FastAPI webhook server.

Endpoints
─────────
POST /telegram       Telegram Bot API webhook — inbound text + button callbacks.
GET  /health         Liveness check.

The old /approve/{workflow_id} and /reject/{workflow_id} HTTP endpoints that
ntfy used are removed. Approval flows exclusively through Telegram inline
buttons (callback_data "approve:{workflow_id}" / "reject:{workflow_id}").

Telegram flow
─────────────
Button tap (callback_query)
  → parse action:workflow_id from callback_data
  → signal Temporal workflow

Free-text message
  → check DB for a pending cart for this user
  → if pending cart: parse_briefing_reply(text, cart_items)
      approve         → signal approve
      reject          → signal reject
      approve_and_add → signal approve + start QuickBuyWorkflow for extras
      reject_and_buy  → signal reject + start QuickBuyWorkflow for subset
      chat            → send reply text
  → else: parse_request(text)
      quick_buy → start QuickBuyWorkflow
      chat      → send reply text

Setup (one-time)
────────────────
  curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WEBHOOK_BASE_URL>/telegram"
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from temporalio.client import Client

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup: detect user state and proactively start the right conversation."""
    from grocery_buddy.notifications import send_telegram_message, telegram_enabled

    if not telegram_enabled():
        logger.warning("Telegram not configured — no startup ping sent")
        yield
        logger.info("Webhook server shutting down")
        return

    user_id = settings.grocery_buddy_user_id
    if not user_id:
        logger.warning("GROCERY_BUDDY_USER_ID not set — skipping startup message")
        yield
        logger.info("Webhook server shutting down")
        return

    try:
        from grocery_buddy.db import get_pool
        from grocery_buddy.tools.conversation import is_first_time
        from grocery_buddy.tools.schedule import describe_cadence, describe_next_run, get_schedule

        pool = await get_pool()
        first_time = await is_first_time(pool, user_id)

        if first_time:
            # Proactively start the onboarding interview without waiting for the user
            logger.info("First-time user detected — starting proactive onboarding")
            await _reset_and_start_onboarding(user_id)
        else:
            # Returning user — show schedule status
            sched = await get_schedule(pool, user_id)
            if sched and sched.get("enabled"):
                cron = sched["cadence"]
                tz = sched.get("timezone", "America/New_York")
                cadence = describe_cadence(cron)
                next_run = describe_next_run(cron, tz)
                sched_line = (
                    f"Your briefing runs <b>{cadence}</b>.\n"
                    f"Next one: <b>{next_run}</b>."
                )
            else:
                sched_line = (
                    "You don't have a recurring schedule set yet.\n"
                    "Say <i>\"run my briefing every day at 8am\"</i> to set one."
                )

            await send_telegram_message(
                "👋 <b>Grocery Buddy is online.</b>\n\n"
                f"{sched_line}\n\n"
                "Say <i>\"I need eggs early\"</i> to order something now, "
                "or <i>\"/status\"</i> to see your pantry."
            )
            logger.info("Startup greeting sent to chat %s", settings.telegram_chat_id)

    except Exception as exc:
        logger.error("Startup message failed: %s", exc)
        await send_telegram_message(
            "🤖 Grocery Buddy is online. (Startup check failed — try /status)"
        )

    yield
    logger.info("Webhook server shutting down")


app = FastAPI(title="grocery-buddy webhook", lifespan=lifespan)

_temporal_client: Client | None = None


# ── Temporal helpers ──────────────────────────────────────────────────────────


async def _get_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
    return _temporal_client


async def _signal(workflow_id: str, signal_name: str) -> None:
    client = await _get_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(signal_name)
    logger.info("Signaled workflow %s → %s", workflow_id, signal_name)


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _resolve_workflow_id(token: str) -> str:
    """Map an approve/reject button token back to a Temporal workflow_id.

    Buttons carry the cart_id (it fits Telegram's 64-byte callback_data limit;
    the full workflow_id doesn't). Legacy buttons that still carry a workflow_id
    aren't valid UUIDs, so we pass those through unchanged.
    """
    from grocery_buddy.db import get_pool

    try:
        cart_uuid = uuid.UUID(token)
    except ValueError:
        return token  # already a workflow_id (older button)

    pool = await get_pool()
    wf = await pool.fetchval("SELECT workflow_id FROM carts WHERE id = $1", cart_uuid)
    return wf or token


async def _get_pending_cart(user_id: str) -> dict | None:
    """Return the most recent pending-approval cart + its items, or None."""
    from grocery_buddy.db import get_pool

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, workflow_id, total_usd
        FROM carts
        WHERE user_id = $1 AND status = 'pending_approval'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        uuid.UUID(user_id),
    )
    if not row:
        return None

    cart_id = str(row["id"])
    items = await pool.fetch(
        "SELECT product, qty, unit, price_usd, notes FROM cart_items WHERE cart_id = $1",
        uuid.UUID(cart_id),
    )
    return {
        "cart_id": cart_id,
        "workflow_id": row["workflow_id"],
        "total_usd": float(row["total_usd"] or 0),
        "items": [dict(i) for i in items],
    }


# ── Auth ──────────────────────────────────────────────────────────────────────


def _authorized(chat_id: str) -> bool:
    """Ignore updates from any chat that isn't the configured one."""
    expected = str(settings.telegram_chat_id)
    if expected and chat_id != expected:
        logger.warning("Ignored Telegram update from unauthorized chat %s", chat_id)
        return False
    return True


# ── QuickBuy helper ───────────────────────────────────────────────────────────


async def _start_quick_buy(items: list[dict], reason: str) -> str:
    """Start a QuickBuyWorkflow and return its workflow_id."""
    from grocery_buddy.models import QuickBuyInput, QuickBuyItem
    from grocery_buddy.workflows.quick_buy import QuickBuyWorkflow

    user_id = settings.grocery_buddy_user_id
    buy_items = [
        QuickBuyItem(
            product=i["product"],
            qty=i["qty"],
            unit=i["unit"],
            preferred_brand=i.get("preferred_brand"),
        )
        for i in items
    ]
    wf_id = f"quick-buy-{user_id}-{uuid.uuid4().hex[:8]}"
    client = await _get_client()
    await client.start_workflow(
        QuickBuyWorkflow.run,
        QuickBuyInput(user_id=user_id, items=buy_items, reason=reason),
        id=wf_id,
        task_queue=settings.temporal_task_queue,
    )
    return wf_id


async def _start_grocery_run(user_id: str, trigger: str = "manual") -> str:
    """Kick off a full GroceryRunWorkflow (predict → price → briefing).

    ``trigger`` is "onboarding"/"manual" for user-initiated runs (bypass cooldown,
    always report back) or "schedule" for the recurring briefing.
    """
    from grocery_buddy.models import GroceryRunInput
    from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow

    wf_id = f"grocery-run-{user_id}-{uuid.uuid4().hex[:8]}"
    client = await _get_client()
    await client.start_workflow(
        GroceryRunWorkflow.run,
        GroceryRunInput(user_id=user_id, trigger=trigger),
        id=wf_id,
        task_queue=settings.temporal_task_queue,
    )
    return wf_id


# ── Telegram webhook ──────────────────────────────────────────────────────────


@app.post("/telegram")
async def telegram(request: Request) -> dict:
    from grocery_buddy.notifications import send_telegram_message

    update = await request.json()

    # ── Inline button callback (approve / reject) ──────────────────────────
    callback = update.get("callback_query")
    if callback:
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        if not _authorized(chat_id):
            return {"ok": True}

        data = callback.get("data", "")
        action, _, token = data.partition(":")

        if action in ("approve", "reject") and token:
            try:
                workflow_id = await _resolve_workflow_id(token)
                await _signal(workflow_id, action)
                label = (
                    "✅ On it — adding everything to your Amazon cart. "
                    "I'll send your checkout link in a moment."
                    if action == "approve"
                    else "❌ Skipped — nothing added."
                )
                await send_telegram_message(label)
            except Exception as exc:
                logger.error("Callback signal failed for %s: %s", data, exc)
                await send_telegram_message(
                    "Couldn't process that — the cart may have already been decided."
                )
        return {"ok": True}

    # ── Free-text message ──────────────────────────────────────────────────
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    logger.info("Telegram message from chat=%s: %r", chat_id, text[:80])

    if not text or not _authorized(chat_id):
        return {"ok": True}

    user_id = settings.grocery_buddy_user_id
    if not user_id:
        await send_telegram_message(
            "GROCERY_BUDDY_USER_ID is not set on the server — can't process requests."
        )
        return {"ok": True}

    # ── /start — restart onboarding (also works as first-time init) ────────
    if text.lower() in ("/start", "/restart", "/onboard"):
        await _reset_and_start_onboarding(user_id)
        return {"ok": True}

    # ── /status — check what the agent knows about you ─────────────────────
    if text.lower() in ("/status", "/help"):
        await _send_status(user_id)
        return {"ok": True}

    from grocery_buddy.db import get_pool
    from grocery_buddy.tools.conversation import get_conversation, is_first_time

    pool = await get_pool()
    mode, conv_messages = await get_conversation(pool, user_id)

    # ── Mid-onboarding: every message is an interview answer ───────────────
    if mode == "onboarding":
        await _handle_onboarding_turn(user_id, text, conv_messages, fresh=False)
        return {"ok": True}

    # ── First contact ever → start the onboarding interview ────────────────
    if await is_first_time(pool, user_id):
        await _handle_onboarding_turn(user_id, text, messages=[], fresh=True)
        return {"ok": True}

    # ── Returning user: pending cart reply, or a fresh request ─────────────
    pending = await _get_pending_cart(user_id)
    if pending:
        await _handle_briefing_reply(text, pending)
    else:
        await _handle_fresh_request(text)

    return {"ok": True}


async def _send_status(user_id: str) -> None:
    """Reply with the full pantry grouped by stock level, plus any pending cart."""
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.stock import format_stock_summary, summarize_stock
    from grocery_buddy.tools.schedule import describe_cadence, describe_next_run, get_schedule

    pool = await get_pool()

    row = await pool.fetchrow("SELECT * FROM preferences WHERE user_id = $1", uuid.UUID(user_id))
    prefs = dict(row) if row else {}
    levels = await summarize_stock(
        pool,
        user_id,
        lead_time_days=float(prefs.get("lead_time_days", 2.0)),
        buffer_days=float(prefs.get("buffer_days", 1.0)),
    )
    pantry = format_stock_summary(levels)

    pending = await _get_pending_cart(user_id)
    pending_info = _render_pending_cart(pending) if pending else "✅ No grocery list waiting"

    sched = await get_schedule(pool, user_id)
    if sched and sched.get("enabled"):
        cron = sched["cadence"]
        tz = sched.get("timezone", "America/New_York")
        sched_info = (
            f"🕐 <b>{describe_cadence(cron)}</b> — next: {describe_next_run(cron, tz)}"
        )
    else:
        sched_info = "🕐 No recurring schedule set"

    await send_telegram_message(
        f"{pantry}\n\n"
        f"{pending_info}\n"
        f"{sched_info}\n\n"
        f"<b>Commands</b>\n"
        f"/start — redo pantry interview\n"
        f"/status — this summary\n"
        f"<i>\"I need eggs early\"</i> — ad-hoc order\n"
        f"<i>\"run my briefing at 9am daily\"</i> — change schedule\n"
        f"<i>\"yes\"</i> / <i>\"no\"</i> — reply to a pending briefing"
    )


def _render_pending_cart(pending: dict) -> str:
    """One-block HTML render of the items in a pending cart, so the user always
    sees what they're being asked to approve — not just that one exists."""
    lines = ["⏳ <b>Grocery list waiting for your OK</b>"]
    for it in pending.get("items", []):
        name = ((it.get("notes") or "").strip() or it.get("product") or "item").strip()
        qty = float(it.get("qty") or 1)
        unit = (it.get("unit") or "").strip()
        qty_str = f" ({qty:g} {unit})".rstrip() if (qty != 1 or unit) else ""
        price = float(it.get("price_usd") or 0)
        lines.append(f"• {name}{qty_str} — ${price:.2f}")
    lines.append(f"<b>Total: ${pending.get('total_usd', 0):.2f}</b>")
    lines.append("Reply <i>yes</i> to add these to your Amazon cart, or <i>no</i> to skip.")
    return "\n".join(lines)


async def _reset_and_start_onboarding(user_id: str) -> None:
    """Clear any in-progress conversation and restart the onboarding interview."""
    from grocery_buddy.agents.onboarding import ONBOARDING_OPENER, advance_onboarding
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.conversation import set_conversation

    pool = await get_pool()
    messages = [{"role": "user", "content": ONBOARDING_OPENER}]
    try:
        reply, messages, done = await advance_onboarding(pool, user_id, messages)
    except Exception as exc:
        logger.error("Onboarding init failed: %s", exc)
        await send_telegram_message("Something went wrong starting the interview — try again.")
        return

    await send_telegram_message(reply)
    if done:
        from grocery_buddy.tools.conversation import clear_conversation
        await clear_conversation(pool, user_id)
        await send_telegram_message("All set! 🎉 Checking what you might need…")
        await _start_grocery_run(user_id, trigger="onboarding")
    else:
        await set_conversation(pool, user_id, "onboarding", messages)


async def _handle_onboarding_turn(
    user_id: str, text: str, messages: list[dict], fresh: bool
) -> None:
    """Drive one onboarding turn over Telegram; trigger a grocery run when complete."""
    from grocery_buddy.agents.onboarding import advance_onboarding
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.conversation import clear_conversation, set_conversation

    pool = await get_pool()

    if fresh:
        messages = [{"role": "user", "content": text}]
    else:
        messages.append({"role": "user", "content": text})

    try:
        reply, messages, done = await advance_onboarding(pool, user_id, messages)
    except Exception as exc:
        logger.error("Onboarding turn failed: %s", exc)
        await send_telegram_message("Hmm, something hiccuped — say that again?")
        return

    await send_telegram_message(reply)

    if done:
        await clear_conversation(pool, user_id)
        await send_telegram_message(
            "All set! 🎉 Let me take a first look at what you might be running low on…"
        )
        await _start_grocery_run(user_id, trigger="onboarding")
    else:
        await set_conversation(pool, user_id, "onboarding", messages)


async def _handle_briefing_reply(text: str, pending: dict) -> None:
    """User replied to a pending morning briefing."""
    from grocery_buddy.agents.assistant import parse_briefing_reply
    from grocery_buddy.notifications import send_telegram_message

    workflow_id = pending["workflow_id"]
    cart_items = pending["items"]

    try:
        intent = await parse_briefing_reply(text, cart_items)
    except Exception as exc:
        logger.error("parse_briefing_reply failed: %s", exc)
        await send_telegram_message("Sorry, couldn't understand that — try 'yes' or 'no'.")
        return

    action = intent["action"]
    logger.info("Briefing reply parsed as: %s", action)

    if action == "approve":
        await _signal(workflow_id, "approve")
        await send_telegram_message(
            "✅ On it — adding everything to your Amazon cart. "
            "I'll send your checkout link in a moment."
        )

    elif action == "reject":
        await _signal(workflow_id, "reject")
        await send_telegram_message("❌ Skipped — nothing added this run.")

    elif action == "approve_and_add":
        await _signal(workflow_id, "approve")
        extras = intent.get("items", [])
        if extras:
            summary = ", ".join(f"{i['qty']:g}× {i['product']}" for i in extras)
            await _start_quick_buy(extras, reason="Extra items from briefing reply")
            await send_telegram_message(
                f"✅ Adding the list to your Amazon cart — checkout link coming up.\n"
                f"I'll also price {summary} and send a separate list to approve."
            )
        else:
            await send_telegram_message(
                "✅ On it — adding everything to your Amazon cart. "
                "I'll send your checkout link in a moment."
            )

    elif action == "reject_and_buy":
        await _signal(workflow_id, "reject")
        items = intent.get("items", [])
        if items:
            summary = ", ".join(f"{i['qty']:g}× {i['product']}" for i in items)
            await _start_quick_buy(items, reason=intent.get("reason", "Subset from briefing"))
            await send_telegram_message(
                f"❌ Skipped the suggested list.\n"
                f"Let me price just {summary} — I'll send a list to approve."
            )
        else:
            await send_telegram_message("❌ Skipped — nothing added this run.")

    elif action == "reject_and_restart":
        # "no, start a new grocery list" — drop this cart and rebuild from the pantry.
        await _signal(workflow_id, "reject")
        await send_telegram_message(
            "❌ Scrapped that list. Let me take a fresh look at what you're running low on…"
        )
        await _start_grocery_run(settings.grocery_buddy_user_id, trigger="manual")

    elif action == "quick_buy":
        # User made a fresh request while a briefing was pending — handle both
        items = intent.get("items", [])
        if items:
            summary = ", ".join(f"{i['qty']:g}× {i['product']}" for i in items)
            await _start_quick_buy(items, reason=intent.get("reason", ""))
            await send_telegram_message(
                f"On it — pricing {summary} separately. I'll send that list to approve.\n\n"
                "You've also still got this list waiting:\n\n"
                f"{_render_pending_cart(pending)}"
            )
        # leave the pending cart open — user can still approve/reject it

    elif action == "update_schedule":
        await _apply_schedule_update(intent)

    elif action == "chat":
        # Reground the user in the list they still need to act on.
        await send_telegram_message(
            f"{intent.get('reply', 'Got it!')}\n\n{_render_pending_cart(pending)}"
        )

    else:
        await send_telegram_message("Got it — not sure what to do with that, try 'yes' or 'no'.")


async def _apply_schedule_update(intent: dict) -> None:
    """Upsert the Temporal schedule from an update_schedule intent dict."""
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.schedule import describe_next_run, upsert_schedule

    user_id = settings.grocery_buddy_user_id
    cron = intent.get("cron", "0 13 * * *")
    tz = intent.get("timezone", "America/New_York")
    description = intent.get("description", "")

    try:
        pool = await get_pool()
        await upsert_schedule(pool, user_id, cron, tz)
        next_run = describe_next_run(cron, tz)
        label = description or cron
        await send_telegram_message(
            f"✅ <b>Schedule updated!</b>\n"
            f"New cadence: <b>{label}</b>\n"
            f"Next briefing: <b>{next_run}</b>"
        )
        logger.info("Schedule updated for %s → %r (tz=%s)", user_id, cron, tz)
    except Exception as exc:
        logger.error("Schedule update failed: %s", exc)
        await send_telegram_message(
            "Couldn't update the schedule right now — is the Temporal worker running?"
        )


async def _handle_fresh_request(text: str) -> None:
    """No pending cart — treat as a fresh purchase, schedule change, or question."""
    from grocery_buddy.agents.assistant import parse_request
    from grocery_buddy.notifications import send_telegram_message

    try:
        intent = await parse_request(text)
    except Exception as exc:
        logger.error("parse_request failed: %s", exc)
        await send_telegram_message("Sorry, something went wrong — try again in a moment.")
        return

    if intent["action"] == "quick_buy":
        items = intent.get("items", [])
        if items:
            summary = ", ".join(f"{i['qty']:g}× {i['product']}" for i in items)
            await _start_quick_buy(items, reason=intent.get("reason", ""))
            await send_telegram_message(
                f"On it — pricing {summary}. I'll send you a list to look over; "
                "nothing goes in your cart until you say so."
            )
    elif intent["action"] == "update_schedule":
        await _apply_schedule_update(intent)
    else:
        await send_telegram_message(intent.get("reply", "Got it!"))


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
