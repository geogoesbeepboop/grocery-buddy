"""Conversational onboarding agent — seeds inventory and consumption habits.

Uses the Anthropic SDK directly with tool use for a guided intake conversation.
Run interactively via: grocery-buddy onboard --user-id <uuid>
"""
from __future__ import annotations

import json
import logging

import anthropic

from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.tools.consumption import upsert_consumption_profile
from grocery_buddy.tools.inventory import upsert_inventory_item

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a friendly grocery assistant helping the user set up their pantry tracker.

Your goal in this conversation:
1. Learn what the user currently has in their pantry (product, quantity, unit).
2. Learn their regular consumption habits (what they buy routinely and how fast they go through it).

Guidelines:
- Ask about one food category at a time (dairy, proteins, produce, pantry staples, etc.).
- When you have enough info for an item, save it immediately with the tools — don't batch.
- For consumption rate, help the user think in practical terms:
  "a dozen eggs lasts about 2 weeks" → declared_rate = 12/14 ≈ 0.86 eggs/day.
- When you've covered the major categories, say exactly: "Setup complete!"
- Be concise and conversational.
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
            },
            "required": ["product", "daily_rate", "unit"],
        },
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
        )
        return f"Saved habit: {result['product']} — {result['declared_rate']}/day"

    return f"Unknown tool: {name}"


async def run_onboarding(user_id: str) -> None:
    """Interactive terminal-based onboarding conversation."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    pool = await get_pool()

    messages: list[dict] = [
        {"role": "user", "content": "Hi! I'd like to set up my grocery tracking."},
    ]

    print("\n🛒 Grocery Buddy Onboarding\n" + "─" * 40)

    while True:
        response = await client.messages.create(
            model=settings.model_smart,
            max_tokens=2048,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await _handle_tool(pool, user_id, block.name, block.input)
                    logger.debug("Tool %s → %s", block.name, result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn — print assistant text
        text_parts = [b.text for b in response.content if hasattr(b, "text")]
        text = "\n".join(text_parts)
        print(f"\nAssistant: {text}\n")

        if "setup complete" in text.lower():
            print("✅ Onboarding complete!\n")
            break

        user_input = input("You: ").strip()
        if not user_input:
            continue
        messages.append({"role": "user", "content": user_input})
