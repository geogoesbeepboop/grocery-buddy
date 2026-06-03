"""Conversation state persistence for the (stateless) Telegram webhook.

Stores the active flow mode + Anthropic message transcript per user so multi-turn
conversations (onboarding) survive across separate webhook HTTP calls.
"""
from __future__ import annotations

import json
from uuid import UUID

import asyncpg


async def get_conversation(pool: asyncpg.Pool, user_id: str) -> tuple[str, list[dict]]:
    """Return (mode, messages). Defaults to ('idle', []) when no row exists."""
    row = await pool.fetchrow(
        "SELECT mode, messages FROM conversation_state WHERE user_id = $1",
        UUID(user_id),
    )
    if not row:
        return "idle", []
    messages = row["messages"]
    if isinstance(messages, str):
        messages = json.loads(messages)
    return row["mode"], (messages or [])


async def set_conversation(
    pool: asyncpg.Pool, user_id: str, mode: str, messages: list[dict]
) -> None:
    await pool.execute(
        """
        INSERT INTO conversation_state (user_id, mode, messages, updated_at)
        VALUES ($1, $2, $3::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET mode = EXCLUDED.mode, messages = EXCLUDED.messages, updated_at = NOW()
        """,
        UUID(user_id), mode, json.dumps(messages),
    )


async def clear_conversation(pool: asyncpg.Pool, user_id: str) -> None:
    await pool.execute(
        "UPDATE conversation_state SET mode = 'idle', messages = '[]'::jsonb, updated_at = NOW() "
        "WHERE user_id = $1",
        UUID(user_id),
    )


async def is_first_time(pool: asyncpg.Pool, user_id: str) -> bool:
    """True when the user has never set up any inventory or consumption habits."""
    inv = await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM inventory_items WHERE user_id = $1)", UUID(user_id)
    )
    prof = await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM consumption_profile WHERE user_id = $1)", UUID(user_id)
    )
    return not (inv or prof)
