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

Free-text message  (full tree: docs/SYSTEM_REFERENCE.md §6)
  → conversation mode onboarding / import_review → that turn handler
  → check DB for a pending cart for this user
  → if pending cart: parse_briefing_reply(text, cart_items)
      approve            → signal approve (checkout the pending cart)
      buy_items          → signal reject + start QuickBuyWorkflow (new cart from
                           the named items; the suggestion is set aside)
      reject             → signal reject (done)
      reject_and_restart → signal reject + start a fresh GroceryRun
      update_inventory   → correct stock + reject + rebuild
      chat               → send reply text + re-show the cart
  → else: parse_request(text, stock_snapshot)
      quick_buy          → start QuickBuyWorkflow
      start_grocery_run  → start GroceryRunWorkflow (manual) — "buy what I'm low on"
      update_inventory / update_schedule
      chat               → send reply text

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

# Shared user-facing copy, so the same action reads the same everywhere (the button
# reject and the text-reply reject were drifting apart).
_MSG_SKIPPED = "❌ Skipped — nothing added."
_MSG_ERROR = "Hmm, something hiccuped on my end — try that again in a sec?"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup: detect user state and proactively start the right conversation."""
    from grocery_buddy.notifications import (
        register_bot_commands,
        send_telegram_message,
        telegram_enabled,
    )

    if not telegram_enabled():
        logger.warning("Telegram not configured — no startup ping sent")
        yield
        logger.info("Webhook server shutting down")
        return

    # Publish the slash-command menu so /import, /status, etc. autocomplete.
    await register_bot_commands()

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
                "Say <i>\"grab some coffee\"</i> to order something now, "
                "<i>\"buy what I'm low on\"</i> to restock, or <i>/status</i> to see your "
                "pantry. New here? <i>/help</i>."
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


async def _retire_pending_cart(pending: dict) -> None:
    """Cleanly close out a pending cart before starting a replacement.

    Signals the workflow to reject AND flips the cart's DB status synchronously.
    Without the synchronous DB flip, the new run/cart we start next races the old
    workflow's own status update — which can leave two carts pending_approval at
    once, or trip the open-cart guard so a user-requested rebuild silently no-ops.
    """
    from grocery_buddy.db import get_pool

    wf = pending.get("workflow_id")
    if wf:
        try:
            await _signal(wf, "reject")
        except Exception as exc:
            logger.warning("Reject signal failed for %s: %s", wf, exc)
    cart_id = pending.get("cart_id")
    if cart_id:
        pool = await get_pool()
        await pool.execute(
            "UPDATE carts SET status = 'rejected', updated_at = NOW() "
            "WHERE id = $1 AND status = 'pending_approval'",
            uuid.UUID(cart_id),
        )


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


async def _start_import_history(user_id: str) -> None:
    """Kick off an Amazon order-history import (scrape → synthesize → review).

    The heavy work runs in ImportHistoryWorkflow; we just acknowledge fast and let
    the workflow send the proposal when it's ready.
    """
    from grocery_buddy.models import ImportHistoryInput
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.workflows.import_history import ImportHistoryWorkflow

    wf_id = f"import-history-{user_id}-{uuid.uuid4().hex[:8]}"
    try:
        client = await _get_client()
        await client.start_workflow(
            ImportHistoryWorkflow.run,
            ImportHistoryInput(user_id=user_id),
            id=wf_id,
            task_queue=settings.temporal_task_queue,
        )
        await send_telegram_message(
            "🔎 Reading through your recent Amazon orders to draft your pantry — this "
            "takes a minute. I'll send what I find so you can fix anything before I save it."
        )
    except Exception as exc:
        logger.error("Failed to start import workflow: %s", exc)
        await send_telegram_message(
            "I couldn't start the Amazon import just now (is the worker running?). "
            "We can set up by hand instead — just tell me what's in your pantry."
        )


async def _handle_amazon_2fa_code(user_id: str, text: str) -> None:
    """Relay the user's Amazon 2FA code back to the waiting re-login activity.

    The user is in ``amazon_2fa`` mode because a re-login activity asked them for
    the one-time code Amazon just sent. We write the digits onto their pending
    challenge (the activity is polling for it) and hand control back — the activity
    enters it and continues the import.
    """
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.auth import submit_otp_code
    from grocery_buddy.tools.conversation import set_conversation

    code = "".join(c for c in text if c.isdigit())
    pool = await get_pool()

    if not code:
        await send_telegram_message(
            "I just need the numeric code Amazon sent you (e.g. <b>123456</b>). "
            "Reply with that, or say /import to start over."
        )
        return

    accepted = await submit_otp_code(pool, user_id, code)
    # Hand control back to the activity; it owns the next message either way.
    await set_conversation(pool, user_id, "idle", [])
    if accepted:
        await send_telegram_message("Thanks — entering that now…")
    else:
        await send_telegram_message(
            "That code isn't needed anymore — I may have already timed out waiting. "
            "Say /import to try again."
        )


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
                    else _MSG_SKIPPED
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

    # ── /clear — (hidden, testing only) wipe pantry + habits; not advertised ─
    # Intentionally absent from /status, /help, and all user-facing copy. Lets the
    # operator reset to a clean slate so the next message re-triggers onboarding.
    if text.lower() == "/clear":
        await _handle_clear(user_id)
        return {"ok": True}

    # ── /start — restart onboarding (also works as first-time init) ────────
    if text.lower() in ("/start", "/restart", "/onboard"):
        await _reset_and_start_onboarding(user_id)
        return {"ok": True}

    # ── /import — bootstrap pantry from Amazon order history ───────────────
    if text.lower() in ("/import", "/importorders"):
        await _start_import_history(user_id)
        return {"ok": True}

    # ── /help — what the bot can do ────────────────────────────────────────
    if text.lower() == "/help":
        await _send_help()
        return {"ok": True}

    # ── /status — check what the agent knows about you ─────────────────────
    if text.lower() == "/status":
        await _send_status(user_id)
        return {"ok": True}

    from grocery_buddy.db import get_pool
    from grocery_buddy.tools.conversation import get_conversation, is_first_time

    pool = await get_pool()
    mode, conv_messages = await get_conversation(pool, user_id)

    # ── Mid-Amazon-relogin: this reply is the 2FA one-time code ────────────
    if mode == "amazon_2fa":
        await _handle_amazon_2fa_code(user_id, text)
        return {"ok": True}

    # ── Mid-onboarding: every message is an interview answer ───────────────
    if mode == "onboarding":
        await _handle_onboarding_turn(user_id, text, conv_messages, fresh=False)
        return {"ok": True}

    # ── Mid-import-review: editing/confirming the synthesized pantry ───────
    if mode == "import_review":
        await _handle_import_review_turn(user_id, text, conv_messages)
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
        f"<b>Quick commands</b>\n"
        f"/import — rebuild pantry from your Amazon orders\n"
        f"/start — set up the pantry by hand\n"
        f"/help — what I can do\n\n"
        f"Or just talk to me: <i>\"we're out of milk\"</i>, "
        f"<i>\"buy what I'm low on\"</i>, <i>\"run my briefing at 9am daily\"</i>."
    )


async def _send_help() -> None:
    """A short capabilities message — what the bot does and how to talk to it."""
    from grocery_buddy.notifications import send_telegram_message

    await send_telegram_message(
        "👋 <b>I'm Grocery Buddy.</b> I keep an eye on your pantry, figure out what "
        "you're running low on, and line up an Amazon cart for you — you always "
        "approve before anything is bought, and you tap the final checkout yourself.\n\n"
        "<b>Get started</b>\n"
        "/import — build your pantry from recent Amazon orders (fastest)\n"
        "/start — set it up by hand instead\n\n"
        "<b>Anytime, just talk to me</b>\n"
        "• <i>\"we're out of milk\"</i> / <i>\"still have plenty of eggs\"</i> — keep stock honest\n"
        "• <i>\"grab some coffee\"</i> — order something now\n"
        "• <i>\"buy what I'm low on\"</i> — restock everything that's low\n"
        "• <i>\"run my briefing at 8am daily\"</i> — set your schedule\n"
        "• <i>/status</i> — see your pantry, any waiting list, and your schedule\n\n"
        "When I send a list, reply <i>yes</i> to order it or <i>no</i> to skip."
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


async def _handle_clear(user_id: str) -> None:
    """(Hidden) Wipe pantry + habits + history so the user can start from scratch.

    Testing utility only — never referenced in any user-facing command list. We do
    reply to confirm (the operator typed it), but we don't advertise it.
    """
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.reset import clear_user_data

    try:
        pool = await get_pool()
        counts = await clear_user_data(pool, user_id)
    except Exception as exc:
        logger.error("Clear failed for %s: %s", user_id, exc)
        await send_telegram_message("⚠️ Couldn't clear your data — check the server logs.")
        return

    wiped = (
        f"{counts.get('inventory_items', 0)} pantry items, "
        f"{counts.get('consumption_profile', 0)} habits, "
        f"{counts.get('consumption_events', 0)} events, "
        f"{counts.get('carts', 0)} carts"
    )
    await send_telegram_message(
        f"🧹 Cleared ({wiped}). You're back to a blank slate — send any message to "
        "start onboarding again."
    )


async def _reset_and_start_onboarding(user_id: str) -> None:
    """Clear any in-progress conversation and restart the onboarding interview."""
    from grocery_buddy.agents.onboarding import ONBOARDING_OPENER, advance_onboarding
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message

    pool = await get_pool()
    messages = [{"role": "user", "content": ONBOARDING_OPENER}]
    try:
        reply, messages, status = await advance_onboarding(pool, user_id, messages)
    except Exception as exc:
        logger.error("Onboarding init failed: %s", exc)
        await send_telegram_message("Something went wrong starting the interview — try again.")
        return

    await send_telegram_message(reply)
    await _resolve_onboarding_status(user_id, status, messages)


async def _resolve_onboarding_status(user_id: str, status: str, messages: list[dict]) -> None:
    """Apply the outcome of an onboarding turn: continue, finish, or import."""
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.conversation import clear_conversation, set_conversation

    pool = await get_pool()
    if status == "import_orders":
        # Hand off to the durable import flow; it will switch the user into
        # import_review mode and send the proposal when ready.
        await clear_conversation(pool, user_id)
        await _start_import_history(user_id)
    elif status == "done":
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

    pool = await get_pool()

    if fresh:
        messages = [{"role": "user", "content": text}]
    else:
        messages.append({"role": "user", "content": text})

    try:
        reply, messages, status = await advance_onboarding(pool, user_id, messages)
    except Exception as exc:
        logger.error("Onboarding turn failed: %s", exc)
        await send_telegram_message("Hmm, something hiccuped — say that again?")
        return

    await send_telegram_message(reply)
    await _resolve_onboarding_status(user_id, status, messages)


# ── Order-history import review ───────────────────────────────────────────────


async def _handle_import_review_turn(user_id: str, text: str, messages: list[dict]) -> None:
    """Drive one turn of reviewing/editing the synthesized import proposal."""
    from grocery_buddy.agents.order_history import advance_import_review
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.conversation import clear_conversation, set_conversation
    from grocery_buddy.tools.imports import get_active_import_proposal, set_proposal_status

    pool = await get_pool()
    proposal = await get_active_import_proposal(pool, user_id)
    if not proposal:
        # Proposal vanished (already confirmed/discarded) — drop back to normal routing.
        await clear_conversation(pool, user_id)
        await send_telegram_message(
            "That import isn't active anymore. Say /import to try again, or just tell me "
            "what's in your pantry."
        )
        return

    messages.append({"role": "user", "content": text})
    try:
        reply, messages, items, outcome = await advance_import_review(
            pool, user_id, proposal["id"], proposal["items"], messages
        )
    except Exception as exc:
        logger.error("Import review turn failed: %s", exc)
        await send_telegram_message("Hmm, something hiccuped reviewing that — say it again?")
        return

    if outcome == "confirm":
        await _finalize_import(user_id, proposal["id"], items, reply)
    elif outcome == "cancel":
        await set_proposal_status(pool, proposal["id"], "discarded")
        await clear_conversation(pool, user_id)
        await send_telegram_message(
            (reply + "\n\n" if reply else "")
            + "No problem — I scrapped that. Tell me what's in your pantry whenever you're "
            "ready, or say /import to try again."
        )
    else:
        if reply:
            await send_telegram_message(reply)
        await set_conversation(pool, user_id, "import_review", messages)


async def _finalize_import(
    user_id: str, proposal_id: str, items: list[dict], reply: str
) -> None:
    """Persist a confirmed proposal into inventory + habits, then run first grocery run."""
    from grocery_buddy.db import get_pool
    from grocery_buddy.notifications import send_telegram_message
    from grocery_buddy.tools.conversation import clear_conversation
    from grocery_buddy.tools.consumption import upsert_consumption_profile
    from grocery_buddy.tools.imports import set_proposal_status
    from grocery_buddy.tools.inventory import upsert_inventory_item

    pool = await get_pool()
    saved = 0
    for it in items:
        try:
            unit = it.get("unit") or "unit"
            await upsert_inventory_item(
                pool, user_id,
                product=it["product"],
                qty=float(it.get("estimated_qty") or 0),
                unit=unit,
                par_level=float(it.get("par_level") or 1) or 1.0,
            )
            rate = float(it.get("daily_rate") or 0)
            if rate > 0:
                await upsert_consumption_profile(
                    pool, user_id,
                    product=it["product"],
                    declared_rate=rate,
                    unit=unit,
                    preferred_brand=it.get("preferred_brand"),
                    brand_flexibility=it.get("brand_flexibility") or "any",
                    notes="imported from Amazon order history",
                )
            saved += 1
        except Exception as exc:
            logger.warning("Failed to import item %r: %s", it.get("product"), exc)

    await set_proposal_status(pool, proposal_id, "confirmed")
    await clear_conversation(pool, user_id)

    if saved == 0:
        await send_telegram_message(
            (reply + "\n\n" if reply else "")
            + "There was nothing left to save. Tell me what's in your pantry whenever "
            "you're ready, or say /import to try the Amazon import again."
        )
        return

    # Show the freshly-populated pantry back so the user can confirm we captured it
    # right (this is the "updated inventory" recap), then kick off the first run.
    from grocery_buddy.stock import format_stock_summary, summarize_stock

    recap = ""
    try:
        levels = await summarize_stock(pool, user_id)
        recap = "\n\n" + format_stock_summary(levels)
    except Exception as exc:
        logger.warning("Post-import stock summary failed: %s", exc)

    await send_telegram_message(
        (reply + "\n\n" if reply else "")
        + f"✅ Saved {saved} item{'s' if saved != 1 else ''} to your pantry.{recap}"
    )
    await send_telegram_message("Now let me take a first look at what you might need…")
    await _start_grocery_run(user_id, trigger="onboarding")


async def _apply_inventory_update(items: list[dict]) -> str:
    """Apply on-the-fly on-hand quantity corrections; return a short summary line."""
    from grocery_buddy.db import get_pool
    from grocery_buddy.tools.inventory import set_actual_quantity

    user_id = settings.grocery_buddy_user_id
    pool = await get_pool()
    applied: list[str] = []
    for it in items:
        try:
            row = await set_actual_quantity(
                pool, user_id, it["product"], float(it["qty"]), it.get("unit")
            )
            unit = (row.get("unit") or "").strip()
            applied.append(f"{row['product']} → {float(row['qty']):g} {unit}".strip())
        except Exception as exc:
            logger.warning("Inventory correction failed for %r: %s", it.get("product"), exc)
    return ", ".join(applied)


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
        await send_telegram_message(_MSG_ERROR)
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
        await _retire_pending_cart(pending)
        await send_telegram_message(_MSG_SKIPPED)

    elif action == "buy_items":
        # User named specific items → build a fresh cart from exactly those and set the
        # pending suggestion aside (one clean approval track, least friction).
        items = intent.get("items", [])
        if not items:
            # Ambiguous "buy something" — ask, and keep the suggestion in view rather
            # than silently skipping it.
            await send_telegram_message(
                "Which items should I buy?\n\n" + _render_pending_cart(pending)
            )
            return
        await _retire_pending_cart(pending)
        summary = ", ".join(f"{i['qty']:g}× {i['product']}" for i in items)
        await _start_quick_buy(items, reason=intent.get("reason", "") or "New cart from your request")
        await send_telegram_message(
            f"On it — building a fresh cart with {summary}. I'll send it to look "
            "over; nothing goes in your cart until you say so."
        )

    elif action == "reject_and_restart":
        # "no, start a new grocery list" — drop this cart and rebuild from the pantry.
        await _retire_pending_cart(pending)
        await send_telegram_message(
            "❌ Scrapped that list. Let me take a fresh look at what you're running low on…"
        )
        await _start_grocery_run(settings.grocery_buddy_user_id, trigger="manual")

    elif action == "update_inventory":
        # The user corrected real on-hand amounts ("we still have plenty of eggs").
        summary = await _apply_inventory_update(intent.get("items", []))
        if not summary:
            # Nothing landed — don't scrap a good cart on a failed update; ask + re-show.
            await send_telegram_message(
                "I couldn't update that — mind naming the item and how much you have?\n\n"
                + _render_pending_cart(pending)
            )
            return
        # The cart was built on stale numbers, so rebuild from the corrected pantry.
        await _retire_pending_cart(pending)
        await send_telegram_message(
            f"👍 Updated your pantry: {summary}.\n"
            "That changes things — let me take a fresh look at what you actually need…"
        )
        await _start_grocery_run(settings.grocery_buddy_user_id, trigger="manual")

    elif action == "update_schedule":
        # Schedule change doesn't decide the cart — apply it, then keep the cart in view
        # so it isn't silently left waiting.
        await _apply_schedule_update(intent)
        await send_telegram_message(_render_pending_cart(pending))

    elif action == "chat":
        # Reground the user in the list they still need to act on.
        await send_telegram_message(
            f"{intent.get('reply', 'Got it!')}\n\n{_render_pending_cart(pending)}"
        )

    else:
        await send_telegram_message(
            "Got it — reply <i>yes</i> to order this list or <i>no</i> to skip it.\n\n"
            + _render_pending_cart(pending)
        )


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


async def _build_stock_snapshot(user_id: str) -> str | None:
    """Compact plain-text pantry snapshot for the assistant (low items first).

    Lets the free-text assistant answer "what am I low on?" and route "buy
    everything low" instead of claiming it has no inventory visibility.
    """
    from grocery_buddy.db import get_pool
    from grocery_buddy.predictor import LOW, MEDIUM
    from grocery_buddy.stock import summarize_stock

    try:
        pool = await get_pool()
        levels = await summarize_stock(pool, user_id)
    except Exception as exc:
        logger.warning("Stock snapshot failed: %s", exc)
        return None
    if not levels:
        return "(the pantry is empty — nothing tracked yet)"

    rank = {LOW: 0, MEDIUM: 1}
    lines = []
    for lv in sorted(levels, key=lambda x: (rank.get(x.bucket, 2), x.days_remaining)):
        tag = {"low": "LOW", "medium": "ok", "large": "plenty"}.get(lv.bucket, lv.bucket)
        lines.append(f"- {lv.product}: {lv.qty:g} {lv.unit} [{tag}]")
    return "\n".join(lines)


async def _handle_fresh_request(text: str) -> None:
    """No pending cart — treat as a fresh purchase, schedule change, or question."""
    from grocery_buddy.agents.assistant import parse_request
    from grocery_buddy.notifications import send_telegram_message

    user_id = settings.grocery_buddy_user_id
    try:
        stock_summary = await _build_stock_snapshot(user_id)
        intent = await parse_request(text, stock_summary=stock_summary)
    except Exception as exc:
        logger.error("parse_request failed: %s", exc)
        await send_telegram_message(_MSG_ERROR)
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
    elif intent["action"] == "start_grocery_run":
        await _start_grocery_run(user_id, trigger="manual")
        await send_telegram_message(
            "🛒 On it — checking everything you're running low on and pricing it out. "
            "I'll send a list to look over; nothing goes in your cart until you say so."
        )
    elif intent["action"] == "update_inventory":
        summary = await _apply_inventory_update(intent.get("items", []))
        if summary:
            await send_telegram_message(f"👍 Updated your pantry: {summary}.")
        else:
            await send_telegram_message(
                "I couldn't update that — mind naming the item and how much you have?"
            )
    elif intent["action"] == "update_schedule":
        await _apply_schedule_update(intent)
    else:
        await send_telegram_message(intent.get("reply", "Got it!"))


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
