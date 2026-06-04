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
    """Create/replace an item from a user-declared quantity (onboarding, MCP set).

    A declared quantity is ground truth, so it anchors BOTH the working estimate
    (``qty``) and the last-confirmed actual (``actual_qty``), and resets
    ``last_estimated_at`` so scheduled depletion starts counting from now.
    """
    product = normalize_product(product)
    row = await pool.fetchrow(
        """
        INSERT INTO inventory_items
            (user_id, product, qty, actual_qty, unit, par_level,
             last_estimated_at, updated_at)
        VALUES ($1, $2, $3, $3, $4, $5, NOW(), NOW())
        ON CONFLICT (user_id, product) DO UPDATE
        SET qty = EXCLUDED.qty, actual_qty = EXCLUDED.qty, unit = EXCLUDED.unit,
            par_level = EXCLUDED.par_level, last_estimated_at = NOW(), updated_at = NOW()
        RETURNING *
        """,
        UUID(user_id), product, qty, unit, par_level,
    )
    return _row(row)


async def set_actual_quantity(
    pool: asyncpg.Pool,
    user_id: str,
    product: str,
    qty: float,
    unit: str | None = None,
) -> dict:
    """Reset an item to the quantity the user just confirmed they actually have.

    This is the on-the-fly correction path ("we still have a full dozen eggs",
    "the family used them all", "I barely touched the coffee this week"). It snaps
    the working estimate (``qty``) back to the truth, records that truth in
    ``actual_qty``, and re-anchors ``last_estimated_at`` so future depletion counts
    from now. The delta vs. the prior estimate is logged as a ``correction`` event
    for the audit trail — but, by source, it is excluded from the consumption-rate
    blend so a one-off ("family came over") never inflates the steady-state rate.

    Creates the item if it isn't tracked yet (preserving any existing unit/par).
    """
    product = normalize_product(product)
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT qty, unit, par_level FROM inventory_items "
                "WHERE user_id = $1 AND product = $2",
                UUID(user_id), product,
            )
            if existing:
                prev_qty = float(existing["qty"])
                resolved_unit = (unit or existing["unit"] or "unit")
                par_level = float(existing["par_level"])
            else:
                prev_qty = 0.0
                resolved_unit = unit or "unit"
                par_level = 1.0

            row = await conn.fetchrow(
                """
                INSERT INTO inventory_items
                    (user_id, product, qty, actual_qty, unit, par_level,
                     last_estimated_at, updated_at)
                VALUES ($1, $2, $3, $3, $4, $5, NOW(), NOW())
                ON CONFLICT (user_id, product) DO UPDATE
                SET qty = EXCLUDED.qty, actual_qty = EXCLUDED.qty, unit = EXCLUDED.unit,
                    last_estimated_at = NOW(), updated_at = NOW()
                RETURNING *
                """,
                UUID(user_id), product, qty, resolved_unit, par_level,
            )
            await conn.execute(
                "INSERT INTO consumption_events (user_id, product, delta, source) "
                "VALUES ($1, $2, $3, 'correction')",
                UUID(user_id), product, qty - prev_qty,
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
                SET qty = GREATEST(0, qty + $3),
                    last_estimated_at = NOW(), updated_at = NOW()
                WHERE user_id = $1 AND product = $2
                """,
                UUID(user_id), product, delta,
            )


def _row(r: asyncpg.Record) -> dict:
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in dict(r).items()}
