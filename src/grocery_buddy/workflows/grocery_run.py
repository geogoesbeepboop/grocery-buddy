"""GroceryRunWorkflow — the durable orchestration heart of the agent.

Flow:
  load_user_data → predict_low_items → lookup_amazon_prices
  → build_draft_cart → [approval gate if over cap] → execute_purchase
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import logging
    from grocery_buddy.models import GroceryRunInput, GroceryRunResult

logger = logging.getLogger(__name__)

# Activities are imported by name at runtime — never imported at module level in
# a workflow (would break Temporal's sandbox). We reference them as strings.
_ACTIVITY_TIMEOUT = timedelta(minutes=10)
_PURCHASE_TIMEOUT = timedelta(minutes=15)
_SHORT_TIMEOUT = timedelta(minutes=2)
_APPROVAL_WAIT = timedelta(hours=24)

_STANDARD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
_NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class GroceryRunWorkflow:
    """Orchestrates one full grocery cycle for a single user."""

    def __init__(self) -> None:
        self._decision: str | None = None  # "approved" | "rejected"

    @workflow.signal
    async def approve(self) -> None:
        self._decision = "approved"

    @workflow.signal
    async def reject(self) -> None:
        self._decision = "rejected"

    @workflow.run
    async def run(self, payload: GroceryRunInput) -> GroceryRunResult:
        user_id = payload.user_id
        workflow_id = workflow.info().workflow_id

        # ── 1. Load user data ─────────────────────────────────────────────────
        user_data = await workflow.execute_activity(
            "load_user_data",
            user_id,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        # ── 2. Predict low items ──────────────────────────────────────────────
        low_items = await workflow.execute_activity(
            "predict_low_items_activity",
            user_data,
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )

        if not low_items:
            return GroceryRunResult(status="no_items_needed", message="Pantry is well stocked")

        # ── 3. Price lookup (Amazon + optional Kroger comparison) ─────────────
        price_payload = {"user_id": user_id, "items": low_items}
        priced_items = await workflow.execute_activity(
            "lookup_amazon_prices",
            price_payload,
            schedule_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        if not priced_items:
            return GroceryRunResult(status="failed", message="Could not price any items on Amazon")

        # ── 4. Build draft cart ───────────────────────────────────────────────
        cart_payload = {
            "user_id": user_id,
            "priced_items": priced_items,
            "workflow_id": workflow_id,
        }
        cart = await workflow.execute_activity(
            "build_draft_cart",
            cart_payload,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        cart_id = cart["cart_id"]
        total_usd = cart["total_usd"]
        item_count = cart["item_count"]
        auto_cap = user_data.get("auto_purchase_cap", 50.0)

        idempotency_key = f"purchase-{cart_id}"

        # ── 5. Auto-purchase or approval gate ─────────────────────────────────
        if total_usd <= auto_cap:
            # Under cap → execute immediately without asking
            purchase_payload = {
                "cart_id": cart_id,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
            result = await workflow.execute_activity(
                "execute_purchase_activity",
                purchase_payload,
                schedule_to_close_timeout=_PURCHASE_TIMEOUT,
                retry_policy=_NO_RETRY,  # idempotency is at app level
            )
            await workflow.execute_activity(
                "run_evals_activity",
                {"user_id": user_id, "run_cost_usd": 0.0},
                schedule_to_close_timeout=timedelta(minutes=2),
                retry_policy=_STANDARD_RETRY,
            )
            return GroceryRunResult(
                status="purchased",
                cart_id=cart_id,
                message=f"Auto-purchased ${total_usd:.2f} (under ${auto_cap:.0f} cap)",
            )

        # Over cap → send approval push and wait for signal
        notif_payload = {
            "cart_id": cart_id,
            "total_usd": total_usd,
            "item_count": item_count,
            "workflow_id": workflow_id,
        }
        await workflow.execute_activity(
            "send_approval_notification",
            notif_payload,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        await workflow.execute_activity(
            "update_cart_status",
            {"cart_id": cart_id, "status": "pending_approval"},
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )

        # Durable wait: up to 24 hours for the user to tap Approve / Reject
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=_APPROVAL_WAIT,
            )
        except asyncio.TimeoutError:
            self._decision = "expired"

        final_status = self._decision or "expired"

        if final_status == "approved":
            await workflow.execute_activity(
                "update_cart_status",
                {"cart_id": cart_id, "status": "approved"},
                schedule_to_close_timeout=timedelta(minutes=1),
                retry_policy=_STANDARD_RETRY,
            )
            purchase_payload = {
                "cart_id": cart_id,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
            await workflow.execute_activity(
                "execute_purchase_activity",
                purchase_payload,
                schedule_to_close_timeout=_PURCHASE_TIMEOUT,
                retry_policy=_NO_RETRY,
            )
            await workflow.execute_activity(
                "run_evals_activity",
                {"user_id": user_id, "run_cost_usd": 0.0},
                schedule_to_close_timeout=timedelta(minutes=2),
                retry_policy=_STANDARD_RETRY,
            )
            return GroceryRunResult(status="purchased", cart_id=cart_id)

        # Rejected or expired
        await workflow.execute_activity(
            "update_cart_status",
            {"cart_id": cart_id, "status": final_status},
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )
        await workflow.execute_activity(
            "run_evals_activity",
            {"user_id": user_id, "run_cost_usd": 0.0},
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=_STANDARD_RETRY,
        )
        return GroceryRunResult(status=final_status, cart_id=cart_id)
