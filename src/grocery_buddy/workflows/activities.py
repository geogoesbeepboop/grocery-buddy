"""Temporal activities — all I/O and side-effects live here (workflows stay pure)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime

from temporalio import activity

from grocery_buddy.config import settings
from grocery_buddy.db import get_pool
from grocery_buddy.models import AMAZON_LOGIN_REQUIRED
from grocery_buddy.notifications import (
    send_briefing,
    send_checkout_link,
    send_telegram_message,
)
from grocery_buddy.predictor import (
    ConsumptionEvent,
    ConsumptionProfile,
    InventoryItem,
    predict_low_items,
)
from grocery_buddy.tools.consumption import (
    get_consumption_profile,
    get_recent_consumption_events,
)
from grocery_buddy.tools.inventory import get_inventory

logger = logging.getLogger(__name__)


# ── User-facing notices (so a run never ends in silence) ──────────────────────


@activity.defn
async def notify_activity(payload: dict) -> None:
    """Send a plain message to the user.

    Used by the workflows to report no-op/skip/failure outcomes so the user is
    never left waiting on a reply that never comes.
    """
    message = payload.get("message", "").strip()
    if message:
        await send_telegram_message(message)


# ── Estimated depletion (scheduled "assume they kept eating") ─────────────────


@activity.defn
async def apply_estimated_depletion_activity(user_id: str) -> list[dict]:
    """Decay each item's estimated qty by assumed consumption since last reconciled.

    Runs at the top of a grocery checkup so prediction sees the freshest estimate.
    Idempotent in effect: it advances ``last_estimated_at`` only for items it
    actually decrements, so re-runs don't double-count.
    """
    from grocery_buddy.depletion import apply_estimated_depletion

    pool = await get_pool()
    return await apply_estimated_depletion(pool, user_id)


# ── T9: Data loading ──────────────────────────────────────────────────────────


@activity.defn
async def load_user_data(user_id: str) -> dict:
    """Load inventory, consumption profiles, recent events, and preferences."""
    pool = await get_pool()
    inventory = await get_inventory(pool, user_id)
    profiles = await get_consumption_profile(pool, user_id)
    events = await get_recent_consumption_events(pool, user_id, lookback_days=30)

    row = await pool.fetchrow(
        "SELECT * FROM preferences WHERE user_id = $1",
        uuid.UUID(user_id),
    )
    prefs = dict(row) if row else {}

    # Guardrail signals for scheduled runs (see GroceryRunWorkflow).
    open_cart_exists = bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM carts WHERE user_id = $1 AND status = 'pending_approval')",
        uuid.UUID(user_id),
    ))
    recent_run_exists = bool(await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM carts
            WHERE user_id = $1
              AND created_at > NOW() - ($2 || ' minutes')::INTERVAL
        )
        """,
        uuid.UUID(user_id), str(settings.run_cooldown_minutes),
    ))

    return {
        "user_id": user_id,
        "inventory": inventory,
        "profiles": profiles,
        "events": events,
        "auto_purchase_cap": float(prefs.get("auto_purchase_cap_usd", settings.auto_purchase_cap_usd)),
        "lead_time_days": float(prefs.get("lead_time_days", 2.0)),
        "buffer_days": float(prefs.get("buffer_days", 1.0)),
        "open_cart_exists": open_cart_exists,
        "recent_run_exists": recent_run_exists,
    }


# ── T6: Prediction ────────────────────────────────────────────────────────────


def _predictor_inputs(
    user_data: dict,
) -> tuple[list[InventoryItem], list[ConsumptionProfile], dict[str, list[ConsumptionEvent]]]:
    """Marshal a ``load_user_data`` dict into the predictor's dataclass inputs."""
    inventory = [
        InventoryItem(
            product=i["product"],
            qty=float(i["qty"]),
            unit=i["unit"],
            par_level=float(i["par_level"]),
        )
        for i in user_data["inventory"]
    ]
    profiles = [
        ConsumptionProfile(
            product=p["product"],
            declared_rate=float(p["declared_rate"]),
            unit=p["unit"],
            household_factor=float(p.get("household_factor", 1.0)),
        )
        for p in user_data["profiles"]
    ]
    events_by_product: dict[str, list[ConsumptionEvent]] = {}
    for e in user_data["events"]:
        ts = e["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        events_by_product.setdefault(e["product"], []).append(
            ConsumptionEvent(delta=float(e["delta"]), ts=ts, source=e.get("source", "user_update"))
        )
    return inventory, profiles, events_by_product


@activity.defn
async def predict_low_items_activity(user_data: dict) -> list[dict]:
    """Run the rule-based predictor; return list of low items."""
    inventory, profiles, events_by_product = _predictor_inputs(user_data)

    low = predict_low_items(
        inventory,
        profiles,
        events_by_product,
        lead_time_days=user_data.get("lead_time_days", 2.0),
        buffer_days=user_data.get("buffer_days", 1.0),
    )

    return [
        {
            "product": r.product,
            "qty": r.qty,
            "unit": r.unit,
            "days_remaining": r.days_remaining,
            "par_level": r.par_level,
        }
        for r in low
    ]


@activity.defn
async def select_run_candidates_activity(user_data: dict) -> dict:
    """Pick what to buy now + what to add to reach free shipping.

    Returns ``must_buy`` (everything running LOW) and ``fillers`` (the MEDIUM items
    due to deplete soonest, capped at ``free_shipping_max_fillers``). Each line is
    tagged with its ``tier`` so the assembler — after pricing — can keep all
    must-buys and pull in fillers only until the cart clears the threshold. Carries
    the threshold/cap through so the workflow needn't read settings in the sandbox.
    """
    import math

    from grocery_buddy.predictor import classify_stock_levels
    from grocery_buddy.runlist import FILLER, MUST_BUY, split_run_candidates

    inventory, profiles, events_by_product = _predictor_inputs(user_data)
    levels = classify_stock_levels(
        inventory,
        profiles,
        events_by_product,
        lead_time_days=user_data.get("lead_time_days", 2.0),
        buffer_days=user_data.get("buffer_days", 1.0),
    )

    max_fillers = int(settings.free_shipping_max_fillers)
    must_buy, fillers = split_run_candidates(levels, max_fillers)

    def _line(lv, tier: str) -> dict:
        days = lv.days_remaining
        # Non-finite days (no declared rate) don't survive JSON cleanly — send null
        # and let the assembler treat it as least-urgent.
        if days is None or math.isinf(days) or math.isnan(days):
            days = None
        return {
            "product": lv.product,
            "qty": lv.qty,
            "unit": lv.unit,
            "par_level": lv.par_level,
            "days_remaining": days,
            "tier": tier,
        }

    return {
        "must_buy": [_line(lv, MUST_BUY) for lv in must_buy],
        "fillers": [_line(lv, FILLER) for lv in fillers],
        "threshold_usd": float(settings.free_shipping_threshold_usd),
        "max_fillers": max_fillers,
    }


# ── T8: Amazon pricing ────────────────────────────────────────────────────────


@activity.defn
async def lookup_amazon_prices(payload: dict) -> list[dict]:
    """Look up Amazon prices for each low item, with brand-aware selection.

    For each item we pull the top candidates off Amazon search, then hand them to
    a Haiku call that picks the best match given the user's brand preference for
    that product (``brand_prefs[product] = {preferred_brand, brand_flexibility}``).
    """
    from grocery_buddy.automation.amazon import get_browser_context, search_grocery_price
    from grocery_buddy.products import normalize_product

    items = payload["items"]
    raw_prefs: dict[str, dict] = payload.get("brand_prefs", {})
    # Match brand prefs by canonical name so "Milk" prefs apply to a "milk" item.
    brand_prefs = {normalize_product(k): v for k, v in raw_prefs.items()}

    # Dedupe by canonical product name so a legacy "Milk"/"milk" pair (or any
    # case/whitespace variant) isn't searched, priced, and added to the cart twice.
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in items:
        key = normalize_product(item["product"])
        if key in seen:
            logger.info("Skipping duplicate item %r (already pricing %r)", item["product"], key)
            continue
        seen.add(key)
        deduped.append(item)
    items = deduped

    p, context = await get_browser_context()
    priced: list[dict] = []
    try:
        for item in items:
            candidates = await search_grocery_price(item["product"], context)
            if not candidates:
                logger.warning("No Amazon results for %r — skipping", item["product"])
                continue

            pref = brand_prefs.get(normalize_product(item["product"]), {})
            chosen, reason = await _select_candidate_by_brand(
                product=item["product"],
                candidates=candidates,
                preferred_brand=pref.get("preferred_brand"),
                brand_flexibility=pref.get("brand_flexibility", "any"),
            )
            if chosen is None:
                logger.info("Brand-strict match unavailable for %r — skipping", item["product"])
                continue

            logger.info("Selected %r for %r (%s)", chosen["product"], item["product"], reason)
            priced.append({
                "product": item["product"],
                "qty": item["par_level"],  # buy up to par
                "unit": item["unit"],
                "price_usd": chosen["price_usd"],
                "price_source": "amazon_scraped",
                "asin": chosen.get("asin"),
                "kroger_sku": None,
                # The actual Amazon listing we'd buy — this is what the user sees in
                # the briefing (the brand/variant matters, not just "milk").
                "notes": chosen["product"],
                # Carried through for the free-shipping assembler: which lines are
                # must-buys vs. fillers, and how soon each runs out.
                "tier": item.get("tier", "must_buy"),
                "days_remaining": item.get("days_remaining"),
            })
    finally:
        await context.close()
        await p.stop()

    return priced


def _cheapest(candidates: list[dict]) -> dict:
    return min(candidates, key=lambda c: c["price_usd"])


async def _select_candidate_by_brand(
    product: str,
    candidates: list[dict],
    preferred_brand: str | None,
    brand_flexibility: str,
) -> tuple[dict | None, str]:
    """Pick the best candidate for the user's brand preference.

    Returns ``(candidate, reason)``. ``candidate`` is None only when flexibility
    is 'strict' and no candidate matches the preferred brand.

    Short-circuits the LLM when there's nothing to reason about (no preference,
    or a single candidate) to keep cost near zero on the common path.
    """
    import anthropic

    # No preference or nothing to choose between → cheapest, no LLM call.
    if not preferred_brand or brand_flexibility == "any" or len(candidates) == 1:
        c = _cheapest(candidates)
        return c, "cheapest match"

    listing = "\n".join(
        f"{i}: {c['product']} — ${c['price_usd']:.2f}" for i, c in enumerate(candidates)
    )
    flex_rule = {
        "strict": (
            "Only choose a listing that is clearly the preferred brand. "
            "If none of them are the preferred brand, return -1."
        ),
        "prefer": (
            "Strongly prefer the preferred brand. If no listing is that brand, "
            "fall back to the cheapest reasonable match (do not return -1)."
        ),
    }.get(brand_flexibility, "Pick the cheapest reasonable match.")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        resp = await client.messages.create(
            model=settings.model_fast,
            max_tokens=256,
            system=(
                "You select the best grocery product listing for a shopper. "
                "Respond ONLY with a compact JSON object: "
                '{"index": <int>, "reason": "<short phrase>"}. '
                "index is the chosen listing number, or -1 if none qualify."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Product needed: {product}\n"
                    f"Preferred brand: {preferred_brand}\n"
                    f"Rule: {flex_rule}\n\n"
                    f"Listings:\n{listing}"
                ),
            }],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        # Tolerate fenced/extra text around the JSON.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        idx = int(data.get("index", -1))
        reason = str(data.get("reason", "")).strip() or "brand selection"
    except Exception as exc:
        logger.warning("Brand selection failed for %r (%s) — falling back", product, exc)
        idx, reason = -1, "fallback: cheapest"

    if idx == -1:
        if brand_flexibility == "strict":
            return None, reason
        return _cheapest(candidates), reason or "fallback: cheapest"
    if 0 <= idx < len(candidates):
        return candidates[idx], reason
    # Out-of-range index → safe fallback.
    return _cheapest(candidates), "fallback: cheapest"


# ── T13: Kroger price comparison ──────────────────────────────────────────────


@activity.defn
async def lookup_kroger_prices(payload: dict) -> list[dict]:
    """Fetch Kroger prices via their public Products API for price comparison."""
    import httpx

    items = payload["items"]
    kroger_token = payload.get("kroger_token", "")
    if not kroger_token:
        logger.info("No Kroger token — skipping price comparison")
        return []

    priced: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for item in items:
            try:
                resp = await client.get(
                    "https://api.kroger.com/v1/products",
                    params={"filter.term": item["product"], "filter.limit": 1},
                    headers={"Authorization": f"Bearer {kroger_token}", "Accept": "application/json"},
                )
                data = resp.json()
                products = data.get("data", [])
                if not products:
                    continue
                p = products[0]
                price_info = p.get("items", [{}])[0].get("price", {})
                reg_price = price_info.get("regular")
                if reg_price is None:
                    continue
                priced.append({
                    "product": p.get("description", item["product"]),
                    "qty": item["par_level"],
                    "unit": item["unit"],
                    "price_usd": float(reg_price),
                    "price_source": "kroger_api",
                    "asin": None,
                    "kroger_sku": p.get("productId"),
                    "notes": None,
                })
            except Exception as exc:
                logger.warning("Kroger price lookup failed for %r: %s", item["product"], exc)

    return priced


# ── Free-shipping assembly ────────────────────────────────────────────────────


@activity.defn
async def assemble_run_cart_activity(payload: dict) -> dict:
    """Trim priced candidates to the final cart that clears free shipping.

    Keeps every priced must-buy and adds the soonest-due fillers only until the
    total crosses the threshold. Returns the final line-items plus a human ``reason``
    (or None) the briefing uses to explain any added items.
    """
    from grocery_buddy.runlist import assemble_for_free_shipping

    priced = payload["priced_items"]
    threshold = float(payload.get("threshold_usd") or settings.free_shipping_threshold_usd)
    max_fillers = int(payload.get("max_fillers") or settings.free_shipping_max_fillers)

    final, reason = assemble_for_free_shipping(priced, threshold, max_fillers)
    logger.info(
        "Assembled run cart: %d of %d priced candidates (threshold $%.2f)",
        len(final), len(priced), threshold,
    )
    return {"items": final, "reason": reason}


# ── T9: Cart building ─────────────────────────────────────────────────────────


@activity.defn
async def build_draft_cart(payload: dict) -> dict:
    """Persist a draft cart to the DB and return its ID + total."""
    user_id = payload["user_id"]
    priced_items: list[dict] = payload["priced_items"]
    workflow_id: str = payload.get("workflow_id", "")

    total = sum(i["price_usd"] * i["qty"] for i in priced_items if i.get("price_usd"))
    pool = await get_pool()

    cart_row = await pool.fetchrow(
        """
        INSERT INTO carts (user_id, status, total_usd, retailer, workflow_id)
        VALUES ($1, 'draft', $2, 'amazon', $3)
        RETURNING id, total_usd
        """,
        uuid.UUID(user_id), round(total, 2), workflow_id,
    )
    cart_id = str(cart_row["id"])

    for item in priced_items:
        await pool.execute(
            """
            INSERT INTO cart_items
                (cart_id, product, qty, unit, price_usd, price_source, asin, kroger_sku, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(cart_id),
            item["product"], item["qty"], item["unit"],
            round(item.get("price_usd") or 0, 2),
            item.get("price_source"),
            item.get("asin"),
            item.get("kroger_sku"),
            item.get("notes"),
        )

    logger.info("Built draft cart %s: $%.2f (%d items)", cart_id, total, len(priced_items))
    return {"cart_id": cart_id, "total_usd": round(total, 2), "item_count": len(priced_items), "retailer": "amazon"}


# ── T10: Notifications ────────────────────────────────────────────────────────


@activity.defn
async def send_approval_notification(payload: dict) -> None:
    """Send the briefing notification. Fetches live cart items from DB for the full breakdown."""
    cart_id = payload["cart_id"]
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT product, qty, unit, price_usd, notes FROM cart_items WHERE cart_id = $1",
        uuid.UUID(cart_id),
    )
    items = [dict(r) for r in rows]
    await send_briefing(
        cart_id=cart_id,
        total_usd=payload["total_usd"],
        workflow_id=payload["workflow_id"],
        items=items,
        reason=payload.get("reason"),
    )


# ── T11/T12: Cart status + purchase ──────────────────────────────────────────


@activity.defn
async def update_cart_status(payload: dict) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE carts SET status = $1, updated_at = NOW() WHERE id = $2",
        payload["status"], uuid.UUID(payload["cart_id"]),
    )


@activity.defn
async def prepare_checkout_activity(payload: dict) -> dict:
    """Stage an Amazon cart for the approved items and return a checkout link.

    This does NOT place an order. It adds the approved items to the real Amazon
    cart (using the saved session) and hands back a checkout URL so the user can
    review and complete the purchase themselves. The user is the one who clicks
    "Place order" — we only get them to the doorstep.

    Idempotency: checks whether this cart was already staged before re-running —
    safe to retry. Returns the staging record.
    """
    from grocery_buddy.automation.amazon import (
        AMAZON_CART_URL,
        add_to_cart_by_asin,
        get_browser_context,
    )

    cart_id = payload["cart_id"]
    idempotency_key = payload["idempotency_key"]
    pool = await get_pool()

    # Idempotency guard — already staged a checkout for this cart.
    existing = await pool.fetchrow(
        "SELECT * FROM purchases WHERE idempotency_key = $1", idempotency_key
    )
    if existing and existing["status"] == "checkout_ready":
        logger.info("Checkout %s already staged — re-sending link", idempotency_key)
        total_usd = float(existing["total_usd"] or 0)
        await send_checkout_link(cart_id, total_usd, existing["retailer_order_ref"])
        return {"status": "checkout_ready", "idempotency_key": idempotency_key, "already_done": True}

    # Insert pending staging record
    purchase_row = await pool.fetchrow(
        """
        INSERT INTO purchases (cart_id, idempotency_key, status)
        VALUES ($1, $2, 'pending')
        ON CONFLICT (idempotency_key) DO UPDATE SET status = 'pending'
        RETURNING id
        """,
        uuid.UUID(cart_id), idempotency_key,
    )
    purchase_id = str(purchase_row["id"])

    # Fetch cart items
    items = await pool.fetch(
        "SELECT * FROM cart_items WHERE cart_id = $1", uuid.UUID(cart_id)
    )

    p, context = await get_browser_context()
    try:
        # Add each item to the Amazon cart. A single item failing shouldn't sink
        # the whole order — stage whatever we can and report it.
        added = 0
        for item in items:
            asin = item["asin"]
            if not asin:
                logger.warning("No ASIN for %s — cannot add to cart", item["product"])
                continue
            if await add_to_cart_by_asin(asin, context):
                added += 1
            else:
                logger.warning("Couldn't add %s (ASIN %s) to cart", item["product"], asin)

        if added == 0:
            raise RuntimeError("Couldn't add any items to the Amazon cart")

        # Hand back the account-scoped cart URL rather than driving into the
        # session-bound /gp/buy/spc/ checkout flow: that SPC URL belongs to this
        # Playwright session and makes the user re-authenticate (the double-auth)
        # when opened on their phone. The cart URL resolves against their own
        # signed-in Amazon — web or app — where these items now sit, so they land
        # one tap from checkout without a second login.
        checkout_url = AMAZON_CART_URL

        total_usd = float(await pool.fetchval(
            "SELECT total_usd FROM carts WHERE id = $1", uuid.UUID(cart_id)
        ) or 0)

        await pool.execute(
            """
            UPDATE purchases
            SET status = 'checkout_ready', retailer_order_ref = $2, total_usd = $3
            WHERE id = $1
            """,
            uuid.UUID(purchase_id), checkout_url, round(total_usd, 2),
        )
        await pool.execute(
            "UPDATE carts SET status = 'checkout_ready', updated_at = NOW() WHERE id = $1",
            uuid.UUID(cart_id),
        )

        await send_checkout_link(cart_id, total_usd, checkout_url)
        logger.info("Checkout staged for cart %s (%d items) → %s", cart_id, added, checkout_url)
        return {"status": "checkout_ready", "idempotency_key": idempotency_key, "total_usd": total_usd}

    except Exception as exc:
        await pool.execute(
            "UPDATE purchases SET status = 'failed', error = $2 WHERE id = $1",
            uuid.UUID(purchase_id), str(exc),
        )
        await pool.execute(
            "UPDATE carts SET status = 'failed', updated_at = NOW() WHERE id = $1",
            uuid.UUID(cart_id),
        )
        # Re-raise — the workflow's top-level handler sends the single user-facing
        # "something went wrong" message (avoids double-notifying + leaking detail).
        raise
    finally:
        await context.close()
        await p.stop()


@activity.defn
async def send_checkout_link_activity(payload: dict) -> None:
    await send_checkout_link(
        cart_id=payload["cart_id"],
        total_usd=payload["total_usd"],
        checkout_url=payload.get("checkout_url"),
    )


# ── T16: Evals + cost alert ───────────────────────────────────────────────────


@activity.defn
async def run_evals_activity(payload: dict) -> dict:
    """Compute prediction accuracy and emit scores to Langfuse."""
    from grocery_buddy.evals import run_evals, check_cost_alert
    user_id = payload["user_id"]
    run_cost_usd = payload.get("run_cost_usd", 0.0)

    await check_cost_alert(run_cost_usd, user_id)
    return await run_evals(user_id)


# ── Onboarding import: re-login → scrape → synthesize → stage proposal ────────


async def _relay_otp(user_id: str) -> str | None:
    """Ask the user for their Amazon 2FA code over Telegram; wait for the reply.

    Called from inside the login activity when Amazon prompts for a one-time code.
    Opens a relay challenge, switches the user into ``amazon_2fa`` conversation mode
    so the webhook routes their next message here, then polls the relay table until
    the code arrives or we run out of time. Heartbeats so the activity isn't reaped
    while a human is typing.
    """
    from grocery_buddy.tools.auth import (
        create_otp_challenge,
        expire_challenge,
        read_answered_code,
    )
    from grocery_buddy.tools.conversation import set_conversation

    pool = await get_pool()
    challenge_id = await create_otp_challenge(pool, user_id)
    await set_conversation(pool, user_id, "amazon_2fa", [])
    await send_telegram_message(
        "📲 Amazon sent you a one-time security code. Reply here with that code "
        "(just the digits) and I'll finish signing in."
    )

    interval = 3
    waited = 0
    try:
        while waited < settings.amazon_login_wait_seconds:
            code = await read_answered_code(pool, challenge_id)
            if code:
                digits = "".join(c for c in code if c.isdigit())
                return digits or None
            await asyncio.sleep(interval)
            waited += interval
            activity.heartbeat()
        return None
    finally:
        await expire_challenge(pool, challenge_id)
        # Drop back to idle so a late code reply isn't misread as a command.
        await set_conversation(pool, user_id, "idle", [])


@activity.defn
async def ensure_amazon_login_activity(user_id: str) -> dict:
    """Make sure the saved Amazon session is valid, self-healing it if not.

    Runs before the scrape, on the MAIN persistent profile so a successful login is
    saved for the scraper (and later pricing/checkout) to reuse. If the session is
    live, returns immediately. If it's expired it re-authenticates:

      • credentials configured → fill them (unattended, even on scheduled runs);
      • otherwise              → open a visible window for the user to sign in.

    A 2FA prompt is relayed to the user over Telegram. Raises a non-retryable
    login-required error if it still can't get in, so the import degrades to a clear
    message instead of silently scraping an empty (logged-out) page.
    """
    from temporalio.exceptions import ApplicationError

    from grocery_buddy.automation.amazon import get_browser_context, is_signed_out
    from grocery_buddy.automation.amazon_auth import (
        login_with_credentials,
        wait_for_interactive_login,
    )

    email = (settings.amazon_email or "").strip()
    password = (settings.amazon_password or "").strip()
    have_creds = bool(email and password)
    profile_name = (
        (settings.amazon_profile_name or settings.amazon_account_first_name or "").strip()
        or None
    )

    # ── 1) Probe the session with a normal browser — don't flash a visible window
    #       just to look. Heal in place if credentials are configured. ──
    p, context = await get_browser_context()
    try:
        if not await is_signed_out(context):
            return {"status": "already_logged_in"}

        if have_creds:
            await send_telegram_message(
                "🔐 Your Amazon session expired — signing you back in…"
            )
            ok = await login_with_credentials(
                context,
                email=email,
                password=password,
                get_otp=lambda: _relay_otp(user_id),
                profile_name=profile_name,
            )
            if ok:
                logger.info("Amazon session re-authenticated for %s (credentials)", user_id)
                return {"status": "reauthenticated"}

            # Automated fill didn't complete. On a headless host there's no window to
            # show, so surface the clear login-required error. When a window CAN be
            # shown (local/dev), fall through to the interactive path below so the
            # user can finish the sign-in by hand instead of staying stuck.
            logger.warning("Automated credential login did not complete for %s", user_id)
            if settings.amazon_headless:
                raise ApplicationError(
                    "Couldn't re-authenticate with Amazon.",
                    type=AMAZON_LOGIN_REQUIRED,
                    non_retryable=True,
                )
    finally:
        await context.close()
        await p.stop()

    # ── 2) Reached when signed out and either no credentials are configured, or the
    #       automated fill above didn't complete on a non-headless host. Open a
    #       VISIBLE window and wait for the user to sign in; the import resumes after. ──
    await send_telegram_message(
        "🔐 Your Amazon session expired. I opened a browser window — please sign in "
        "there and I'll pick the import back up automatically."
    )
    p, context = await get_browser_context(headless=False)
    try:
        ok = await wait_for_interactive_login(
            context,
            timeout_s=settings.amazon_login_wait_seconds,
            on_tick=activity.heartbeat,
        )
        if not ok:
            raise ApplicationError(
                "Interactive Amazon login wasn't completed in time.",
                type=AMAZON_LOGIN_REQUIRED,
                non_retryable=True,
            )
        logger.info("Amazon session re-authenticated for %s (interactive)", user_id)
        return {"status": "reauthenticated"}
    finally:
        await context.close()
        await p.stop()


@activity.defn
async def scrape_amazon_orders_activity(user_id: str) -> list[dict]:
    """Scrape the user's Amazon order history (Playwright). Returns raw orders.

    Assumes ``ensure_amazon_login_activity`` already ran, so the session is valid.
    Reads off the orders SEARCH listing scoped to ``AMAZON_ACCOUNT_FIRST_NAME`` (so
    we don't import other household profiles' purchases). Uses a temp copy of the
    auth session profile so this browser instance does not conflict with the pricing
    browser that holds the main profile's lock.
    """
    import shutil

    from grocery_buddy.automation.amazon import get_scraper_context, scrape_order_history

    search_name = (settings.amazon_account_first_name or "").strip() or None
    if not search_name:
        logger.warning(
            "AMAZON_ACCOUNT_FIRST_NAME is not set — importing the full unfiltered "
            "order history (may include other household profiles' purchases)."
        )

    pw, context, temp_dir = await get_scraper_context()
    try:
        return await scrape_order_history(
            context,
            search_name=search_name,
            max_pages=settings.amazon_import_max_pages,
            max_orders=settings.amazon_import_max_orders,
        )
    finally:
        await context.close()
        await pw.stop()
        shutil.rmtree(str(temp_dir), ignore_errors=True)


@activity.defn
async def synthesize_pantry_from_orders_activity(orders: list[dict]) -> list[dict]:
    """Sonnet synthesis: raw orders → proposed grocery/household pantry items."""
    from grocery_buddy.agents.order_history import synthesize_grocery_history

    return await synthesize_grocery_history(orders)


@activity.defn
async def present_import_proposal_activity(payload: dict) -> dict:
    """Stage the proposal, switch the user into import-review mode, and send it.

    The actual write to inventory/consumption happens later, only if the user
    confirms in the review conversation (handled by the webhook).
    """
    from grocery_buddy.agents.order_history import render_proposal
    from grocery_buddy.tools.conversation import set_conversation
    from grocery_buddy.tools.imports import create_import_proposal

    user_id = payload["user_id"]
    items = payload["items"]

    pool = await get_pool()
    proposal = await create_import_proposal(pool, user_id, items)
    # Empty transcript: the proposal itself lives in the review system prompt, so
    # the next inbound user message is the first turn of the edit conversation.
    await set_conversation(pool, user_id, "import_review", [])
    await send_telegram_message(render_proposal(items))
    return {"proposal_id": proposal["id"], "item_count": len(items)}
