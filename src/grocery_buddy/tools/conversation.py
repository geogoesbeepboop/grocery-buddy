"""Conversation state persistence for the (stateless) Telegram webhook.

Stores the active flow mode + Anthropic message transcript per user so multi-turn
conversations (onboarding) survive across separate webhook HTTP calls.
"""
from __future__ import annotations

import json
from uuid import UUID

import asyncpg

from grocery_buddy.config import settings


def _is_clean_user_start(msg: dict) -> bool:
    """True if ``msg`` is a valid first message: a user turn that is not a
    tool_result (a tool_result must follow its assistant tool_use, so a window can
    never *begin* with one)."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return True


def truncate_messages(messages: list[dict], max_messages: int | None = None) -> list[dict]:
    """Keep only the last ``max_messages`` of a transcript (rolling window).

    The persisted transcript (onboarding / import review) is replayed to Claude in
    full every webhook turn, so an unbounded transcript means unbounded, uncached
    token growth. We keep the most recent window and trim from the FRONT to a clean
    user turn, so the replay never starts on an assistant message or an orphaned
    tool_result (which the Messages API rejects). The tail is always intact, so the
    most recent complete turn is preserved.
    """
    cap = settings.conversation_max_messages if max_messages is None else max_messages
    if cap <= 0 or len(messages) <= cap:
        return messages
    trimmed = messages[-cap:]
    while trimmed and not _is_clean_user_start(trimmed[0]):
        trimmed = trimmed[1:]
    return trimmed or messages[-1:]


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
        UUID(user_id), mode, json.dumps(truncate_messages(messages)),
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
