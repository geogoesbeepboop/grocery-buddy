"""ImportHistoryWorkflow — bootstrap a pantry from Amazon order history.

Onboarding accelerator: instead of the user dictating their whole pantry, we read
their Amazon "Returns & Orders", synthesize a candidate grocery/household list with
Sonnet, and hand it back for review. Nothing is written to the live pantry here —
the workflow ends after staging the proposal and switching the user into the
review conversation. The user edits/confirms over Telegram (webhook), and only on
confirm does the pantry get populated + the first grocery run kick off.

Why a workflow: scraping is slow and brittle and synthesis is a model call — both
are exactly the durable, retryable I/O Temporal is for, and it keeps the webhook
turn fast (it just starts this and returns).

SANDBOX RULES: see grocery_run.py — no `from __future__`, no module-level project
imports outside the passthrough block, reference activities by string name.
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from grocery_buddy.models import (
        AMAZON_LOGIN_REQUIRED,
        GroceryRunResult,
        ImportHistoryInput,
    )

# Re-login can block on a human (entering a 2FA code, or finishing an interactive
# sign-in), so give it a generous window and a heartbeat so Temporal knows the
# activity is alive while it waits. Never retried — re-prompting would double up the
# Telegram asks; on failure we fall back to manual setup.
_LOGIN_TIMEOUT = timedelta(minutes=8)
_LOGIN_HEARTBEAT = timedelta(seconds=30)
_SCRAPE_TIMEOUT = timedelta(minutes=6)   # browser launch + multi-page navigation
_SYNTH_TIMEOUT = timedelta(minutes=3)
_SHORT_TIMEOUT = timedelta(minutes=2)

# Scraping/synthesis are best-effort and idempotent enough to retry a couple times,
# but we don't want to hammer Amazon — keep attempts low.
_SCRAPE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=2,
)
_STANDARD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
)
_NO_RETRY = RetryPolicy(maximum_attempts=1)


def _is_login_required(exc) -> bool:
    """True if ``exc`` is (or wraps) the scrape's 'Amazon login expired' signal.

    The activity raises ApplicationError(type=AMAZON_LOGIN_REQUIRED); Temporal
    delivers it to the workflow wrapped in an ActivityError, so we walk the cause
    chain looking for that type.
    """
    seen = 0
    cur = exc
    while cur is not None and seen < 6:
        if getattr(cur, "type", None) == AMAZON_LOGIN_REQUIRED:
            return True
        cur = getattr(cur, "cause", None) or getattr(cur, "__cause__", None)
        seen += 1
    return False


@workflow.defn
class ImportHistoryWorkflow:
    """Scrape → synthesize → stage a pantry proposal for review."""

    async def _notify(self, message: str) -> None:
        await workflow.execute_activity(
            "notify_activity",
            {"message": message},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )

    @workflow.run
    async def run(self, payload: ImportHistoryInput) -> GroceryRunResult:
        user_id = payload.user_id
        try:
            return await self._run(user_id)
        except Exception as exc:
            workflow.logger.error("Order-history import failed: %s", exc)
            if _is_login_required(exc):
                # We tried to sign in automatically and couldn't finish (e.g. the
                # 2FA code didn't arrive in time, or the login window wasn't
                # completed). Just invite a retry — no terminal commands.
                message = (
                    "I couldn't finish signing in to your Amazon account just now — "
                    "the security code may not have come through in time. Say /import "
                    "to try again, or tell me what's in your pantry to set up by hand."
                )
            else:
                message = (
                    "I couldn't read your Amazon orders just now — no worries, we can set "
                    "things up the quick way instead. Just tell me what's in your pantry "
                    "(item, how much, how often you use it), or say /start to begin."
                )
            try:
                await self._notify(message)
            except Exception:
                pass
            raise

    async def _run(self, user_id: str) -> GroceryRunResult:
        # ── 0. Make sure we can get into Amazon (self-healing re-login) ────────
        # No-op when the saved session is still valid; otherwise it re-authenticates
        # (stored credentials or an interactive window) and relays any 2FA prompt to
        # the user — so they never have to run a terminal command. Raises a
        # non-retryable login-required error if it can't, handled in run().
        await workflow.execute_activity(
            "ensure_amazon_login_activity",
            user_id,
            schedule_to_close_timeout=_LOGIN_TIMEOUT,
            heartbeat_timeout=_LOGIN_HEARTBEAT,
            retry_policy=_NO_RETRY,
        )

        # ── 1. Scrape the order history ───────────────────────────────────────
        orders = await workflow.execute_activity(
            "scrape_amazon_orders_activity",
            user_id,
            schedule_to_close_timeout=_SCRAPE_TIMEOUT,
            retry_policy=_SCRAPE_RETRY,
        )
        if not orders:
            await self._notify(
                "I looked through your Amazon account but couldn't pull any recent orders "
                "to learn from. Let's set up your pantry the quick way — just tell me what "
                "you keep on hand (item, how much, how often you use it)."
            )
            return GroceryRunResult(status="no_items_needed", message="No orders found")

        # ── 2. Synthesize a pantry proposal (Sonnet) ──────────────────────────
        items = await workflow.execute_activity(
            "synthesize_pantry_from_orders_activity",
            orders,
            schedule_to_close_timeout=_SYNTH_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        if not items:
            await self._notify(
                "I went through your Amazon orders but didn't spot a clear set of "
                "groceries you reorder. Let's do it the quick way — tell me what's in your "
                "pantry (item, how much, how often you use it)."
            )
            return GroceryRunResult(status="no_items_needed", message="Nothing to propose")

        # ── 3. Stage it + open the review conversation ────────────────────────
        result = await workflow.execute_activity(
            "present_import_proposal_activity",
            {"user_id": user_id, "items": items},
            schedule_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_STANDARD_RETRY,
        )
        workflow.logger.info(
            "Import proposal staged for %s (%d items)", user_id, result.get("item_count", 0)
        )
        return GroceryRunResult(
            status="import_proposed", message=f"{result.get('item_count', 0)} items proposed"
        )
