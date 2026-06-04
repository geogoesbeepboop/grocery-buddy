"""Staging CRUD for order-history import proposals.

A proposal is the candidate pantry + habits list synthesized from a user's Amazon
order history. It lives in ``import_proposals`` while the user reviews and edits it
conversationally, and is only written into ``inventory_items`` /
``consumption_profile`` once they confirm — so nothing touches the live pantry
until the user signs off.

Each item in ``items`` (JSONB array) is shaped like::

    {
      "product": "large brown eggs",   # canonical generic name
      "unit": "count",
      "estimated_qty": 6,              # on-hand estimate given last order + rate
      "par_level": 12,                 # reorder threshold
      "daily_rate": 0.86,             # units/day inferred from reorder cadence
      "preferred_brand": "Eggland's Best" | null,
      "brand_flexibility": "prefer",  # any | prefer | strict
      "last_ordered": "2026-05-03" | null,
      "times_ordered": 4,
      "category": "dairy & eggs"
    }
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

import asyncpg

from grocery_buddy.products import normalize_product

logger = logging.getLogger(__name__)


async def create_import_proposal(
    pool: asyncpg.Pool, user_id: str, items: list[dict], source: str = "amazon_orders"
) -> dict:
    """Stage a fresh proposal, discarding any earlier pending one for this user."""
    uid = UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE import_proposals SET status = 'discarded', updated_at = NOW() "
                "WHERE user_id = $1 AND status = 'pending_review'",
                uid,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO import_proposals (user_id, source, items, status)
                VALUES ($1, $2, $3::jsonb, 'pending_review')
                RETURNING *
                """,
                uid, source, json.dumps(items),
            )
    return _row(row)


async def get_active_import_proposal(pool: asyncpg.Pool, user_id: str) -> dict | None:
    """Return the user's most recent pending-review proposal, or None."""
    row = await pool.fetchrow(
        """
        SELECT * FROM import_proposals
        WHERE user_id = $1 AND status = 'pending_review'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        UUID(user_id),
    )
    return _row(row) if row else None


async def update_proposal_items(
    pool: asyncpg.Pool, proposal_id: str, items: list[dict]
) -> None:
    await pool.execute(
        "UPDATE import_proposals SET items = $2::jsonb, updated_at = NOW() WHERE id = $1",
        UUID(proposal_id), json.dumps(items),
    )


async def set_proposal_status(pool: asyncpg.Pool, proposal_id: str, status: str) -> None:
    await pool.execute(
        "UPDATE import_proposals SET status = $2, updated_at = NOW() WHERE id = $1",
        UUID(proposal_id), status,
    )


# ── Pure edit application (testable, no I/O) ──────────────────────────────────


def apply_edits(
    items: list[dict],
    *,
    remove: list[str] | None = None,
    update: list[dict] | None = None,
    add: list[dict] | None = None,
) -> list[dict]:
    """Apply review edits to a proposal's item list, matching by canonical name.

    - ``remove``: product names to drop.
    - ``update``: dicts with ``product`` plus any fields to overwrite.
    - ``add``: new item dicts to append (or overwrite if the product already exists).
    Returns a new list; does not mutate the input.
    """
    by_key: dict[str, dict] = {normalize_product(it["product"]): dict(it) for it in items}

    for name in remove or []:
        by_key.pop(normalize_product(name), None)

    for upd in update or []:
        key = normalize_product(upd.get("product", ""))
        if not key or key not in by_key:
            continue
        for field, value in upd.items():
            if field == "product" or value is None:
                continue
            by_key[key][field] = value

    for new_item in add or []:
        key = normalize_product(new_item.get("product", ""))
        if not key:
            continue
        merged = {**by_key.get(key, {}), **{k: v for k, v in new_item.items() if v is not None}}
        merged["product"] = new_item["product"]
        by_key[key] = merged

    return list(by_key.values())


def _row(r: asyncpg.Record) -> dict:
    out: dict = {}
    for k, v in dict(r).items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif k == "items" and isinstance(v, str):
            out[k] = json.loads(v)
        else:
            out[k] = v
    return out
