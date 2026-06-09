"""Conversational onboarding agent — seeds inventory and consumption habits.

Uses the Anthropic SDK directly with tool use for a guided intake conversation.
Run interactively via: grocery-buddy onboard --user-id <uuid>
"""
from __future__ import annotations

import logging

from grocery_buddy import llm
from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.tools.consumption import upsert_consumption_profile
from grocery_buddy.tools.inventory import upsert_inventory_item

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a friendly grocery assistant helping the user set up their pantry tracker —
quickly, with as little back-and-forth as possible.

Your goal: capture, in as few messages as you can, (a) what's in their pantry now and
(b) how fast they go through the things they buy regularly.

How to run the conversation:
- Your VERY FIRST message must LEAD with the Amazon-import shortcut as the recommended
  way to start: because we can see their Amazon account, you can read their recent orders
  and draft the whole pantry FOR them — brands, quantities, and how often they reorder —
  so they barely type anything. Make that the headline offer (they can just say "yes" or
  tap /import). Then, in one line, mention they can instead type their pantry by hand if
  they prefer. Don't bury the import option below a wall of instructions.
- If they accept the import (e.g. "yes", "import", "use my orders", "/import"), call
  import_amazon_orders. They'll get a draft to review and fix before anything is saved.
- If they'd rather type it out, invite them to dump their WHOLE pantry in ONE message as a
  free-form list. Tell them that for each item it helps to include — if they have it
  handy — the item, how much they have on hand, how often they eat/use it, and any brand
  preference. Make clear they can give as much or as little as they like; no need to wait
  for you to ask category by category.
- Parse whatever they send and save EVERY item immediately with the tools. A single
  message will usually contain many items — loop through all of them and call the tools
  for each one. Never make the user repeat something they already told you.
- Capture the SPECIFIC kind they buy, not just the category — the variant is what
  lets us buy the right thing later. Save the product name with its descriptor when
  they give one: "2% milk" (not just "milk"), "sourdough bread", "Oreos" rather than
  "cookies", "large brown eggs". If they're vague, that's fine — save what they said.
- Infer sensible values instead of interrogating:
    • Convert natural phrasing to a daily rate: "a dozen eggs lasts ~2 weeks" →
      declared_rate = 12/14 ≈ 0.86/day; "a gallon of milk a week" → ≈ 0.14/day.
    • If they give a quantity but no rate (or a rate but no quantity), save what you can
      and move on — partial data is fine.
    • Brand: default "any". "must be Brand X" → strict; "prefer Brand X but ok with
      cheaper" → prefer.
- Only ask follow-ups when something important is genuinely ambiguous, and batch ALL
  follow-ups into ONE short message. Do not go category-by-category or item-by-item.
- When the user has given what they want (or says they're done), call finish_onboarding.
  The user may ALSO ask to wrap up or to "do a grocery run now" partway through — if they
  do, save anything still outstanding and then call finish_onboarding. Finishing setup is
  exactly what kicks off their first grocery list and approval briefing, so never tell the
  user that a grocery run is out of scope.
- Be warm and concise.
"""

_TOOLS: list[dict] = [
    {
        "name": "save_inventory_item",
        "description": "Save an item currently in the pantry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product name (e.g., 'Eggs', 'Whole milk')"},
                "qty": {"type": "number", "description": "Current quantity on hand"},
                "unit": {"type": "string", "description": "Unit (e.g., 'dozen', 'oz', 'lbs', 'count', 'gallon')"},
                "par_level": {
                    "type": "number",
                    "description": "Minimum qty before they want to reorder (defaults to 1)",
                },
            },
            "required": ["product", "qty", "unit"],
        },
    },
    {
        "name": "save_consumption_habit",
        "description": "Save how frequently the household consumes an item.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "daily_rate": {
                    "type": "number",
                    "description": "Units consumed per day (e.g., 0.14 gallons/day = 1 gallon/week)",
                },
                "unit": {"type": "string"},
                "household_factor": {
                    "type": "number",
                    "description": "Multiplier for household size (1.0 = individual, 2.0 = couple, etc.)",
                },
                "notes": {"type": "string", "description": "Optional notes (e.g., 'more in summer')"},
                "preferred_brand": {
                    "type": "string",
                    "description": "Brand the user prefers for this product, if any (e.g., 'Cheerios', 'Horizon'). Omit if no preference.",
                },
                "brand_flexibility": {
                    "type": "string",
                    "enum": ["any", "prefer", "strict"],
                    "description": "How strict to be: 'any' = brand doesn't matter, 'prefer' = prefer the brand but cheaper alternatives ok, 'strict' = only the preferred brand. Defaults to 'any'.",
                },
            },
            "required": ["product", "daily_rate", "unit"],
        },
    },
    {
        "name": "import_amazon_orders",
        "description": (
            "Call this when the user wants you to set up their pantry from their Amazon "
            "order history instead of (or before) typing it out — e.g. 'import from "
            "Amazon', 'use my orders', 'yeah do that for me', or accepting the head-start "
            "offer. This kicks off reading their recent orders and drafting a pantry they "
            "can review. Do not also save items by hand in the same turn."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "finish_onboarding",
        "description": (
            "Call this when the user has finished giving pantry info, says they're done, "
            "or explicitly asks to wrap up / proceed / 'do a grocery run now'. Save any "
            "outstanding items with the other tools FIRST, then call this. After this, the "
            "system generates a grocery list from what was saved and sends an approval "
            "briefing — so calling this is exactly how the user's first grocery run starts."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def _handle_tool(pool, user_id: str, name: str, args: dict) -> str:
    if name == "save_inventory_item":
        result = await upsert_inventory_item(
            pool, user_id,
            product=args["product"],
            qty=args["qty"],
            unit=args["unit"],
            par_level=args.get("par_level", 1.0),
        )
        return f"Saved: {result['product']} — {result['qty']} {result['unit']}"

    if name == "save_consumption_habit":
        result = await upsert_consumption_profile(
            pool, user_id,
            product=args["product"],
            declared_rate=args["daily_rate"],
            unit=args["unit"],
            household_factor=args.get("household_factor", 1.0),
            notes=args.get("notes", ""),
            preferred_brand=args.get("preferred_brand"),
            brand_flexibility=args.get("brand_flexibility", "any"),
        )
        brand_note = ""
        if result.get("preferred_brand"):
            brand_note = f" [{result['preferred_brand']}, {result['brand_flexibility']}]"
        return f"Saved habit: {result['product']} — {result['declared_rate']}/day{brand_note}"

    return f"Unknown tool: {name}"


# Opener used to kick off a fresh onboarding conversation.
ONBOARDING_OPENER = "Hi! I'd like to set up my grocery tracking."


def _serialize_blocks(content_blocks: list) -> list[dict]:
    """Convert Anthropic SDK content blocks to JSON-safe dicts for persistence."""
    out: list[dict] = []
    for b in content_blocks:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": b.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


async def advance_onboarding(
    pool, user_id: str, messages: list[dict]
) -> tuple[str, list[dict], str]:
    """Run one onboarding turn to completion (handling any tool calls).

    ``messages`` must already include the latest user message. Returns
    ``(assistant_text, updated_messages, status)`` where ``status`` is:
      • "continue"      — keep the interview going
      • "done"          — user finished (finish_onboarding) → kick off first grocery run
      • "import_orders" — user asked to import from Amazon → caller starts that flow
    The transcript is kept JSON-serializable so callers can persist it between
    stateless webhook calls.
    """
    client = llm.get_client()

    while True:
        # Onboarding is structured intake (parse a free-form pantry dump into tool
        # calls) — well within Haiku's reach, and much cheaper than Sonnet.
        # _SYSTEM and _TOOLS are byte-stable, so a cache breakpoint on the last
        # message replays the whole growing prefix (tools+system+transcript) once
        # it clears Haiku's 4096-token floor on the later turns.
        response = await client.messages.create(
            model=settings.model_fast,
            max_tokens=2048,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=llm.with_transcript_cache(messages),
        )
        llm.record_usage(settings.model_fast, response.usage, label="onboarding")
        messages.append({"role": "assistant", "content": _serialize_blocks(response.content)})

        if response.stop_reason == "tool_use":
            tool_results = []
            finished = False
            import_orders = False
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if block.name == "finish_onboarding":
                    finished = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Onboarding complete — starting a grocery run now.",
                    })
                    continue
                if block.name == "import_amazon_orders":
                    import_orders = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Starting the Amazon order import now.",
                    })
                    continue
                result = await _handle_tool(pool, user_id, block.name, block.input)
                logger.debug("Tool %s → %s", block.name, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

            if import_orders:
                text = "\n".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                return (
                    text or "Great — let me read your recent Amazon orders…"
                ), messages, "import_orders"
            if finished:
                text = "\n".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                return (text or "Great — that's everything I need to get started!"), messages, "done"
            continue

        text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        # Fallback completion signal in case the model wraps up in prose instead of
        # calling finish_onboarding.
        status = "done" if "setup complete" in text.lower() else "continue"
        return text, messages, status


async def run_onboarding(user_id: str) -> None:
    """Interactive terminal-based onboarding conversation (CLI: grocery-buddy onboard)."""
    pool = await get_pool()
    messages: list[dict] = [{"role": "user", "content": ONBOARDING_OPENER}]

    print("\n🛒 Grocery Buddy Onboarding\n" + "─" * 40)

    while True:
        text, messages, status = await advance_onboarding(pool, user_id, messages)
        print(f"\nAssistant: {text}\n")
        if status == "import_orders":
            print(
                "ℹ️  Amazon order import runs over the Telegram bot (it needs the durable "
                "workflow + your live browser session). Let's continue here by hand — or "
                "use the bot to import.\n"
            )
            messages.append({
                "role": "user",
                "content": "Let's just set it up by hand here instead.",
            })
            continue
        if status == "done":
            print("✅ Onboarding complete!\n")
            break
        user_input = input("You: ").strip()
        if not user_input:
            continue
        messages.append({"role": "user", "content": user_input})
