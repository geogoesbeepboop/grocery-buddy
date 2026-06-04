"""MCP server — exposes pantry tools for local Claude Code development.

Run with: grocery-mcp  (or: uv run grocery-mcp)

In production the same logic in tools/ is called directly by workflow activities.
"""
from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP

from grocery_buddy.config import settings
from grocery_buddy.db import close_pool, get_pool
from grocery_buddy.tools.consumption import (
    get_consumption_profile,
    get_recent_consumption_events,
    upsert_consumption_profile,
)
from grocery_buddy.tools.inventory import (
    get_inventory,
    log_consumption_event,
    set_actual_quantity,
    upsert_inventory_item,
)

mcp = FastMCP("grocery-buddy")


# ── Inventory tools ───────────────────────────────────────────────────────────


@mcp.tool()
async def list_inventory(user_id: str) -> str:
    """List all pantry inventory items for a user."""
    pool = await get_pool()
    items = await get_inventory(pool, user_id)
    return json.dumps(items, default=str)


@mcp.tool()
async def set_inventory_item(
    user_id: str,
    product: str,
    qty: float,
    unit: str,
    par_level: float = 1.0,
) -> str:
    """Create or update an inventory item (current quantity + restock threshold)."""
    pool = await get_pool()
    result = await upsert_inventory_item(pool, user_id, product, qty, unit, par_level)
    return json.dumps(result, default=str)


@mcp.tool()
async def record_consumption(
    user_id: str,
    product: str,
    amount_consumed: float,
    unit: str,
) -> str:
    """Record that the user consumed some amount of a product. Updates inventory qty."""
    pool = await get_pool()
    await log_consumption_event(pool, user_id, product, -abs(amount_consumed), "user_update")
    return f"Recorded -{amount_consumed} {unit} of {product}"


@mcp.tool()
async def correct_inventory_quantity(
    user_id: str,
    product: str,
    qty: float,
    unit: str = "",
) -> str:
    """Reset an item to the quantity actually on hand (e.g. after a manual recount).

    Snaps the running estimate back to this confirmed amount and re-anchors when
    depletion is measured from. Use 0 if the item is gone/used up.
    """
    pool = await get_pool()
    result = await set_actual_quantity(pool, user_id, product, qty, unit or None)
    return json.dumps(result, default=str)


# ── Consumption profile tools ─────────────────────────────────────────────────


@mcp.tool()
async def list_consumption_habits(user_id: str) -> str:
    """List declared consumption habits (rates) for all tracked products."""
    pool = await get_pool()
    profiles = await get_consumption_profile(pool, user_id)
    return json.dumps(profiles, default=str)


@mcp.tool()
async def set_consumption_habit(
    user_id: str,
    product: str,
    daily_rate: float,
    unit: str,
    household_factor: float = 1.0,
    notes: str = "",
) -> str:
    """Declare how much of a product the household consumes per day."""
    pool = await get_pool()
    result = await upsert_consumption_profile(
        pool, user_id, product, daily_rate, unit, household_factor, notes
    )
    return json.dumps(result, default=str)


@mcp.tool()
async def list_consumption_events(user_id: str, lookback_days: int = 30) -> str:
    """List recent consumption events (purchases, user updates, inferred)."""
    pool = await get_pool()
    events = await get_recent_consumption_events(pool, user_id, lookback_days)
    return json.dumps(events, default=str)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
