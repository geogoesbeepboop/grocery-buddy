"""Inventory CRUD — used by MCP server, workflow activities, and onboarding agent."""
from __future__ import annotations

from uuid import UUID

import asyncpg

from grocery_buddy.products import normalize_product


async def get_inventory(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM inventory_items WHERE user_id = $1 ORDER BY product",
        UUID(user_id),
    )
    return [_row(r) for r in rows]


async def upsert_inventory_item(
    pool: asyncpg.Pool,
    user_id: str,
    product: str,
    qty: float,
    unit: str,
    par_level: float = 1.0,
) -> dict:
    product = normalize_product(product)
    row = await pool.fetchrow(
        """
        INSERT INTO inventory_items (user_id, product, qty, unit, par_level, updated_at)
        VALUES ($1, $2, $3, $4, $5, NOW())
        ON CONFLICT (user_id, product) DO UPDATE
        SET qty = EXCLUDED.qty, unit = EXCLUDED.unit,
            par_level = EXCLUDED.par_level, updated_at = NOW()
        RETURNING *
        """,
        UUID(user_id), product, qty, unit, par_level,
    )
    return _row(row)


async def log_consumption_event(
    pool: asyncpg.Pool,
    user_id: str,
    product: str,
    delta: float,
    source: str = "user_update",
) -> None:
    """Record a consumption event and update the inventory qty accordingly."""
    product = normalize_product(product)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO consumption_events (user_id, product, delta, source) VALUES ($1, $2, $3, $4)",
                UUID(user_id), product, delta, source,
            )
            await conn.execute(
                """
                UPDATE inventory_items
                SET qty = GREATEST(0, qty + $3), updated_at = NOW()
                WHERE user_id = $1 AND product = $2
                """,
                UUID(user_id), product, delta,
            )


def _row(r: asyncpg.Record) -> dict:
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in dict(r).items()}
