"""QuickBuyWorkflow — ad-hoc "buy me X right now" requests.

This is the durable backend for the conversational feature: the user says
"I need eggs earlier than expected", an intent agent turns that into a
``QuickBuyInput``, and this workflow prices just those items, builds a cart, and
ALWAYS asks for approval before purchasing (ad-hoc buys are never auto-approved,
regardless of the spend cap).

Shares the same activities and the same approve/reject signals as
GroceryRunWorkflow, so the existing ntfy push + webhook approval path works
unchanged.

SANDBOX RULES: see grocery_run.py — no `from __future__`, no module-level
project imports outside the passthrough block, reference activities by string.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from grocery_buddy.config import settings
    from grocery_buddy.models import GroceryRunResult, QuickBuyInput
    from grocery_buddy.products import normalize_product

_ACTIVITY_TIMEOUT = timedelta(minutes=10)
_PURCHASE_TIMEOUT = timedelta(minutes=15)
_SHORT_TIMEOUT = timedelta(minutes=2)
_APPROVAL_WAIT = timedelta(hours=6)  # ad-hoc requests are time-sensitive
_CONFIRM_WAIT = timedelta(hours=settings.purchase_confirm_wait_hours)


def _parse_eta(eta_iso: str | None) -> datetime | None:
    """Parse an ISO ETA from the record activity into an aware datetime (or None)."""
    if not eta_iso:
        return None
    try:
        dt = datetime.fromisoformat(eta_iso)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

_STANDARD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
_NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class QuickBuyWorkflow:
    """Buy a specific, user-requested set of items — always behind approval."""

    def __init__(self) -> None:
        self._decision: str | None = None
        self._purchase_decision: str | None = None

    @workflow.signal
    async def approve(self) -> None:
        self._decision = "approved"

    @workflow.signal
    async def reject(self) -> None:
        self._decision = "rejected"

    @workflow.signal
    async def confirm_purchase(self) -> None:
        """The user placed the staged order — items are now on the way."""
        self._purchase_decision = "confirmed"

    @workflow.signal
    async def mark_not_purchased(self) -> None:
        """The user decided not to place the staged order after all."""
        self._purchase_decision = "not_purchased"

    async def _notify(self, message: str) -> None:
        """Best-effort user-facing message so a request never ends in silence."""
        await workflow.execute_activity(
            "notify_activity",
            {"message": message},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

    @workflow.run
    async def run(self, payload: QuickBuyInput) -> GroceryRunResult:
        # Top-level safety net: surface unexpected failures to the user instead of
        # leaving them waiting on a reply that never comes.
        try:
            return await self._run(payload)
        except Exception as exc:
            workflow.logger.error("Quick buy failed unexpectedly: %s", exc)
            try:
                await self._notify(
                    "Something went wrong while I was working on that order, and I couldn't "
                    "finish. Mind trying again in a few minutes?"
                )
            except Exception:
                pass
            raise

    async def _run(self, payload: QuickBuyInput) -> GroceryRunResult:
        user_id = payload.user_id
        workflow_id = workflow.info().workflow_id

        if not payload.items:
            await self._notify(
                "I couldn't tell what you wanted to buy — mind naming the item again?"
            )
            return GroceryRunResult(status="no_items_needed", message="No items requested")

        # ── 1. Load user data (brand prefs + known units/par levels) ──────────
        user_data = await workflow.execute_activity(
            "load_user_data",
            user_id,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        # Resolve units/qty from the user's profile (pure transformation). Key by
        # the canonical product name so a "Milk" request matches a stored "milk".
        units: dict[str, str] = {}
        for prof in user_data.get("profiles", []):
            units[normalize_product(prof["product"])] = prof.get("unit", "unit")
        for inv in user_data.get("inventory", []):
            units.setdefault(normalize_product(inv["product"]), inv.get("unit", "unit"))

        brand_prefs = {
            normalize_product(prof["product"]): {
                "preferred_brand": prof.get("preferred_brand"),
                "brand_flexibility": prof.get("brand_flexibility", "any"),
            }
            for prof in user_data.get("profiles", [])
        }

        # An inline brand override from the request ("get Eggland's Best instead")
        # wins over the stored profile preference, and implies strict matching.
        for it in payload.items:
            if it.preferred_brand:
                brand_prefs[normalize_product(it.product)] = {
                    "preferred_brand": it.preferred_brand,
                    "brand_flexibility": "strict",
                }

        items = [
            {
                "product": normalize_product(it.product),
                "unit": it.unit or units.get(normalize_product(it.product), "unit"),
                "par_level": it.qty,  # lookup activity buys "par_level" of each
            }
            for it in payload.items
        ]

        # ── 2. Price the requested items (brand-aware) ────────────────────────
        priced_items = await workflow.execute_activity(
            "lookup_amazon_prices",
            {"user_id": user_id, "items": items, "brand_prefs": brand_prefs},
            schedule_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        if not priced_items:
            names = ", ".join(it.product for it in payload.items)
            await self._notify(
                f"I looked but couldn't find {names} on Amazon right now — could be a hiccup "
                "on their end. Want me to try again in a bit?"
            )
            return GroceryRunResult(status="failed", message="Could not price the requested items")

        # ── 3. Build draft cart ───────────────────────────────────────────────
        cart = await workflow.execute_activity(
            "build_draft_cart",
            {"user_id": user_id, "priced_items": priced_items, "workflow_id": workflow_id},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        cart_id = cart["cart_id"]
        total_usd = cart["total_usd"]
        item_count = cart["item_count"]
        idempotency_key = f"quickbuy-{cart_id}"

        # ── 4. Always require approval ────────────────────────────────────────
        await workflow.execute_activity(
            "send_approval_notification",
            {
                "cart_id": cart_id,
                "total_usd": total_usd,
                "item_count": item_count,
                "workflow_id": workflow_id,
                "reason": payload.reason or "Quick buy request",
            },
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        await workflow.execute_activity(
            "update_cart_status",
            {"cart_id": cart_id, "status": "pending_approval"},
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )

        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=_APPROVAL_WAIT,
            )
        except asyncio.TimeoutError:
            self._decision = "expired"

        final_status = self._decision or "expired"
        workflow.logger.info("QuickBuy cart %s decision: %s", cart_id, final_status)

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
            return await self._await_purchase_confirmation(
                user_id, cart_id, float(user_data.get("lead_time_days", 2.0))
            )

        await workflow.execute_activity(
            "update_cart_status",
            {"cart_id": cart_id, "status": final_status},
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )
        return GroceryRunResult(status=final_status, cart_id=cart_id)

    async def _await_purchase_confirmation(
        self, user_id: str, cart_id: str, lead_time_days: float
    ) -> GroceryRunResult:
        """Close the loop after checkout — see GroceryRunWorkflow for the rationale.

        Quick-buys are the common "I need eggs early" path, so tracking them as
        in-transit is exactly what stops the next scheduled run from re-suggesting the
        same item.
        """
        try:
            await workflow.wait_condition(
                lambda: self._purchase_decision is not None,
                timeout=_CONFIRM_WAIT,
            )
        except asyncio.TimeoutError:
            workflow.logger.info("No purchase confirmation for quick-buy cart %s", cart_id)
            return GroceryRunResult(status="checkout_ready", cart_id=cart_id)

        if self._purchase_decision == "not_purchased":
            await workflow.execute_activity(
                "update_cart_status",
                {"cart_id": cart_id, "status": "rejected"},
                schedule_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_STANDARD_RETRY,
            )
            return GroceryRunResult(status="rejected", cart_id=cart_id)

        summary = await workflow.execute_activity(
            "record_replenishments_activity",
            {"cart_id": cart_id, "user_id": user_id, "lead_time_days": lead_time_days},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        eta = _parse_eta(summary.get("eta"))
        if eta is not None:
            delay = eta - workflow.now()
            if delay > timedelta(0):
                await workflow.sleep(delay)
        await workflow.execute_activity(
            "reconcile_arrivals_activity",
            user_id,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        return GroceryRunResult(status="purchased", cart_id=cart_id)
