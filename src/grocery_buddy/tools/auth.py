"""Mailbox for relaying an Amazon 2FA code from the webhook to a waiting worker.

When automated re-login hits a one-time-code prompt, the worker activity — holding
the browser open on the OTP page — can't read the user's authenticator. It opens a
'pending' challenge here and asks the user for the code over Telegram; the webhook
writes the user's reply back here; the activity polls until it appears.

Two processes, one DB table as the channel: the webhook (user → DB) and the worker
activity (DB → Amazon).
"""
from __future__ import annotations

from uuid import UUID

import asyncpg


async def create_otp_challenge(pool: asyncpg.Pool, user_id: str) -> str:
    """Open a fresh pending OTP challenge, expiring any older un-consumed one.

    Returns the new challenge id, which the activity polls with
    :func:`read_answered_code`.
    """
    uid = UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE amazon_auth_challenges SET status = 'expired' "
                "WHERE user_id = $1 AND status IN ('pending', 'answered')",
                uid,
            )
            row = await conn.fetchrow(
                "INSERT INTO amazon_auth_challenges (user_id, kind, status) "
                "VALUES ($1, 'otp', 'pending') RETURNING id",
                uid,
            )
    return str(row["id"])


async def submit_otp_code(pool: asyncpg.Pool, user_id: str, code: str) -> bool:
    """Record the user's reply against their latest pending challenge.

    Returns True if a pending challenge was actually waiting (so the webhook knows
    the code was expected), False if there was nothing to answer (timed out / stale).
    """
    row = await pool.fetchrow(
        """
        UPDATE amazon_auth_challenges
        SET status = 'answered', code = $2, answered_at = NOW()
        WHERE id = (
            SELECT id FROM amazon_auth_challenges
            WHERE user_id = $1 AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        )
        RETURNING id
        """,
        UUID(user_id), code,
    )
    return row is not None


async def read_answered_code(pool: asyncpg.Pool, challenge_id: str) -> str | None:
    """Consume and return the code if this challenge has been answered, else None.

    Atomically flips 'answered' → 'consumed' so the code is read exactly once.
    """
    row = await pool.fetchrow(
        """
        UPDATE amazon_auth_challenges
        SET status = 'consumed'
        WHERE id = $1 AND status = 'answered'
        RETURNING code
        """,
        UUID(challenge_id),
    )
    return row["code"] if row else None


async def expire_challenge(pool: asyncpg.Pool, challenge_id: str) -> None:
    """Mark a challenge expired (the activity gave up waiting)."""
    await pool.execute(
        "UPDATE amazon_auth_challenges SET status = 'expired' "
        "WHERE id = $1 AND status IN ('pending', 'answered')",
        UUID(challenge_id),
    )
