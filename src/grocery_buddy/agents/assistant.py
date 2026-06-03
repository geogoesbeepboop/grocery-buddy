"""Conversational assistant — turns free-text into structured actions.

Two entry points:
  parse_request(message)
      Fresh message with no pending context (e.g. "I need eggs early").
      Returns quick_buy or chat.

  parse_briefing_reply(message, cart_items)
      Reply in the context of a pending grocery cart (the morning briefing).
      Returns approve | reject | approve_and_add | reject_and_buy | chat.

Both are channel-agnostic: the CLI `ask` command and the Telegram webhook both
call these. The Telegram /telegram route passes cart context when a pending cart
exists; the CLI always uses parse_request (no pending cart).
"""
from __future__ import annotations

import logging

import anthropic

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)

# ── Shared tool schemas ────────────────────────────────────────────────────────

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "product": {"type": "string", "description": "Product name"},
        "qty": {"type": "number", "description": "Quantity (default 1)"},
        "unit": {
            "type": "string",
            "description": "Unit if stated by user; otherwise omit",
        },
        "preferred_brand": {
            "type": "string",
            "description": (
                "Brand the user explicitly named for THIS item right now, e.g. "
                "\"get Eggland's Best eggs instead\" → 'Eggland's Best'. Omit if "
                "they didn't name a brand."
            ),
        },
    },
    "required": ["product"],
}

# ── Schedule-update tool (shared between fresh and briefing-reply contexts) ────

_SCHEDULE_TOOL: dict = {
    "name": "update_schedule",
    "description": (
        "Update the time or frequency of the user's daily grocery briefing. "
        "Use when the user says things like 'change my briefing to 9am', "
        "'run it every 6 hours', 'schedule it for 7:30am', "
        "'every Monday and Thursday at 8am', etc. "
        "Convert what the user says to a standard 5-field cron expression (UTC). "
        "If they give a local time and don't specify a timezone, assume their "
        "timezone is America/New_York unless context says otherwise."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": (
                    "5-field cron expression in UTC. Examples: "
                    "'0 13 * * *' = daily 8am ET, "
                    "'0 */6 * * *' = every 6 hours, "
                    "'*/30 * * * *' = every 30 minutes."
                ),
            },
            "timezone": {
                "type": "string",
                "description": (
                    "IANA timezone the user is in, e.g. 'America/New_York'. "
                    "Used for display only — the cron must already be in UTC."
                ),
            },
            "description": {
                "type": "string",
                "description": "Plain-English cadence label, e.g. 'daily at 9:00 am ET'.",
            },
        },
        "required": ["cron"],
    },
}

# ── Fresh-request tools (no cart context) ─────────────────────────────────────

_FRESH_TOOLS: list[dict] = [
    {
        "name": "request_purchase",
        "description": (
            "Start an approval-gated purchase for items the user explicitly asked for."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": _ITEM_SCHEMA,
                    "description": "Items to buy",
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason shown in the approval message",
                },
            },
            "required": ["items"],
        },
    },
    _SCHEDULE_TOOL,
]

# ── Briefing-reply tools (pending cart context) ────────────────────────────────

_BRIEFING_TOOLS: list[dict] = [
    {
        "name": "approve_cart",
        "description": (
            "The user wants to buy everything in the pending cart as-is. "
            "Use when they say 'yes', 'ok', 'approve', 'go ahead', 'buy it all', etc."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reject_cart",
        "description": (
            "The user doesn't want to buy anything this run and is done. "
            "Use when they say 'no', 'skip', 'cancel', 'not now', etc. — with NO "
            "request to build another list. If they want a fresh list instead, use "
            "reject_and_restart; if they name items to buy instead, use reject_and_buy."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reject_and_restart",
        "description": (
            "The user wants to throw away this pending list and have you build a "
            "brand-new one from scratch by re-checking their pantry. Use when they say "
            "'no, start a new grocery list', 'scrap this and make a new one', 'redo my "
            "list', 'start over', etc. Do NOT use this if they named specific items to "
            "buy instead (that's reject_and_buy)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "approve_and_add",
        "description": (
            "The user wants everything in the cart PLUS additional items they specified. "
            "Use when they say 'yes and add X', 'buy it all plus Y', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "extra_items": {
                    "type": "array",
                    "items": _ITEM_SCHEMA,
                    "description": "Additional items to also buy",
                },
            },
            "required": ["extra_items"],
        },
    },
    {
        "name": "reject_and_buy",
        "description": (
            "The user wants to skip the suggested cart as-is but buy specific items "
            "instead. Use when they say 'only get the eggs', 'just the milk', 'skip "
            "everything except X', or name a subset or different items they actually "
            "need. ALSO use this when they want the same item(s) but priced differently — "
            "a specific/different brand ('those eggs are the wrong brand, get Eggland's "
            "Best instead' → item product 'eggs', preferred_brand 'Eggland's Best'), or a "
            "cheaper option ('those eggs are too expensive, find a cheaper one' → re-buy "
            "'eggs' with no preferred_brand so the cheapest is picked). The items can be a "
            "subset of the cart or completely different products."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": _ITEM_SCHEMA,
                    "description": "Items to actually buy instead of the pending cart",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason (optional)",
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "request_purchase",
        "description": (
            "The user is making a completely NEW purchase request unrelated to the "
            "pending cart (they want something in addition to or instead of the briefing). "
            "Only use this if their message is clearly NOT a response to the pending cart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": _ITEM_SCHEMA},
                "reason": {"type": "string"},
            },
            "required": ["items"],
        },
    },
    _SCHEDULE_TOOL,
]


def _build_item(it: dict) -> dict:
    return {
        "product": it["product"],
        "qty": float(it.get("qty") or 1),
        "unit": it.get("unit") or "",
        "preferred_brand": (it.get("preferred_brand") or "").strip() or None,
    }


# ── Briefing composition (natural-language grocery list) ──────────────────────


def _item_display_name(it: dict) -> str:
    """The real thing we'd buy — the chosen Amazon listing (stored in notes),
    falling back to the generic product name."""
    return ((it.get("notes") or "").strip() or it.get("product") or "item").strip()


def _render_briefing_lines(items: list[dict]) -> list[str]:
    """Deterministic, hallucination-proof item lines used both as the fallback
    render and as the ground-truth facts handed to the LLM composer."""
    lines: list[str] = []
    for it in items:
        name = _item_display_name(it)
        qty = float(it.get("qty") or 1)
        unit = (it.get("unit") or "").strip()
        qty_str = f"{qty:g} {unit}".strip()
        price = float(it.get("price_usd") or 0)
        suffix = f" ({qty_str})" if qty_str and qty_str != "1" else ""
        lines.append(f"• {name}{suffix} — ${price:.2f}")
    return lines


def _fallback_briefing(items: list[dict], total_usd: float, reason: str | None) -> str:
    header = reason.strip() if reason else "Here's what I think you're running low on"
    body = "\n".join(_render_briefing_lines(items)) or "  (no items)"
    return (
        f"🛒 <b>{header}</b>\n\n"
        f"{body}\n\n"
        f"<b>Total: ${total_usd:.2f}</b>\n\n"
        "Does this look good? If so, I'll add these to your Amazon cart and send you a "
        "checkout link to finish — I never place the order myself. Reply to tweak it "
        "(swap a brand, drop something, add an item)."
    )


async def compose_briefing(
    items: list[dict], total_usd: float, reason: str | None = None
) -> str:
    """Write a warm, natural grocery-approval message (Telegram HTML).

    Grounded on exact item names/prices so it can't drift — the model gets the
    rendered facts and must reuse them. Falls back to a deterministic render on any
    error or if the model drops the total, so the briefing always goes out.
    """
    if not items:
        return _fallback_briefing(items, total_usd, reason)

    facts = "\n".join(_render_briefing_lines(items))
    context = f"Context for this run: {reason}\n" if reason else ""
    system = (
        "You are Grocery Buddy texting a user their grocery list for approval. "
        "Write a short, warm, natural message — like a helpful friend, not a receipt.\n\n"
        "Hard rules:\n"
        "• Use the EXACT items and prices given. Don't invent, drop, or re-price anything.\n"
        "• Show each item with its brand/variant (the names already include them) and its price.\n"
        "• State the exact total.\n"
        "• End by asking if it looks good, and make clear that if they approve you'll add "
        "everything to their Amazon cart and send a checkout link — you NEVER buy on their "
        "behalf, they finish checkout themselves.\n"
        "• Invite quick tweaks (swap a brand, drop one, add something).\n"
        "• Telegram HTML only: <b>, <i>. No markdown, no code blocks. Keep it tight."
    )
    user = (
        f"{context}"
        f"Items (use these exact names and prices):\n{facts}\n\n"
        f"Total: ${total_usd:.2f}"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=settings.model_fast,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        # Guard against drift: the exact total must survive.
        if text and f"{total_usd:.2f}" in text:
            return text
        logger.warning("Briefing composer output missing total — using fallback")
    except Exception as exc:
        logger.warning("Briefing composition failed (%s) — using fallback", exc)

    return _fallback_briefing(items, total_usd, reason)


async def parse_request(message: str) -> dict:
    """Interpret a free-text message with no pending-cart context.

    Returns one of:
      {"action": "quick_buy", "items": [...], "reason": str}
      {"action": "update_schedule", "cron": str, "timezone": str, "description": str}
      {"action": "chat", "reply": str}
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = (
        "You are Grocery Buddy's assistant. The user talks to you in plain language.\n\n"
        "If they want to buy specific item(s) now, call request_purchase — even if "
        "they were vague about quantity or brand. NEVER interrogate them with a "
        "checklist of questions like 'how much?' or 'any specific type?'. If they said "
        "'a loaf of bread', that's qty 1 — just buy it. If no quantity is given, "
        "default to 1. They review and tweak everything (quantity, brand, swaps) at the "
        "approval step before anything is bought, so there's no need to ask up front.\n"
        "If they want to change when or how often the grocery briefing runs, "
        "call update_schedule — convert what they say into a UTC cron expression.\n"
        "Otherwise, reply conversationally. Be concise."
    )
    response = await client.messages.create(
        model=settings.model_fast,
        max_tokens=512,
        system=system,
        tools=_FRESH_TOOLS,
        messages=[{"role": "user", "content": message}],
    )
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        args = block.input or {}

        if block.name == "request_purchase":
            items = [_build_item(it) for it in args.get("items", []) if it.get("product")]
            if items:
                return {
                    "action": "quick_buy",
                    "items": items,
                    "reason": args.get("reason", ""),
                }

        if block.name == "update_schedule":
            return {
                "action": "update_schedule",
                "cron": args.get("cron", "0 13 * * *"),
                "timezone": args.get("timezone", "America/New_York"),
                "description": args.get("description", ""),
            }

    reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    return {"action": "chat", "reply": reply or "(no response)"}


async def parse_briefing_reply(message: str, cart_items: list[dict]) -> dict:
    """Interpret a reply in the context of a pending grocery cart.

    ``cart_items`` — list of dicts with at least {product, qty, unit, price_usd}.

    Returns one of:
      {"action": "approve"}
      {"action": "reject"}
      {"action": "reject_and_restart"}                # reject cart + rebuild from pantry
      {"action": "approve_and_add", "items": [...]}   # approve cart + QuickBuy extras
      {"action": "reject_and_buy", "items": [...]}    # reject cart + QuickBuy subset
      {"action": "quick_buy", "items": [...], "reason": str}  # unrelated fresh request
      {"action": "chat", "reply": str}
    """
    cart_lines = "\n".join(
        f"  • {it['product']} × {it['qty']:g} {it['unit']} — ${it.get('price_usd') or 0:.2f}"
        for it in cart_items
    )
    total = sum(float(it.get("price_usd") or 0) * float(it.get("qty") or 1) for it in cart_items)

    system = (
        "You are Grocery Buddy's assistant. The user just received their daily grocery "
        "briefing and replied. Interpret their reply in the context of the pending cart "
        "shown below, and call the appropriate tool.\n\n"
        f"Pending cart (${total:.2f} total):\n{cart_lines}\n\n"
        "Tool guide:\n"
        "• approve_cart — they want to buy everything\n"
        "• reject_cart — they want to skip everything and are done\n"
        "• reject_and_restart — they want this list scrapped and a NEW one built from "
        "scratch ('no, start a new grocery list', 'redo it', 'start over')\n"
        "• approve_and_add — buy the cart plus extra items they mentioned\n"
        "• reject_and_buy — skip the suggested cart, buy only what they specified, OR "
        "re-buy the same item priced differently (a different/specific brand, or a "
        "cheaper option)\n"
        "• request_purchase — a completely new/unrelated request\n\n"
        "Critical: a 'no' that comes with ANY request to make a new list, start over, "
        "or buy something else is NOT a plain reject_cart — honor what they asked for "
        "(reject_and_restart or reject_and_buy). Only use reject_cart when they simply "
        "want nothing.\n"
        "For reject_and_buy, derive the item list from what the user said (can be a "
        "subset of the cart or different products). If they name a brand for an item "
        "('get Eggland's Best eggs instead'), put it in that item's preferred_brand. "
        "If they just want it cheaper, re-buy the item with no preferred_brand. "
        "Do not add items they didn't mention, and never ask for a quantity or brand "
        "they didn't give — default quantity to 1 and let them tweak at the next "
        "approval step."
    )
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.model_fast,
        max_tokens=512,
        system=system,
        tools=_BRIEFING_TOOLS,
        messages=[{"role": "user", "content": message}],
    )

    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        args = block.input or {}

        if block.name == "approve_cart":
            return {"action": "approve"}

        if block.name == "reject_cart":
            return {"action": "reject"}

        if block.name == "reject_and_restart":
            return {"action": "reject_and_restart"}

        if block.name == "approve_and_add":
            extras = [_build_item(it) for it in args.get("extra_items", []) if it.get("product")]
            if not extras:
                return {"action": "approve"}
            return {"action": "approve_and_add", "items": extras}

        if block.name == "reject_and_buy":
            items = [_build_item(it) for it in args.get("items", []) if it.get("product")]
            return (
                {"action": "reject_and_buy", "items": items, "reason": args.get("reason", "")}
                if items else {"action": "reject"}
            )

        if block.name == "request_purchase":
            items = [_build_item(it) for it in args.get("items", []) if it.get("product")]
            if items:
                return {
                    "action": "quick_buy",
                    "items": items,
                    "reason": args.get("reason", ""),
                }

        if block.name == "update_schedule":
            return {
                "action": "update_schedule",
                "cron": args.get("cron", "0 13 * * *"),
                "timezone": args.get("timezone", "America/New_York"),
                "description": args.get("description", ""),
            }

    reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    return {"action": "chat", "reply": reply or "Got it!"}
