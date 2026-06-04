"""Wipe a user's pantry state back to a clean slate.

Internal/testing utility behind the hidden ``/clear`` Telegram command. Removes
everything that makes a user "known" — inventory, declared habits, consumption
history, carts, any in-progress conversation, and staged import proposals — so the
next message re-triggers first-time onboarding. Intentionally NOT surfaced in any
user-facing command list, /status, /help, or docs.

It deliberately does NOT delete the ``users`` or ``preferences`` rows (the account
itself), nor any Temporal schedule — only the pantry data you'd want to reset
between test runs.
"""
from __future__ import annotations

import logging
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


async def clear_user_data(pool: asyncpg.Pool, user_id: str) -> dict[str, int]:
    """Delete all pantry/consumption/cart state for a user. Returns row counts wiped.

    ``carts`` cascades to ``cart_items``/``approvals``/``purchases`` via FKs, so a
    single delete there clears the whole purchase trail.
    """
    uid = UUID(user_id)
    counts: dict[str, int] = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in (
                "consumption_events",
                "consumption_profile",
                "inventory_items",
                "import_proposals",
                "carts",  # cascades to cart_items, approvals, purchases
            ):
                status = await conn.execute(
                    f"DELETE FROM {table} WHERE user_id = $1", uid
                )
                # asyncpg returns e.g. "DELETE 4" — pull the trailing count.
                counts[table] = int(status.rsplit(" ", 1)[-1] or 0)

            # Reset any in-progress conversation (onboarding/import) back to idle.
            await conn.execute(
                "UPDATE conversation_state SET mode = 'idle', messages = '[]'::jsonb, "
                "updated_at = NOW() WHERE user_id = $1",
                uid,
            )

    logger.info("Cleared user %s data: %s", user_id, counts)
    return counts
