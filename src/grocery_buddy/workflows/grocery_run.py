"""GroceryRunWorkflow — the durable orchestration heart of the agent.

Flow:
  load_user_data → predict_low_items → lookup_amazon_prices
  → build_draft_cart → [approval gate if over cap] → execute_purchase

SANDBOX RULES (Temporal Python SDK):
  - No `from __future__ import annotations` — breaks Temporal's type introspection.
  - No module-level imports of non-stdlib / project code outside the
    `workflow.unsafe.imports_passed_through()` block.
  - Use workflow.logger (not a module-level logger) inside workflow methods.
  - Activities are referenced by string name so their heavy deps (asyncpg,
    playwright, httpx) never get pulled into the sandbox.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from grocery_buddy.models import GroceryRunInput, GroceryRunResult

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
        self._decision: str | None = None  # set by approve/reject signals

    @workflow.signal
    async def approve(self) -> None:
        self._decision = "approved"

    @workflow.signal
    async def reject(self) -> None:
        self._decision = "rejected"

    async def _notify(self, message: str) -> None:
        """Best-effort user-facing message so a run never ends in silence."""
        await workflow.execute_activity(
            "notify_activity",
            {"message": message},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

    @workflow.run
    async def run(self, payload: GroceryRunInput) -> GroceryRunResult:
        # Top-level safety net: if any step fails unexpectedly (e.g. an activity
        # exhausts its retries), tell the user instead of leaving them waiting on a
        # reply that never comes.
        try:
            return await self._run(payload)
        except Exception as exc:
            workflow.logger.error("Grocery run failed unexpectedly: %s", exc)
            try:
                await self._notify(
                    "Something went wrong on my end and I couldn't finish that. "
                    "Mind trying again in a few minutes?"
                )
            except Exception:
                pass
            raise

    async def _run(self, payload: GroceryRunInput) -> GroceryRunResult:
        user_id = payload.user_id
        workflow_id = workflow.info().workflow_id
        is_scheduled = payload.trigger == "schedule"

        # ── 1. Load user data ─────────────────────────────────────────────────
        user_data = await workflow.execute_activity(
            "load_user_data",
            user_id,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        # ── 1b. Guardrails ────────────────────────────────────────────────────
        # Never stack on top of a cart still awaiting approval (would create a
        # confusing second pending cart). For user-initiated runs, tell them why
        # instead of going silent. For scheduled runs, stay quiet.
        if user_data.get("open_cart_exists"):
            workflow.logger.info("Skipping run for %s — a cart is awaiting approval", user_id)
            if not is_scheduled:
                await self._notify(
                    "You've still got a grocery list waiting for your okay — reply to that "
                    "one first (just say yes, no, or tell me what to change) and I'll take it "
                    "from there."
                )
            return GroceryRunResult(
                status="skipped", message="A previous cart is still awaiting your approval"
            )
        # The cooldown only exists to stop a high-frequency cron from spamming —
        # it must never block a run the user explicitly asked for.
        if is_scheduled and user_data.get("recent_run_exists"):
            workflow.logger.info("Skipping scheduled run for %s — within cooldown window", user_id)
            return GroceryRunResult(
                status="skipped", message="Already ran recently (within cooldown window)"
            )

        # ── 2. Predict low items ──────────────────────────────────────────────
        low_items = await workflow.execute_activity(
            "predict_low_items_activity",
            user_data,
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )

        if not low_items:
            workflow.logger.info("Pantry well stocked for user %s", user_id)
            if not is_scheduled:
                await self._notify(
                    "Good news — you look well stocked right now, so there's nothing I'd add "
                    "to a cart. I'll keep an eye on things."
                )
            return GroceryRunResult(status="no_items_needed", message="Pantry is well stocked")

        # ── 3. Amazon price lookup (brand-aware) ──────────────────────────────
        brand_prefs = {
            prof["product"]: {
                "preferred_brand": prof.get("preferred_brand"),
                "brand_flexibility": prof.get("brand_flexibility", "any"),
            }
            for prof in user_data.get("profiles", [])
        }
        priced_items = await workflow.execute_activity(
            "lookup_amazon_prices",
            {"user_id": user_id, "items": low_items, "brand_prefs": brand_prefs},
            schedule_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        if not priced_items:
            await self._notify(
                "I tried to price what you're low on, but couldn't pull anything usable from "
                "Amazon just now — might be a hiccup on their end. Try again in a few minutes?"
            )
            return GroceryRunResult(status="failed", message="Could not price any items on Amazon")

        # ── 4. Build draft cart ───────────────────────────────────────────────
        cart = await workflow.execute_activity(
            "build_draft_cart",
            {"user_id": user_id, "priced_items": priced_items, "workflow_id": workflow_id},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        cart_id: str = cart["cart_id"]
        total_usd: float = cart["total_usd"]
        item_count: int = cart["item_count"]
        idempotency_key = f"purchase-{cart_id}"

        workflow.logger.info(
            "Cart %s built: $%.2f (%d items)", cart_id, total_usd, item_count,
        )

        # ── 5. Always require approval ────────────────────────────────────────
        # We never auto-buy. The user must see the itemized list and explicitly
        # approve before we stage an Amazon cart + hand back a checkout link.
        await workflow.execute_activity(
            "send_approval_notification",
            {"cart_id": cart_id, "total_usd": total_usd, "item_count": item_count, "workflow_id": workflow_id},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        await workflow.execute_activity(
            "update_cart_status",
            {"cart_id": cart_id, "status": "pending_approval"},
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )

        # Durable wait: survives worker crashes; resumes when signal arrives
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=_APPROVAL_WAIT,
            )
        except asyncio.TimeoutError:
            self._decision = "expired"

        final_status = self._decision or "expired"
        workflow.logger.info("Cart %s decision: %s", cart_id, final_status)

        if final_status == "approved":
            await workflow.execute_activity(
                "update_cart_status",
                {"cart_id": cart_id, "status": "approved"},
                schedule_to_close_timeout=timedelta(minutes=1),
                retry_policy=_STANDARD_RETRY,
            )
            await workflow.execute_activity(
                "prepare_checkout_activity",
                {"cart_id": cart_id, "user_id": user_id, "idempotency_key": idempotency_key},
                schedule_to_close_timeout=_PURCHASE_TIMEOUT,
                retry_policy=_NO_RETRY,
            )
            await workflow.execute_activity(
                "run_evals_activity",
                {"user_id": user_id, "run_cost_usd": 0.0},
                schedule_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_STANDARD_RETRY,
            )
            return GroceryRunResult(status="checkout_ready", cart_id=cart_id)

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
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        return GroceryRunResult(status=final_status, cart_id=cart_id)
