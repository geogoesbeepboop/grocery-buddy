"""GroceryRunWorkflow — the durable orchestration heart of the agent.

Flow:
  apply_estimated_depletion → load_user_data → select_run_candidates
  → lookup_amazon_prices → assemble_run_cart → build_draft_cart
  → approval gate (always) → [if approved] prepare_checkout_activity (stage cart +
  checkout link; never places the order) → run_evals

SANDBOX RULES (Temporal Python SDK):
  - No `from __future__ import annotations` — breaks Temporal's type introspection.
  - No module-level imports of non-stdlib / project code outside the
    `workflow.unsafe.imports_passed_through()` block.
  - Use workflow.logger (not a module-level logger) inside workflow methods.
  - Activities are referenced by string name so their heavy deps (asyncpg,
    playwright, httpx) never get pulled into the sandbox.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from grocery_buddy.config import settings
    from grocery_buddy.models import GroceryRunInput, GroceryRunResult

_ACTIVITY_TIMEOUT = timedelta(minutes=10)
_PURCHASE_TIMEOUT = timedelta(minutes=15)
_SHORT_TIMEOUT = timedelta(minutes=2)
_APPROVAL_WAIT = timedelta(hours=24)
# How long to keep a durable ear open for "I placed the order" after staging checkout.
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
class GroceryRunWorkflow:
    """Orchestrates one full grocery cycle for a single user."""

    def __init__(self) -> None:
        self._decision: str | None = None  # set by approve/reject signals
        self._purchase_decision: str | None = None  # set after checkout: confirmed/not_purchased

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

        # ── 0a. Land anything that arrived ─────────────────────────────────────
        # Any in-transit order whose ETA has passed becomes on-hand stock before we
        # predict, so the pantry reflects deliveries even if the per-order delivery
        # timer was missed (worker down, etc.). Idempotent — lands each row once.
        await workflow.execute_activity(
            "reconcile_arrivals_activity",
            user_id,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        # ── 0b. Assume consumption since we last looked ────────────────────────
        # Decay each item's estimated qty by (rate × days elapsed) so prediction
        # works off the freshest estimate. The user's last confirmed actual_qty is
        # untouched — corrections snap the estimate back to it.
        await workflow.execute_activity(
            "apply_estimated_depletion_activity",
            user_id,
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

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

        # ── 2. Pick what to buy: low items now + soonest mediums to clear free
        #       shipping. We never run on fillers alone — if nothing's actually low,
        #       there's no order to round out and we don't nag. ──────────────────
        candidates = await workflow.execute_activity(
            "select_run_candidates_activity",
            user_data,
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=_STANDARD_RETRY,
        )
        must_buy = candidates.get("must_buy", [])
        fillers = candidates.get("fillers", [])

        if not must_buy:
            workflow.logger.info("Pantry well stocked for user %s", user_id)
            if not is_scheduled:
                await self._notify(
                    "Good news — you look well stocked right now, so there's nothing I'd add "
                    "to a cart. I'll keep an eye on things."
                )
            return GroceryRunResult(status="no_items_needed", message="Pantry is well stocked")

        # ── 3. Amazon price lookup (brand-aware) — must-buys + capped fillers in
        #       one browser session. ────────────────────────────────────────────
        brand_prefs = {
            prof["product"]: {
                "preferred_brand": prof.get("preferred_brand"),
                "brand_flexibility": prof.get("brand_flexibility", "any"),
            }
            for prof in user_data.get("profiles", [])
        }
        priced_items = await workflow.execute_activity(
            "lookup_amazon_prices",
            {"user_id": user_id, "items": must_buy + fillers, "brand_prefs": brand_prefs},
            schedule_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        if not priced_items:
            await self._notify(
                "I tried to price what you're low on, but couldn't pull anything usable from "
                "Amazon just now — might be a hiccup on their end. Try again in a few minutes?"
            )
            return GroceryRunResult(status="failed", message="Could not price any items on Amazon")

        # ── 3b. Assemble the cart: keep every must-buy, add fillers only until we
        #        clear the free-shipping minimum. ``reason`` explains any extras. ──
        assembled = await workflow.execute_activity(
            "assemble_run_cart_activity",
            {
                "priced_items": priced_items,
                "threshold_usd": candidates.get("threshold_usd"),
                "max_fillers": candidates.get("max_fillers"),
            },
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        final_items = assembled.get("items", [])
        run_reason = assembled.get("reason")

        if not final_items:
            # Fillers may have priced, but no must-buy did — don't ship a cart of
            # things that aren't actually running out.
            await self._notify(
                "I tried to price what you're low on, but couldn't pull anything usable from "
                "Amazon just now — might be a hiccup on their end. Try again in a few minutes?"
            )
            return GroceryRunResult(
                status="failed", message="Could not price the low items on Amazon"
            )

        # ── 4. Build draft cart ───────────────────────────────────────────────
        cart = await workflow.execute_activity(
            "build_draft_cart",
            {"user_id": user_id, "priced_items": final_items, "workflow_id": workflow_id},
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
            {
                "cart_id": cart_id,
                "total_usd": total_usd,
                "item_count": item_count,
                "workflow_id": workflow_id,
                "reason": run_reason,
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
            # The cart is staged and the user has a checkout link. Stay alive to close
            # the loop: wait for "I placed the order", then track the delivery and top
            # up the pantry when it lands.
            return await self._await_purchase_confirmation(
                user_id, cart_id, float(user_data.get("lead_time_days", 2.0))
            )

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

    async def _await_purchase_confirmation(
        self, user_id: str, cart_id: str, lead_time_days: float
    ) -> GroceryRunResult:
        """Close the loop after checkout: confirm → in-transit → delivered → restock.

        Waits (durably) for the user to confirm they placed the staged order. The
        webhook records the in-transit items immediately on the tap, so this is the
        elegant half: on confirm we (idempotently) ensure they're recorded, then sleep
        a durable timer until the estimated arrival and top up the pantry. We never
        assume an order happened — silence just leaves the cart checkout_ready.
        """
        try:
            await workflow.wait_condition(
                lambda: self._purchase_decision is not None,
                timeout=_CONFIRM_WAIT,
            )
        except asyncio.TimeoutError:
            workflow.logger.info("No confirmation for cart %s — leaving checkout_ready", cart_id)
            return GroceryRunResult(status="checkout_ready", cart_id=cart_id)

        if self._purchase_decision == "not_purchased":
            await workflow.execute_activity(
                "update_cart_status",
                {"cart_id": cart_id, "status": "rejected"},
                schedule_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_STANDARD_RETRY,
            )
            return GroceryRunResult(status="rejected", cart_id=cart_id)

        # Confirmed. Ensure the items are logged as in-transit (idempotent — the
        # webhook usually did this already on the button tap).
        summary = await workflow.execute_activity(
            "record_replenishments_activity",
            {"cart_id": cart_id, "user_id": user_id, "lead_time_days": lead_time_days},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

        # Durable delivery timer: sleep until the estimated arrival, then reconcile.
        # The run-start reconcile is the safety net; this gives a timely "it arrived"
        # nudge without waiting for the next scheduled run.
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
