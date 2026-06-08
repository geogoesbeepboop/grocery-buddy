# Grocery-Buddy: features & roadmap

What's shipped, what's cheap to add next, and what needs a decision or a new
service/credential from you. Companion to [SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md)
and [PROCUREMENT_CONVERGENCE.md](PROCUREMENT_CONVERGENCE.md).

Legend — effort: **S** ≤ half a day · **M** a day or two · **L** multi-day / needs creds.

> **Decisions (George, 2026-06-05):** ✅ pursue **real auto-buy** (locally-built
> mandate + a real card rail — §1 below); ✅ **Gmail delivery tracking** (§2); keep
> grocery-buddy and procurement-agent **fully separate** (reimplement, don't share —
> see [PROCUREMENT_CONVERGENCE.md](PROCUREMENT_CONVERGENCE.md)); next no-new-service
> wins queued = **budget envelopes → learn-rate-from-purchases → perishable-lite**
> (price-anomaly guard deferred). Sequencing at the bottom.

---

## ✅ Shipped in this change — the in-transit replenishment loop

The agent's pantry now spans **on-hand + on-the-way**, closing the loop after a
checkout link:

- **Confirm = "I placed the order."** A button on the checkout-link message
  (`✅ I placed the order`) or a plain reply (`"ordered"`, `"done"`). On confirm the
  cart's items become **in-transit replenishments** with an ETA (`ordered_at +
  lead_time_days`).
- **No double-ordering.** Prediction and `/status` count in-transit qty as covered
  stock, so a confirmed order is never re-suggested while it's on the way — *"don't
  buy eggs tomorrow if I accepted eggs today."*
- **Auto-restock on arrival.** When the ETA passes, a reconcile step (top of every
  run, plus a durable per-order delivery timer) lands the order: it adds the qty to
  the pantry, logs a `purchase` consumption event, and nudges *"order landed — pantry
  topped up."* Idempotent — each order lands exactly once.
- **"It never came."** `"the milk never came"` / `"cancel that order"` removes
  in-transit items so they count as needed again.
- **`/status` → 🚚 On the way.** A new section lists ordered-but-not-arrived items
  with ETAs.

See `replenishment.py`, migration `009_pending_replenishments.sql`, and the durable
saga in `workflows/grocery_run.py::_await_purchase_confirmation`.

---

## 🟢 Ready to build now — no new services, mostly existing data

These need only a decision (and a little code). Several are direct adoptions from
procurement-agent's spine — see [PROCUREMENT_CONVERGENCE.md](PROCUREMENT_CONVERGENCE.md) §4.

| Feature | What it does | Why it's cheap | Effort | Status |
|---|---|---|---|---|
| **Budget envelopes** | Wire up `preferences.monthly_budget_usd` (exists, unused): track month-to-date spend, warn before a cart blows the budget, show "remaining this month" in `/status`. | Column + carts/purchases already there; procurement has atomic reserve/release to borrow. | S | ✅ **queued (1st)** |
| **Learn the consumption rate from real purchases** | Use confirmed-order cadence (now captured as in-transit → arrived) to refine each product's rate, not just declared + manual updates. | The `purchase` consumption events the new arrival step writes are exactly the signal. | S–M | ✅ **queued (2nd)** |
| **Perishable / shelf-life lite** | Tag perishables (milk, produce, eggs) with a shelf life; cap their effective "days left" so we don't over-order long-life staples or assume a 2-week-old gallon is still good. | Small per-product field + a clamp in the predictor. | S | ✅ **queued (3rd)** |
| **Price-anomaly / deal guard** | Learn a normal price band per product from the `price_snapshots` we already capture; flag "milk is 2.6× its usual price" in the briefing, and (optional) hold a non-perishable reorder when the price spikes. | Data is already stored; procurement-agent's `learn_price_band` is a drop-in algorithm. | S–M | deferred |
| **"Snooze" / pause an item** | "stop suggesting paper towels for a month" — a per-item mute with an expiry. | One table + a predictor filter. | S | idea |
| **Richer `/status` & history** | "what did I buy this month", spend trend, most-reordered — straight from `carts`/`purchases`. | Pure reads. | S | idea |
| **Substitution memory** | When a preferred item is out of stock, remember the substitute the user accepted and prefer it next time. | Extends the existing brand-flexibility logic. | M | idea |

---

## 🟡 Needs your input or a new service/credential

The big bets. Each lists **what I'd need from you** so we can greenlight or shelve it.

### 1. True autonomous checkout — *actually buy the safe tier* (the headline) — ✅ GREENLIT
- **What:** Stop always handing a checkout link. For cheap, known, recurring
  staples under a cap, the agent places the order itself under a signed
  purchase-mandate + a spend-capped card; novel/expensive items still get the human
  gate. This is the grocery-buddy ⇄ procurement-agent convergence made real.
- **Why it matters:** It's the difference between "assistant that drafts a cart" and
  "agent that runs your pantry." It's also the most resume-defining piece (Visa/
  Stripe-grade money control, OWASP/Rule-of-Two safety story).
- **What I'd need from you:**
  - A card rail account: **Privacy.com** (issuer-locked single-use cards, simplest)
    and/or **Lithic ASA** (real-time auth your code approves). [procurement-agent
    research](../../procurement-agent/docs/05-money-control.md) has the picks.
  - A **risk decision**: max auto-buy cap, which categories are eligible.
  - An **Amazon-ToS call** (automated checkout vs. ACP/merchant-sanctioned rails).
- **Effort:** L (and gated on the above).

### 2. Real delivery tracking — *replace estimated ETAs with actual ones* — ✅ GREENLIT (Gmail)
- **What:** Instead of estimating arrival as `ordered_at + lead_time_days` and asking
  "did you order it?", read the real order: parse the Amazon order-confirmation email
  (or scrape "Your Orders") to auto-confirm the purchase, capture the **exact items +
  real delivery date**, and land arrivals on the true date.
- **Why it matters:** Removes the one bit of friction left (the confirm tap) and makes
  in-transit precise instead of estimated.
- **What I'd need from you:** **Gmail API access** (or an email-forwarding rule to a
  parseable inbox), or acceptance of deeper Amazon "Your Orders" scraping.
- **Effort:** M–L.

### 3. Multi-retailer + price comparison actuation
- **What:** Go beyond Amazon — Kroger / Instacart / Walmart — and pick the cheapest
  source per item. The Kroger price API is *already wired for comparison*
  (`lookup_kroger_prices`); this turns comparison into actuation behind one interface.
- **Why it matters:** Real savings + resilience when one retailer fails. Maps cleanly
  onto procurement-agent's `CommerceBackend` (ACP / Stagehand / Skyvern) abstraction.
- **What I'd need from you:** Accounts/credentials per retailer (Kroger dev key,
  Instacart, etc.) and which retailers you actually use.
- **Effort:** L.

### 4. Recipe / meal-plan-driven shopping
- **What:** "Plan 5 dinners this week" → generate the ingredient list → diff against
  the pantry → add only what's missing to the run.
- **Why it matters:** Shifts from "restock what's low" to "shop for what you'll cook"
  — a much stickier use case.
- **What I'd need from you:** Scope (just your recipes? a recipe API like Spoonacular?
  dietary constraints?). The dietary-notes field already exists in `preferences`.
- **Effort:** M–L.

### 5. Calendar / household awareness
- **What:** "Family visiting this weekend → buy extra"; skip a run while you're
  travelling; scale quantities to events.
- **Why it matters:** Consumption isn't constant; context makes predictions far better.
- **What I'd need from you:** Calendar access (a Calendar MCP is available in this
  environment) and which signals you want it to act on.
- **Effort:** M.

### 6. Multi-user / shared household
- **What:** Today the schema and Telegram routing are strictly single-user
  (`GROCERY_BUDDY_USER_ID`). Support a household: multiple chats, shared pantry,
  per-person Amazon profiles (the `amazon_profiles` table already anticipates this).
- **Why it matters:** Real households share a pantry; it's also the step toward a
  product, not a personal tool.
- **What I'd need from you:** Whether this is a goal now (it's a meaningful refactor)
  vs. staying single-user.
- **Effort:** L.

### 7. Agent-to-agent commerce (ACP / x402)
- **What:** Let grocery-buddy transact over agentic-commerce rails — ACP
  (Stripe/OpenAI/Meta) or x402/USDC (your `jim-agent`) — instead of a human-mediated
  checkout. The endgame of the whole portfolio.
- **Why it matters:** This is the frontier the career goal points at (Coinbase/Visa/
  Bloomberg). It's where grocery, procurement, and jim converge.
- **What I'd need from you:** A platform-level decision; depends on items 1 & 3 and
  the broader multi-agent build.
- **Effort:** L+ (research-stage).

---

## Sequencing (decided 2026-06-05)

1. **Quick wins, no deps (queued):** budget envelopes → learn-rate-from-purchases →
   perishable-lite. All small, all reuse data we already have. (price-anomaly guard
   deferred.)
2. **The auto-buy gate, safe-mode first:** build the risk-tiered policy gate + signed
   mandate locally (reimplementing procurement-agent's design — *not* a shared dep),
   exercised with a dry-run/fake card. No accounts needed for this stage.
3. **Gmail delivery tracking:** wire read-only Gmail to auto-confirm orders + land
   arrivals on the real delivery date (removes the confirm tap; makes in-transit exact).
   *Needs: Gmail OAuth.*
4. **Real card rail, money-live last:** put a spend-capped card (Privacy.com / Lithic)
   behind the gate, behind a flag. *Needs: a card account + spend-cap/category risk
   decision + an Amazon-ToS call.*

Stages 2–4 are the "real auto-buy" build; they turn grocery-buddy from "drafts a cart"
into "runs your pantry." See [PROCUREMENT_CONVERGENCE.md](PROCUREMENT_CONVERGENCE.md) §3, §7.
