"""Temporal worker — registers all workflows and activities, then runs forever."""
from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from grocery_buddy.config import settings
from grocery_buddy.db import close_pool
from grocery_buddy.workflows.activities import (
    build_draft_cart,
    execute_purchase_activity,
    load_user_data,
    lookup_amazon_prices,
    lookup_kroger_prices,
    predict_low_items_activity,
    send_approval_notification,
    send_purchase_confirmation_activity,
    update_cart_status,
)
from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[GroceryRunWorkflow],
        activities=[
            load_user_data,
            predict_low_items_activity,
            lookup_amazon_prices,
            lookup_kroger_prices,
            build_draft_cart,
            send_approval_notification,
            update_cart_status,
            execute_purchase_activity,
            send_purchase_confirmation_activity,
        ],
    )

    logger.info(
        "Worker started — task queue=%s, namespace=%s",
        settings.temporal_task_queue,
        settings.temporal_namespace,
    )
    async with worker:
        await asyncio.Event().wait()  # run forever


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
