"""Conversational assistant — turns free-text into structured actions.

Two entry points:
  parse_request(message)
      Fresh message with no pending context (e.g. "I need eggs early").
      Returns quick_buy or chat.

  parse_briefing_reply(message, cart_items)
      Reply in the context of a pending grocery cart (the morning briefing).
      Returns approve | buy_items | reject | reject_and_restart | update_inventory |
      update_schedule | chat. Guiding rule: naming items → a new cart (buy_items);
      only an explicit approval checks out the pending suggestion.

Both are channel-agnostic: the CLI `ask` command and the Telegram webhook both
call these. The Telegram /telegram route passes cart context when a pending cart
exists; the CLI always uses parse_request (no pending cart).
"""
from __future__ import annotations

import logging

from grocery_buddy import llm
from grocery_buddy.config import settings

logger = logging.getLogger(__name__)

# ── Shared voice ───────────────────────────────────────────────────────────────
# One persona snippet so every Claude-authored message sounds like the same
# assistant, and the "don't interrogate / HTML-only" rules live in exactly one
# place instead of drifting across prompts.
PERSONA = (
    "You are Grocery Buddy — a warm, concise assistant that quietly keeps up with the "
    "user's pantry and makes reordering effortless. Talk like a helpful friend, not a "
    "form. Never interrogate the user for a quantity or brand they didn't give — "
    "default quantity to 1; they can tweak anything at the approval step before "
    "anything is bought. Keep replies short. Telegram HTML only (<b>, <i>); no "
    "markdown, no code blocks."
)

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

# ── Pantry-correction tool (shared between fresh and briefing-reply contexts) ──

_UPDATE_INVENTORY_TOOL: dict = {
    "name": "update_pantry_quantity",
    "description": (
        "Correct how much of one or more pantry items the user CURRENTLY has on hand. "
        "Use this whenever the user tells you their real quantity rather than asking to "
        "buy — e.g. 'we still have a full dozen eggs', 'I've only got about 2 eggs left', "
        "'we're out of milk', 'family came over so the eggs are gone', or 'I barely "
        "touched the coffee this week, still almost full'. This RESETS our running "
        "estimate back to what they actually have. The user can correct several items in "
        "one message — include every item they mention. Use qty 0 when they say something "
        "is gone / used up / out. Do NOT use this to buy things (that's request_purchase) "
        "— only to update on-hand amounts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Each pantry item whose on-hand quantity the user is correcting.",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {"type": "string", "description": "Product name"},
                        "qty": {
                            "type": "number",
                            "description": "Current quantity on hand (0 if gone/used up).",
                        },
                        "unit": {
                            "type": "string",
                            "description": "Unit if the user stated one; otherwise omit and we keep the known unit.",
                        },
                    },
                    "required": ["product", "qty"],
                },
            },
        },
        "required": ["items"],
    },
}


def _build_qty_items(raw_items: list[dict]) -> list[dict]:
    """Normalize update_pantry_quantity tool items into {product, qty, unit}."""
    out: list[dict] = []
    for it in raw_items:
        product = (it.get("product") or "").strip()
        if not product:
            continue
        out.append({
            "product": product,
            "qty": float(it.get("qty") or 0),
            "unit": (it.get("unit") or "").strip() or None,
        })
    return out


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

_RESTOCK_TOOL: dict = {
    "name": "restock_low_items",
    "description": (
        "The user wants to buy whatever is running low / restock the pantry / do a "
        "grocery run, WITHOUT naming specific items. Use for 'buy all items running "
        "low', 'order everything I'm low on', 'restock whatever I need', 'do a grocery "
        "run', 'top up the pantry', 'buy what I need'. This checks the pantry's current "
        "stock levels, builds a list of the low items, prices them on Amazon, and sends "
        "an approval list — the user confirms before anything is bought. You DO have "
        "access to their pantry stock; this is the tool that reads it and acts. Do NOT "
        "claim you can't see inventory — call this instead. (If they named specific "
        "items to buy, use request_purchase; if they're just asking what they're low on "
        "without buying, answer from the stock summary in your context.)"
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

# ── "It didn't arrive" tool (cancels in-transit so the item is needed again) ──

_NOT_ARRIVED_TOOL: dict = {
    "name": "report_not_arrived",
    "description": (
        "The user is telling you an order they placed did NOT arrive, or that they "
        "cancelled / want to cancel an order that was on the way — e.g. 'the milk never "
        "came', 'my eggs didn't show up', 'cancel that coffee order', 'that order fell "
        "through'. This takes those items off the 'on the way' list so they count as "
        "needed again. Use ONLY for items that were ordered and in transit — for normal "
        "on-hand corrections use update_pantry_quantity instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Products that didn't arrive / should be cancelled.",
                "items": {
                    "type": "object",
                    "properties": {"product": {"type": "string", "description": "Product name"}},
                    "required": ["product"],
                },
            },
        },
        "required": ["items"],
    },
}

_FRESH_TOOLS: list[dict] = [
    {
        "name": "request_purchase",
        "description": (
            "Buy specific item(s) the user named. Use this for ANY message that names a "
            "thing to get — even a bare noun or a vague amount: 'milk', 'we need paper "
            "towels', 'grab some coffee', 'a loaf of bread', 'order more dog food', "
            "'get Eggland's Best eggs'. A bare product name means qty 1 — just buy it, "
            "don't ask how much or which brand. Put any brand they named in that item's "
            "preferred_brand. (If they didn't name items but want to restock whatever's "
            "low, use restock_low_items instead.)"
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
    _RESTOCK_TOOL,
    _UPDATE_INVENTORY_TOOL,
    _NOT_ARRIVED_TOOL,
    _SCHEDULE_TOOL,
]

# ── Briefing-reply tools (pending cart context) ────────────────────────────────

_BRIEFING_TOOLS: list[dict] = [
    {
        "name": "approve_cart",
        "description": (
            "The user wants to check out the pending suggested cart as-is. Use ONLY when "
            "they refer to that pending list — 'yes', 'ok', 'approve', 'go ahead', 'buy "
            "it all', 'looks good', 'checkout my pending cart', 'check out my cart'. If "
            "they instead name specific items to buy, that's buy_items (a new cart), not "
            "this."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reject_cart",
        "description": (
            "The user doesn't want to buy anything this run and is done. "
            "Use when they say 'no', 'skip', 'cancel', 'not now', etc. — with NO named "
            "items and NO request to build another list. If they want a fresh suggested "
            "list, use reject_and_restart; if they name items to buy, use buy_items."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reject_and_restart",
        "description": (
            "The user wants you to throw away this suggestion and build a brand-new "
            "SUGGESTED list from scratch by re-checking their pantry. Use for 'redo my "
            "list', 'start over', 'make a new one', 'scrap this and rebuild' — when they "
            "do NOT name specific items. If they name items, that's buy_items."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "buy_items",
        "description": (
            "The user named specific item(s) they want to buy. Build a BRAND-NEW cart "
            "from exactly those items and set the pending suggested cart aside — naming "
            "items means they're telling you what they want instead of acting on the "
            "suggestion. Use for 'buy milk and eggs', 'just get the eggs', 'I need bread "
            "and butter', 'order more coffee', and for re-buys priced differently: a "
            "named brand ('get Eggland's Best eggs instead' → product 'eggs', "
            "preferred_brand 'Eggland's Best') or a cheaper option ('find a cheaper one' "
            "→ re-buy with no preferred_brand). Default qty to 1; never ask for a "
            "quantity or brand they didn't give — they tweak at the next approval step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": _ITEM_SCHEMA,
                    "description": "Items to put in the new cart",
                },
                "reason": {"type": "string", "description": "Brief reason (optional)"},
            },
            "required": ["items"],
        },
    },
    _UPDATE_INVENTORY_TOOL,
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
    """Write the grocery-approval message (Telegram HTML).

    The deterministic render (`_render_briefing_lines` + `_fallback_briefing`) is
    already exact and hallucination-proof, so we only spend a Haiku call when there
    is prose worth writing: a ``reason`` note explaining why the cart looks the way
    it does (e.g. extra items added to clear the free-shipping minimum). With no
    such note the deterministic render IS the answer — paying Haiku to reword item
    lines we'd only validate back against that same render is pure waste. On any
    error, or if the model drops the exact total, we fall back to the deterministic
    render so the briefing always goes out.
    """
    # No items, or no note to phrase as prose → ship the exact deterministic
    # render and skip the LLM entirely (the common, no-fillers path).
    if not items or not (reason and reason.strip()):
        return _fallback_briefing(items, total_usd, reason)

    facts = "\n".join(_render_briefing_lines(items))
    context = f"Context for this run: {reason}\n"
    system = (
        f"{PERSONA}\n\n"
        "You're texting the user their grocery list for approval — warm and natural, like "
        "a friend, not a receipt.\n\n"
        "Hard rules:\n"
        "• Use the EXACT items and prices given. Don't invent, drop, or re-price anything.\n"
        "• Show each item with its brand/variant (the names already include them) and its price.\n"
        "• State the exact total.\n"
        "• If the context note explains why extra items were added (e.g. to reach a "
        "free-shipping minimum), work that reasoning in naturally so the added items "
        "don't look random — but keep it to a sentence.\n"
        "• End by asking if it looks good, and make clear that if they approve you'll add "
        "everything to their Amazon cart and send a checkout link — you NEVER buy on their "
        "behalf, they finish checkout themselves.\n"
        "• Invite quick tweaks (swap a brand, drop one, add something)."
    )
    user = (
        f"{context}"
        f"Items (use these exact names and prices):\n{facts}\n\n"
        f"Total: ${total_usd:.2f}"
    )

    try:
        resp = await llm.get_client().messages.create(
            model=settings.model_fast,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        llm.record_usage(settings.model_fast, resp.usage, label="briefing")
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        # Guard against drift: the exact total must survive.
        if text and f"{total_usd:.2f}" in text:
            return text
        logger.warning("Briefing composer output missing total — using fallback")
    except Exception as exc:
        logger.warning("Briefing composition failed (%s) — using fallback", exc)

    return _fallback_briefing(items, total_usd, reason)


async def parse_request(message: str, stock_summary: str | None = None) -> dict:
    """Interpret a free-text message with no pending-cart context.

    ``stock_summary`` — optional plain-text snapshot of the user's current pantry
    (what's low / on hand) so the assistant can answer "what am I low on?" and
    route "buy everything low" correctly instead of claiming it can't see stock.

    Returns one of:
      {"action": "quick_buy", "items": [...], "reason": str}
      {"action": "start_grocery_run"}                 # restock everything low
      {"action": "update_inventory", "items": [{product, qty, unit}]}
      {"action": "update_schedule", "cron": str, "timezone": str, "description": str}
      {"action": "chat", "reply": str}
    """
    client = llm.get_client()
    pantry_context = (
        f"\n\nThe user's current pantry snapshot (you CAN see this — use it to answer "
        f"stock questions and to decide what's low):\n{stock_summary}\n"
        if stock_summary else ""
    )
    system = (
        f"{PERSONA}\n\n"
        "The user just sent a message with no grocery list pending. Route it:\n"
        "• request_purchase — they named item(s) to buy ('milk', 'we need paper towels', "
        "'a loaf of bread'). A bare noun = qty 1; just buy it, don't ask how much.\n"
        "• restock_low_items — they want to restock EVERYTHING low without naming items "
        "('buy all items running low', 'order what I need', 'do a grocery run'). You have "
        "full access to their pantry stock (below when available); never say you can't see "
        "their inventory.\n"
        "• update_pantry_quantity — they're telling you how much they CURRENTLY have, not "
        "buying ('we still have plenty of eggs', 'we're out of milk', 'the kids finished "
        "the bread'). This corrects the estimate; it buys nothing.\n"
        "• report_not_arrived — an order that was on the way did NOT arrive or is being "
        "cancelled ('the milk never came', 'cancel that coffee order'). Removes it from "
        "the on-the-way list so it counts as needed again.\n"
        "• update_schedule — change when/how often the briefing runs (convert to a UTC "
        "cron expression).\n"
        "• Otherwise just reply. Answer pantry questions ('what am I low on?') from the "
        "snapshot. If it's small talk or the intent is unclear, chat — don't trigger a "
        "purchase or a run on a guess."
        f"{pantry_context}"
    )
    response = await client.messages.create(
        model=settings.model_fast,
        max_tokens=512,
        system=system,
        tools=_FRESH_TOOLS,
        messages=[{"role": "user", "content": message}],
    )
    llm.record_usage(settings.model_fast, response.usage, label="parse_request")
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

        if block.name == "restock_low_items":
            return {"action": "start_grocery_run"}

        if block.name == "update_pantry_quantity":
            items = _build_qty_items(args.get("items", []))
            if items:
                return {"action": "update_inventory", "items": items}

        if block.name == "report_not_arrived":
            products = [
                (it.get("product") or "").strip()
                for it in args.get("items", [])
                if (it.get("product") or "").strip()
            ]
            if products:
                return {"action": "report_not_arrived", "items": products}

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
      {"action": "approve"}                           # check out the pending cart
      {"action": "reject"}                             # skip, done
      {"action": "reject_and_restart"}                # drop cart + rebuild suggestion
      {"action": "buy_items", "items": [...], "reason": str}  # new cart from named items
      {"action": "update_inventory", "items": [{product, qty, unit}]}  # correct stock + rebuild
      {"action": "chat", "reply": str}
    """
    cart_lines = "\n".join(
        f"  • {it['product']} × {it['qty']:g} {it['unit']} — ${it.get('price_usd') or 0:.2f}"
        for it in cart_items
    )
    total = sum(float(it.get("price_usd") or 0) * float(it.get("qty") or 1) for it in cart_items)

    system = (
        f"{PERSONA}\n\n"
        "The user just received their grocery briefing and replied. Interpret their reply "
        "in the context of the pending cart provided in the user message, and call the "
        "right tool.\n\n"
        "The guiding rule (least friction): if the user NAMES specific items to buy, "
        "they want a brand-new cart of just those items — call buy_items and the pending "
        "suggestion is set aside. Only an explicit approval of the pending list checks it "
        "out. Don't agonize over whether they want to keep the suggestion too — naming "
        "items always means a fresh cart.\n\n"
        "Tool guide:\n"
        "• approve_cart — check out the pending suggested cart ('yes', 'looks good', "
        "'approve', 'buy it all', 'checkout my pending cart')\n"
        "• buy_items — they named item(s) to buy → new cart from exactly those items "
        "('buy milk and eggs', 'just get the eggs', 'get Eggland's Best eggs instead', "
        "'find a cheaper one', 'I also need bread'). Put any named brand in that item's "
        "preferred_brand; for a cheaper re-buy leave preferred_brand off.\n"
        "• reject_cart — skip everything and done, with no named items ('no', 'not now')\n"
        "• reject_and_restart — scrap this suggestion and rebuild a NEW suggested list "
        "from the pantry, WITHOUT naming items ('redo it', 'start over')\n"
        "• update_pantry_quantity — the user is correcting how much they ACTUALLY have, "
        "not deciding on the cart: 'we still have plenty of eggs', 'the family used all "
        "the bread'. This means the suggestion used stale numbers, so we fix stock and "
        "rebuild — include every item they mention with its corrected quantity (0 if "
        "gone). Prefer this over reject when they state a real on-hand amount.\n"
        "• update_schedule — change when/how often the briefing runs."
    )
    # Cart + reply ride the user turn so the system+tools prefix stays byte-stable
    # (the cart used to live in the system prompt, which rewrote the prefix on every
    # briefing — cache-hostile). cacheable_system marks it; below Haiku's 4096-token
    # floor this no-ops today, but the stable structure is the prerequisite to ever
    # caching here.
    user = (
        f"Pending cart (${total:.2f} total):\n{cart_lines}\n\n"
        f"The user's reply: {message}"
    )
    response = await llm.get_client().messages.create(
        model=settings.model_fast,
        max_tokens=512,
        system=llm.cacheable_system(system),
        tools=_BRIEFING_TOOLS,
        messages=[{"role": "user", "content": user}],
    )
    llm.record_usage(settings.model_fast, response.usage, label="briefing_reply")

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

        if block.name == "buy_items":
            # Keep an empty list as buy_items (NOT reject) so the caller can ask which
            # items rather than silently skipping the pending cart on an ambiguous reply.
            items = [_build_item(it) for it in args.get("items", []) if it.get("product")]
            return {"action": "buy_items", "items": items, "reason": args.get("reason", "")}

        if block.name == "update_pantry_quantity":
            items = _build_qty_items(args.get("items", []))
            if items:
                return {"action": "update_inventory", "items": items}

        if block.name == "update_schedule":
            return {
                "action": "update_schedule",
                "cron": args.get("cron", "0 13 * * *"),
                "timezone": args.get("timezone", "America/New_York"),
                "description": args.get("description", ""),
            }

    reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    return {"action": "chat", "reply": reply or "Got it!"}
