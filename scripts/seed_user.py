"""Create the first user row and default preferences.

Usage:
  uv run python scripts/seed_user.py --email you@example.com --name "George"
"""
from __future__ import annotations

import argparse
import asyncio

from grocery_buddy.db import close_pool, get_pool


async def seed(email: str, name: str) -> None:
    pool = await get_pool()

    # Create user (idempotent by email)
    row = await pool.fetchrow(
        """
        INSERT INTO users (email, name)
        VALUES ($1, $2)
        ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, email, name, created_at
        """,
        email, name,
    )
    user_id = str(row["id"])

    # Create default preferences
    await pool.execute(
        """
        INSERT INTO preferences (user_id)
        VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
        """,
        row["id"],
    )

    await close_pool()

    print("\n✅ User ready")
    print(f"   id:    {user_id}")
    print(f"   email: {row['email']}")
    print(f"   name:  {row['name']}")
    print(f"\nUse this for all CLI commands:")
    print(f"   --user-id {user_id}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed initial user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    asyncio.run(seed(args.email, args.name))


if __name__ == "__main__":
    main()
