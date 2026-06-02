"""Consumption profile CRUD — declared habits and recent events."""
from __future__ import annotations

from uuid import UUID

import asyncpg


async def get_consumption_profile(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM consumption_profile WHERE user_id = $1 ORDER BY product",
        UUID(user_id),
    )
    return [_row(r) for r in rows]


async def upsert_consumption_profile(
    pool: asyncpg.Pool,
    user_id: str,
    product: str,
    declared_rate: float,
    unit: str,
    household_factor: float = 1.0,
    notes: str = "",
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO consumption_profile
            (user_id, product, declared_rate, unit, household_factor, notes, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (user_id, product) DO UPDATE
        SET declared_rate = EXCLUDED.declared_rate,
            unit = EXCLUDED.unit,
            household_factor = EXCLUDED.household_factor,
            notes = EXCLUDED.notes,
            updated_at = NOW()
        RETURNING *
        """,
        UUID(user_id), product, declared_rate, unit, household_factor, notes,
    )
    return _row(row)


async def get_recent_consumption_events(
    pool: asyncpg.Pool, user_id: str, lookback_days: int = 30
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM consumption_events
        WHERE user_id = $1
          AND ts >= NOW() - ($2 || ' days')::INTERVAL
        ORDER BY product, ts DESC
        """,
        UUID(user_id), str(lookback_days),
    )
    return [_row(r) for r in rows]


def _row(r: asyncpg.Record) -> dict:
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in dict(r).items()}
