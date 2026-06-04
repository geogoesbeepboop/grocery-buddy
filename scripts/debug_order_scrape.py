"""Run JUST the Amazon order-history scrape and print the JSON it produced.

Use this to verify /import scraping (and iterate on selectors) without spinning up
Temporal + the full workflow. It reuses the saved login session and the same
scraper code the workflow calls.

  # uses AMAZON_ACCOUNT_FIRST_NAME from .env, watches the browser:
  AMAZON_HEADLESS=false uv run python scripts/debug_order_scrape.py

  # or override the search name / limits inline:
  uv run python scripts/debug_order_scrape.py --name George --max-orders 30 --max-pages 3

Tip: if you get 0 orders, run with AMAZON_HEADLESS=false and watch which page
loads — a login wall or a different card layout is the usual culprit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil

from grocery_buddy.automation.amazon import get_scraper_context, scrape_order_history
from grocery_buddy.config import settings


async def main() -> None:
    parser = argparse.ArgumentParser(description="Debug the Amazon order-history scrape")
    parser.add_argument(
        "--name", default=None,
        help="First name to search (defaults to AMAZON_ACCOUNT_FIRST_NAME from .env)",
    )
    parser.add_argument("--max-pages", type=int, default=settings.amazon_import_max_pages)
    parser.add_argument("--max-orders", type=int, default=settings.amazon_import_max_orders)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    search_name = args.name or (settings.amazon_account_first_name or "").strip() or None
    print(f"Scraping (search_name={search_name!r}, max_pages={args.max_pages}, "
          f"max_orders={args.max_orders}, headless={settings.amazon_headless})\n")

    pw, context, temp_dir = await get_scraper_context()
    try:
        orders = await scrape_order_history(
            context,
            search_name=search_name,
            max_pages=args.max_pages,
            max_orders=args.max_orders,
        )
    finally:
        await context.close()
        await pw.stop()
        shutil.rmtree(str(temp_dir), ignore_errors=True)

    item_count = sum(len(o["items"]) for o in orders)
    dated = sum(1 for o in orders if o.get("order_date"))
    print(f"\n=== {len(orders)} orders, {item_count} items, {dated} with a date ===")
    print(json.dumps(orders, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
