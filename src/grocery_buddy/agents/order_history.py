"""Order-history import agent — turns raw Amazon orders into a reviewable pantry.

Two stages:

  synthesize_grocery_history(orders)
      Sonnet reads the messy scraped order list (titles + dates) and proposes a
      clean grocery/household pantry: product, brand, pack/unit, an inferred daily
      consumption rate from reorder cadence, and an on-hand estimate. This is the
      "complex" step the user flagged for the smarter model.

  advance_import_review(...)
      A lightweight (Haiku) conversational loop where the user edits the proposal
      ("drop the donuts", "remove all the unhealthy snacks, I'm on a diet",
      "I get the oat milk weekly now") and finally confirms. Nothing is written to
      the live pantry until they confirm — the caller persists from the staged list.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from html import escape

from grocery_buddy import llm
from grocery_buddy.config import settings
from grocery_buddy.tools.imports import apply_edits, update_proposal_items

logger = logging.getLogger(__name__)


# ── Stage 1: synthesis (Sonnet) ───────────────────────────────────────────────

_PROPOSE_TOOL: dict = {
    "name": "propose_pantry",
    "description": "Return the synthesized grocery/household pantry proposal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "Clean generic product name with key descriptor, "
                            "e.g. 'large brown eggs', '2% milk', 'sourdough bread'. Lowercase.",
                        },
                        "unit": {"type": "string", "description": "count, oz, lb, gallon, pack, etc."},
                        "estimated_qty": {
                            "type": "number",
                            "description": "REALISTIC units physically on hand TODAY — NOT how much "
                            "was ordered. Perishables (milk, eggs, bread, produce, yogurt, meat) "
                            "past their shelf life since the last order = 0. Non-perishables: "
                            "deplete from the last order by daily_rate over days_since_last. When in "
                            "doubt, estimate LOW (better to reorder than assume stock they lack).",
                        },
                        "par_level": {
                            "type": "number",
                            "description": "Reorder threshold — roughly one typical order's worth.",
                        },
                        "daily_rate": {
                            "type": "number",
                            "description": "Units consumed per day — the HABIT, always captured for "
                            "an included item even if they're out now. Infer from cadence: total "
                            "units ÷ days between first and last order. If ordered once, estimate a "
                            "sensible rate for that product type.",
                        },
                        "perishable": {
                            "type": "boolean",
                            "description": "True for foods that spoil within weeks (dairy, eggs, "
                            "bread, fresh produce, meat). Drives the on-hand estimate above.",
                        },
                        "preferred_brand": {
                            "type": "string",
                            "description": "Brand from the order title if clear; omit if generic/unclear.",
                        },
                        "brand_flexibility": {
                            "type": "string",
                            "enum": ["any", "prefer", "strict"],
                            "description": "'prefer' when they consistently buy one brand, else 'any'.",
                        },
                        "last_ordered": {
                            "type": "string",
                            "description": "Most recent order date for this product, ISO YYYY-MM-DD.",
                        },
                        "times_ordered": {"type": "integer", "description": "How many orders included it."},
                        "category": {
                            "type": "string",
                            "description": "Short category label, e.g. 'dairy & eggs', 'snacks', "
                            "'cleaning', 'beverages', 'pantry staples'.",
                        },
                    },
                    "required": ["product", "unit", "estimated_qty", "par_level", "daily_rate"],
                },
            },
        },
        "required": ["items"],
    },
}

_SYNTH_SYSTEM = """\
You are bootstrapping a household's grocery/pantry tracker from their Amazon order
history. You are given a list of PRODUCTS they've ordered, already aggregated: the
title, how many separate orders included it (times_ordered), total units bought,
the order dates, and how many days ago the most recent order was (days_since_last).
Today's date is provided.

Make TWO separate judgments for every product:

1) IS IT A RECURRING CONSUMABLE WORTH TRACKING? (what to include)
   - INCLUDE groceries, food, drinks, and household consumables a home restocks:
     pantry staples, snacks, beverages, paper goods, cleaning, toiletries, pet,
     baby. Being ordered 2+ times, or in multiple units, is strong evidence.
   - EXCLUDE durable or one-off items even if grocery-adjacent: clothing, bedding,
     towels, electronics, kitchenware, books, toys, gifts, tools, a supplement or
     gadget bought a single time. When unsure, LEAVE IT OUT — a tight, trustworthy
     list beats a noisy one.

2) LEARN THE HABIT vs ESTIMATE WHAT'S ON HAND — keep these SEPARATE:
   - HABIT (always capture for an included item, even if they're out now):
     daily_rate, preferred_brand, brand_flexibility, unit, par_level. A long-ago
     purchase still teaches the habit — milk bought 3 months ago tells you "this
     household drinks ~X of 2% [brand] milk per week." Capture that regardless.
   - ON-HAND NOW (estimated_qty): a REALISTIC count of what is physically in the
     pantry TODAY — NOT how much was ordered. Old stock is gone.
       * PERISHABLES (milk, eggs, bread, fresh produce, yogurt, meat, deli): if the
         last order is older than the item's realistic shelf life, estimated_qty = 0
         and set perishable=true. Nobody has 3-month-old milk; only count it if
         ordered recently enough to plausibly still be good.
       * NON-PERISHABLES (canned/dry goods, paper towels, detergent, coffee, rice):
         deplete from the last order — estimated_qty ≈
         max(0, last_order_units − daily_rate × days_since_last).
       * When in doubt, estimate LOW. Under-stating on-hand just means we reorder;
         over-stating means they run out.

Other rules:
- ONE entry per product. Set times_ordered and last_ordered (ISO YYYY-MM-DD) from
  the data given.
- daily_rate: infer from cadence — total units ÷ days between first and last order.
  Ordered once → estimate a sensible rate for that product type.
- Clean, lowercase generic product name with the key descriptor; pull brand/size
  from the title (e.g. "Eggland's Best Large Brown Eggs, 12 Count" → product
  'large brown eggs', preferred_brand "Eggland's Best", unit 'count'). Set
  brand_flexibility 'prefer' when they stick to one brand, else 'any'.
- par_level ≈ one typical order's worth.

Return the result ONLY by calling the propose_pantry tool. Do not write prose.\
"""


_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y")


def _parse_date(raw: str | None):
    """Parse a scraped order date ('June 3, 2026') into a date, or None."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _aggregate_orders(orders: list[dict], today: date) -> list[dict]:
    """Collapse raw scraped orders into one compact record per product.

    Keying on ASIN (falling back to a title prefix) merges every order of the same
    product, so the model receives cadence-rich, de-duplicated records instead of
    150 raw rows. This both shrinks the prompt and bounds the model's output to one
    entry per distinct product — which is what was overflowing max_tokens before.
    """
    groups: dict[str, dict] = {}
    for order in orders:
        d = _parse_date(order.get("order_date"))
        seen_in_order: set[str] = set()  # count each product once per ORDER
        for it in order.get("items", []):
            title = (it.get("title") or "").strip()
            if not title:
                continue
            asin = it.get("asin")
            key = asin or title[:60].lower()
            g = groups.setdefault(key, {
                "title": title, "times_ordered": 0, "total_units": 0, "_dates": [],
            })
            g["total_units"] += max(1, int(it.get("qty") or 1))
            if key not in seen_in_order:
                seen_in_order.add(key)
                g["times_ordered"] += 1
                if d:
                    g["_dates"].append(d)

    records: list[dict] = []
    for g in groups.values():
        dates = sorted(g.pop("_dates"))
        if dates:
            g["first_ordered"] = dates[0].isoformat()
            g["last_ordered"] = dates[-1].isoformat()
            g["days_since_last"] = (today - dates[-1]).days
            g["span_days"] = (dates[-1] - dates[0]).days
            # Most-recent dates only, so a very frequent item doesn't bloat the prompt.
            g["order_dates"] = [dt.isoformat() for dt in dates[-8:]]
        records.append(g)

    # Most-ordered first — the strongest staples lead, and if we ever truncate the
    # list for a giant history, we keep the highest-signal items.
    records.sort(key=lambda r: (r.get("times_ordered", 0), r.get("total_units", 0)), reverse=True)
    return records


# Sonnet 4.x emits up to 64k output tokens; stay just under the ceiling so a huge
# history still fits without the API rejecting the request.
_SYNTH_MAX_OUTPUT_CEILING = 60_000
_SYNTH_TOKENS_PER_PRODUCT = 240  # one proposed pantry entry as JSON, with headroom
_SYNTH_BASE_TOKENS = 4_000       # fixed overhead + room for a partial trailing entry


def _synthesis_token_budget(product_count: int) -> int:
    """Output-token budget for the proposal, scaled to the history size.

    A fixed 8k budget silently truncated large imports (100+ products → empty
    pantry). We instead budget ~240 tokens per product plus overhead so a 200-item
    history (~52k) still completes, capped below Sonnet's max output. An explicit
    ``AMAZON_IMPORT_SYNTHESIS_MAX_TOKENS`` overrides the auto-scaling.
    """
    override = settings.amazon_import_synthesis_max_tokens
    if override and override > 0:
        return min(override, _SYNTH_MAX_OUTPUT_CEILING)
    scaled = _SYNTH_BASE_TOKENS + _SYNTH_TOKENS_PER_PRODUCT * max(1, product_count)
    return max(8_192, min(scaled, _SYNTH_MAX_OUTPUT_CEILING))


async def synthesize_grocery_history(orders: list[dict]) -> list[dict]:
    """Run Sonnet over scraped orders; return a list of proposed pantry items.

    Returns [] if there's nothing usable. Never raises — failures degrade to an
    empty proposal so the caller can fall back to manual onboarding.
    """
    if not orders:
        return []

    today = datetime.now(UTC).date()
    products = _aggregate_orders(orders, today)
    if not products:
        logger.warning("Order-history synthesis: no products after aggregation")
        return []

    payload = {
        "today": today.isoformat(),
        "product_count": len(products),
        "products": products,
    }
    user = (
        f"Today's date is {today.isoformat()}.\n\n"
        "Here are the household's ordered products (JSON), already aggregated per "
        "product. Synthesize the recurring grocery/household pantry and call "
        "propose_pantry:\n\n"
        f"{json.dumps(payload, default=str)}"
    )

    # Budget output to the size of the history so a long order list (200+ items)
    # never truncates the proposal mid-tool-call (the old fixed 8k dropped 100-item
    # imports to zero proposed items).
    max_tokens = _synthesis_token_budget(len(products))

    try:
        client = llm.get_client()
        # Stream the response: a large max_tokens generation can run past the
        # non-streaming request window, and the stream helper assembles the final
        # tool_use block for us regardless of how long the proposal gets.
        async with client.messages.stream(
            model=settings.model_smart,  # complex parsing/synthesis → Sonnet
            max_tokens=max_tokens,
            system=_SYNTH_SYSTEM,
            tools=[_PROPOSE_TOOL],
            tool_choice={"type": "tool", "name": "propose_pantry"},
            messages=[{"role": "user", "content": user}],
        ) as stream:
            resp = await stream.get_final_message()
        # Record tokens/cost for the streaming call (create_message can't wrap a stream).
        await llm.record_usage(settings.model_smart, resp.usage, "synthesize_grocery_history")
    except Exception as exc:
        logger.warning("Order-history synthesis failed: %s", exc)
        return []

    if resp.stop_reason == "max_tokens":
        # Truncated mid-tool-call despite the scaled budget: the JSON is likely
        # unparseable and items is empty. Log loudly so this never silently degrades
        # to "no groceries found" — the fix is a bigger budget, not fewer orders.
        logger.warning(
            "Order-history synthesis hit max_tokens with %d products at a %d-token "
            "budget — proposal may be truncated; raise AMAZON_IMPORT_SYNTHESIS_MAX_TOKENS",
            len(products), max_tokens,
        )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_pantry":
            items = _clean_items((block.input or {}).get("items", []))
            logger.info(
                "Order-history synthesis: %d products → %d proposed pantry items "
                "(stop_reason=%s)", len(products), len(items), resp.stop_reason,
            )
            return items

    logger.warning(
        "Order-history synthesis returned no propose_pantry call (stop_reason=%s)",
        resp.stop_reason,
    )
    return []


def _clean_items(items: list[dict]) -> list[dict]:
    """Coerce model output into well-typed, deduped proposal items."""
    cleaned: list[dict] = []
    seen: set[str] = set()
    for it in items:
        product = (it.get("product") or "").strip().lower()
        if not product:
            continue
        if product in seen:
            continue
        seen.add(product)
        flex = it.get("brand_flexibility")
        if flex not in ("any", "prefer", "strict"):
            flex = "prefer" if it.get("preferred_brand") else "any"
        cleaned.append({
            "product": product,
            "unit": (it.get("unit") or "unit").strip(),
            "estimated_qty": _num(it.get("estimated_qty"), 0.0),
            "par_level": _num(it.get("par_level"), 1.0) or 1.0,
            "daily_rate": max(0.0, _num(it.get("daily_rate"), 0.0)),
            "preferred_brand": (it.get("preferred_brand") or "").strip() or None,
            "brand_flexibility": flex,
            "last_ordered": (it.get("last_ordered") or "").strip() or None,
            "times_ordered": int(it.get("times_ordered") or 1),
            "category": (it.get("category") or "other").strip(),
        })
    return cleaned


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── Rendering ─────────────────────────────────────────────────────────────────


def _low_preview(items: list[dict], limit: int = 6) -> list[str]:
    """Names of proposed items that already look depleted, soonest-first.

    Uses the same signal the predictor does (days left = on-hand ÷ daily_rate) so the
    preview matches what the first grocery run would surface. ~3 days mirrors the
    predictor's default lead+buffer low threshold.
    """
    scored: list[tuple[float, str]] = []
    for it in items:
        rate = _num(it.get("daily_rate"), 0.0)
        qty = _num(it.get("estimated_qty"), 0.0)
        days = qty / rate if rate > 0 else float("inf")
        if days <= 3.0:
            brand = (it.get("preferred_brand") or "").strip()
            label = (it.get("product") or "item") + (f" ({brand})" if brand else "")
            scored.append((days, label))
    scored.sort(key=lambda t: t[0])
    return [name for _, name in scored[:limit]]


def render_proposal(items: list[dict]) -> str:
    """Compact Telegram-HTML summary of a freshly synthesized proposal.

    A long order history can yield 90+ items, and dumping them all both overflows
    Telegram's 4096-char limit and reads as a wall of text. Instead we summarize:
    how many we found, the category spread, and a preview of what already looks low.
    The review agent holds the full list, so the user can drill into any category by
    asking, or say "show me everything" for the full (chunked) list.
    """
    if not items:
        return "I couldn't find recurring grocery items in your Amazon orders."

    counts: dict[str, int] = {}
    for it in items:
        cat = (it.get("category") or "other").strip() or "other"
        counts[cat] = counts.get(cat, 0) + 1
    cat_line = " · ".join(
        f"{escape(cat.title())} ({n})"
        for cat, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    lines = [
        f"🧾 <b>I pieced together {len(items)} items from your Amazon orders</b>",
        "",
        cat_line,
    ]

    low = _low_preview(items)
    if low:
        lines += ["", "<b>Looks like you're already low on:</b>"]
        lines += [f"• {escape(name)}" for name in low]

    lines += [
        "",
        "Reply to fix anything — e.g. <i>\"drop the donuts\"</i>, <i>\"remove the "
        "snacks, I'm on a diet\"</i>, <i>\"what cleaning stuff did you find?\"</i>, or "
        "<i>\"show me everything\"</i> for the full list. Say <i>\"looks good\"</i> and "
        "I'll save it and line up a first order.",
    ]
    return "\n".join(lines)


def render_full_proposal(items: list[dict]) -> str:
    """The complete proposal, every item grouped by category (Telegram HTML).

    Sent only when the user asks to see everything — ``send_telegram_message``
    chunks it across multiple sends if it's over the length limit.
    """
    if not items:
        return "I couldn't find recurring grocery items in your Amazon orders."

    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it.get("category") or "other", []).append(it)

    lines = ["🧾 <b>Everything I pieced together from your Amazon orders</b>", ""]
    for category in sorted(groups):
        lines.append(f"<b>{escape(category.title())}</b>")
        for it in groups[category]:
            brand = f" — {escape(it['preferred_brand'])}" if it.get("preferred_brand") else ""
            qty = it.get("estimated_qty", 0)
            unit = (it.get("unit") or "").strip()
            qty_str = f"~{qty:g} {unit}".strip()
            lines.append(f"• {escape(it['product'])}{brand} (have {escape(qty_str)})")
        lines.append("")
    lines.append(
        "Reply to fix anything — e.g. <i>\"drop the donuts\"</i>, "
        "<i>\"remove the unhealthy snacks, I'm on a diet\"</i>, "
        "<i>\"I go through the oat milk faster than that\"</i> — or say "
        "<i>\"looks good\"</i> and I'll save it and check what you need."
    )
    return "\n".join(lines)


# ── Stage 2: review conversation (Haiku) ──────────────────────────────────────

_REVIEW_TOOLS: list[dict] = [
    {
        "name": "remove_items",
        "description": (
            "Remove one or more proposed items. Use for 'drop the donuts', 'I don't "
            "buy that anymore', and for category requests like 'remove the unhealthy "
            "snacks' (list every matching item) or 'I'm vegetarian, drop the meat'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "products": {"type": "array", "items": {"type": "string"},
                             "description": "Exact product names from the proposal to remove."},
            },
            "required": ["products"],
        },
    },
    {
        "name": "update_item",
        "description": (
            "Change fields on a proposed item — quantity on hand, how fast they go "
            "through it (daily_rate), brand, or unit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Exact product name from the proposal."},
                "estimated_qty": {"type": "number"},
                "daily_rate": {"type": "number", "description": "Units per day, if they tell you cadence."},
                "par_level": {"type": "number"},
                "unit": {"type": "string"},
                "preferred_brand": {"type": "string"},
                "brand_flexibility": {"type": "string", "enum": ["any", "prefer", "strict"]},
            },
            "required": ["product"],
        },
    },
    {
        "name": "add_item",
        "description": "Add a pantry item the user mentions that wasn't in the proposal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "unit": {"type": "string"},
                "estimated_qty": {"type": "number"},
                "par_level": {"type": "number"},
                "daily_rate": {"type": "number"},
                "preferred_brand": {"type": "string"},
                "brand_flexibility": {"type": "string", "enum": ["any", "prefer", "strict"]},
                "category": {"type": "string"},
            },
            "required": ["product"],
        },
    },
    {
        "name": "show_full_list",
        "description": (
            "The user wants to see the COMPLETE list of proposed items, not the "
            "summary. Use for 'show me everything', 'list them all', 'what's the full "
            "list', 'show the whole thing'. (To answer about one category — 'what "
            "snacks did you find?' — just reply in text from the list above; only call "
            "this when they want the entire list.)"
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "confirm_import",
        "description": (
            "The user is happy with the (possibly edited) list and wants to save it. "
            "Use for 'looks good', 'yes save it', 'that's right', 'go ahead'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cancel_import",
        "description": "The user wants to throw away the whole import and not save any of it.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _review_system(items: list[dict]) -> str:
    listing = "\n".join(
        f"- {it['product']}"
        + (f" [{it['preferred_brand']}]" if it.get("preferred_brand") else "")
        + f" — have ~{it.get('estimated_qty', 0):g} {it.get('unit', '')}".rstrip()
        + f", uses ~{it.get('daily_rate', 0):g}/day"
        + f" ({it.get('category', 'other')})"
        for it in items
    ) or "(the list is now empty)"
    return (
        "You are Grocery Buddy helping the user review a pantry list we synthesized "
        "from their Amazon order history, BEFORE saving anything. Nothing is in their "
        "pantry yet — your edits change this staged proposal only.\n\n"
        "Current proposed items:\n"
        f"{listing}\n\n"
        "Interpret the user's reply and call the right tool(s):\n"
        "• remove_items — drop items. For a category like 'remove unhealthy snacks' or "
        "'I'm on a diet, cut the junk', YOU decide which listed items qualify (chips, "
        "candy, soda, cookies, donuts, ice cream, etc.) and remove all of them.\n"
        "• update_item — adjust quantity, rate, brand, or unit for an item.\n"
        "• add_item — add something they mention that's missing.\n"
        "• show_full_list — they want to see every proposed item, not the summary.\n"
        "• confirm_import — they're happy; save the list.\n"
        "• cancel_import — they want to discard the whole thing.\n\n"
        "They were shown a SUMMARY (counts + what looks low), not every item — but you "
        "can see the full list above, so answer questions about any category directly. "
        "Only call show_full_list when they ask for the entire list.\n\n"
        "You may call several tools in one turn (e.g. remove three snacks at once). "
        "After editing, briefly tell the user what you changed and what remains, and "
        "ask if it looks good now. Match product names to the list above exactly. Be "
        "warm and concise. Telegram HTML only (<b>, <i>); no markdown."
    )


async def advance_import_review(
    pool, user_id: str, proposal_id: str, items: list[dict], messages: list[dict]
) -> tuple[str, list[dict], list[dict], str]:
    """Run one review turn to completion.

    ``messages`` already includes the latest user message. Returns
    ``(assistant_text, updated_messages, updated_items, outcome)`` where outcome is
    one of 'continue' | 'confirm' | 'cancel'. Edits are persisted to the staged
    proposal as they happen; the caller writes to the live pantry only on 'confirm'.
    """
    while True:
        resp = await llm.create_message(
            model=settings.model_fast,
            label="advance_import_review",
            user_id=user_id,
            max_tokens=1500,
            system=_review_system(items),
            tools=_REVIEW_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": _serialize_blocks(resp.content)})

        if resp.stop_reason != "tool_use":
            text = "\n".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            return text or "Want me to save this list?", messages, items, "continue"

        # Collect edits across all tool_use blocks this turn, then apply once.
        removes: list[str] = []
        updates: list[dict] = []
        adds: list[dict] = []
        outcome = "continue"
        show_full = False
        tool_results: list[dict] = []

        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            args = block.input or {}
            note = "ok"
            if block.name == "remove_items":
                removes.extend(p for p in args.get("products", []) if p)
                note = f"removing {', '.join(args.get('products', [])) or 'nothing'}"
            elif block.name == "update_item":
                updates.append(args)
                note = f"updated {args.get('product', '')}"
            elif block.name == "add_item":
                adds.append(args)
                note = f"added {args.get('product', '')}"
            elif block.name == "show_full_list":
                show_full = True
                note = "showing the full list"
            elif block.name == "confirm_import":
                outcome = "confirm"
                note = "confirmed — saving"
            elif block.name == "cancel_import":
                outcome = "cancel"
                note = "cancelled"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": note})

        if removes or updates or adds:
            items = apply_edits(items, remove=removes, update=updates, add=adds)
            await update_proposal_items(pool, proposal_id, items)

        if outcome in ("confirm", "cancel"):
            text = "\n".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            return text, messages, items, outcome

        # Close out the tool calls in the transcript, then dump the full list as a
        # verbatim reply (paraphrasing it through the model would risk dropping
        # items). send_telegram_message chunks it if it's over the length limit.
        messages.append({"role": "user", "content": tool_results})
        if show_full:
            full = render_full_proposal(items)
            messages.append({"role": "assistant", "content": [{"type": "text", "text": full}]})
            return full, messages, items, "continue"


def _serialize_blocks(content_blocks: list) -> list[dict]:
    out: list[dict] = []
    for b in content_blocks:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": b.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out
