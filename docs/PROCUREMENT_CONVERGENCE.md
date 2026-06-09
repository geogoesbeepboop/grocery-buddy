# Grocery-Buddy ⇄ Procurement-Agent: scope, convergence, and the merge decision

> Decision doc. Status: **decided 2026-06-05** (see §3, §7). Companion to
> [SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md), [FEATURES_AND_ROADMAP.md](FEATURES_AND_ROADMAP.md),
> and the cross-agent plan in `/dev/multi-agent-docs`.

## TL;DR

The original framing was that **procurement-agent is a wider-scope superset of
grocery-buddy**. Given how each is actually built today, that's no longer the right
picture. They've grown into **two complementary halves**, not a superset and a subset:

- **grocery-buddy** is a *real, working* commerce **actuator + domain + UX**: live
  Amazon automation, Telegram conversation, Supabase persistence, rule-based
  prediction/depletion, onboarding, order-history import, an approval gate.
- **procurement-agent** is a *deterministic* commerce **money-authority spine**:
  risk-tiered autonomy, HMAC-signed purchase mandates, dual card rails
  (Privacy.com issuer-lock / Lithic ASA real-time auth), a merchant-agnostic
  checkout interface (ACP / Stagehand / Skyvern), an append-only audit log, and a
  Temporal purchase saga — but every external integration is a **credential-blocked
  stub** and storage is **in-memory**.

**Decision (George, 2026-06-05): keep the two agents fully separate** — independent
repos, independent lifecycles, *no shared code module*. grocery-buddy gains its own
auto-buy capability by **reimplementing** the money-authority pieces it needs
(policy gate, signed mandate, card rail) **locally**, using procurement-agent's
proven design as a reference rather than a dependency. So the relationship is
**parallel evolution with shared design DNA**, not one shared spine. (I'd recommended
layering on a shared `agent-core`; George chose independence — fewer cross-repo
coupling risks, and it fits the multi-agent "disposable box" philosophy and the
portfolio goal of several distinct, self-contained commerce agents.)

The analysis below (§1–§6, why they're complementary) still stands and is the
*design playbook* for what grocery-buddy reimplements. The first concrete step is
**already implemented**: the in-transit replenishment loop added in this change
(confirm → on-the-way → restock) is the consumer-grocery analog of
procurement-agent's open-mandate → settled-purchase → consumption-history lifecycle
— built natively in grocery-buddy, informed by procurement-agent's model.

---

## 1. What each agent actually is today

| Capability | grocery-buddy | procurement-agent |
|---|---|---|
| **Real actuation** | ✅ Amazon (Playwright): search, price, stage cart, checkout link | ⛔ ACP / Stagehand / Skyvern backends are interface-fixed **stubs** (`NotImplementedError`) |
| **Persistence** | ✅ Supabase Postgres (18 tables, migrations) | ⛔ in-memory repos only (Postgres schema *drafted*) |
| **Conversational UX** | ✅ Telegram (intent parsing, approval, onboarding, 2FA relay) | ⛔ none (MCP tools + Slack-signal stub) |
| **Prediction / replenishment** | ✅ rule-based predictor + estimated depletion + par levels | ✅ pure consumption-rate model (`sourcing/consumption.py`) |
| **Approval gate** | ✅ always-on, human approves every cart | ✅ HUMAN tier parks on a durable Temporal signal |
| **Risk-tiered autonomy** | ⛔ one mode: stage + hand a checkout link | ✅ AUTONOMOUS / GUARDED / HUMAN from a deterministic policy gate |
| **Signed purchase mandate** | ⛔ — | ✅ HMAC-signed, TTL'd, envelope-checked on the auth hot path |
| **Card / money rails** | ⛔ user pays on Amazon themselves | ✅ Privacy.com issuer-lock (autonomous) + Lithic ASA (guarded) — *stubbed on creds* |
| **Price-anomaly defense** | ⛔ (has `price_snapshots`, unused for this) | ✅ learned price band; planted-price listings get declined by code |
| **Audit trail** | partial (carts/purchases/events) | ✅ append-only, immutable, per-mandate |
| **Orchestration** | ✅ Temporal (GroceryRun / QuickBuy / Import) | ✅ Temporal (purchase saga + human signal) |
| **Maturity** | **runs for real, single user, live** | **deterministic core proven in-memory; integrations pending creds** |

**Read:** grocery-buddy has everything procurement-agent needs to *touch the real
world* (a merchant, a database, a user). procurement-agent has everything
grocery-buddy needs to *safely spend money on its own* instead of always punting to
a human checkout tap.

## 2. Why the "superset" framing broke

procurement-agent was conceived as "grocery, but for anything you buy." In
practice the team built the **hard, differentiated part first** — the money-control
safety spine (propose/dispose, signed mandates, issuer-enforced caps, real-time
auth gating, the OWASP/Rule-of-Two threat model). It deliberately stubbed
actuation, persistence, and UX because those are *solved* in grocery-buddy and
because the credentials (Privacy/Lithic/ACP) gate go-live anyway.

So procurement-agent isn't a bigger grocery-buddy. It's the **trust-and-authority
layer** a grown-up grocery-buddy would sit on top of. The scope didn't widen; it
went *deeper* on the part that's genuinely hard and resume-defining.

## 3. The decision

### Keep the two agents fully separate — parallel evolution, shared design DNA.

**Decided by George, 2026-06-05.** No shared code module; grocery-buddy reimplements
the money-authority pieces it needs (policy gate, signed mandate, card rail)
*natively*, using procurement-agent's design as the reference. The complementarity in
§1–§2 is real, but it's leveraged as a **playbook to copy from**, not a dependency to
import.

**Why separate (the chosen path):**

1. **Independence over DRY.** Two self-contained commerce agents are a stronger
   portfolio than one coupled system, and each can move/deploy/fail on its own. Fits
   the multi-agent "disposable box" philosophy and avoids a cross-repo blast radius.
2. **No maturity drag.** grocery-buddy is live and real; procurement-agent is
   in-memory stubs. A shared library would couple grocery's release cadence to
   procurement's churn. Copying a *proven design* costs a few hundred lines; coupling
   costs forever.
3. **The design, not the code, is the asset.** procurement-agent already did the hard
   thinking (propose/dispose, mandate envelope, issuer-enforced caps, OWASP/Rule-of-Two).
   grocery-buddy reimplementing that locally still gets the full safety story — and a
   second, independent implementation is its own form of validation.

**The tradeoff we're accepting:** the replenishment math (and Temporal/approval
plumbing) stays duplicated across the two repos by choice. That's the cost of
independence; it's small and bounded.

### The shape

```
   grocery-buddy (self-contained)            procurement-agent (self-contained)
   ───────────────────────────────          ──────────────────────────────────
   • Amazon actuation                        • ACP / Stagehand / Skyvern
   • Supabase persistence                    • Privacy / Lithic card rails
   • Telegram UX + onboarding                • broad-catalog sourcing
   • pantry prediction + in-transit          • merchant directory
   • policy gate + signed mandate ◄┄┄┄┄┄┄┄┄┄ • policy gate + signed mandate
   • spend-capped card rail        (design   • dual card-rail design
   • price-band, budget, audit      reference) • price-band, budget, audit
                                    ┄┄┄┄┄┄┄►
            design DNA flows one way (proven there → reimplemented here);
            no shared package, no import, no cross-repo deploy coupling.
```

End state: **two independent commerce agents** that happen to share a safety design
language. grocery-buddy becomes a full autonomous-buy agent in its own right;
procurement-agent remains the general-purpose "buy anything" agent. Whatever's proven
in one is free design inspiration for the other.

## 4. What each side teaches the other (concrete)

> Per the keep-separate decision (§3), "adopt" below means **reimplement locally using
> the other's design as a reference** — not import a shared module. The
> procurement-agent → grocery-buddy direction is the active playbook (grocery is
> building auto-buy); the reverse is kept as a design note for procurement-agent's own
> future actuation/persistence/UX work.

### procurement-agent → grocery-buddy (reimplement these)

| Bring over | What it unlocks in grocery-buddy | Effort |
|---|---|---|
| **Risk-tiered autonomy + policy gate** | Auto-buy cheap known staples under a cap; human-approve only novel/expensive items, instead of approving *everything* | M |
| **Signed mandate + card rails** | *Actually place the order* for the safe tier (close the "we never buy" gap) — see [FEATURES_AND_ROADMAP.md](FEATURES_AND_ROADMAP.md) | L (needs creds) |
| **Price-band anomaly model** | Don't reorder eggs at 3× normal; flag price spikes. grocery already stores `price_snapshots` — the data is sitting there unused | S |
| **Budget envelopes** | `preferences.monthly_budget_usd` exists but is unused; procurement has atomic reserve/release | S |
| **Append-only audit** | A clean compliance/debug trail for every spend decision | S |

### grocery-buddy → procurement-agent (adopt these)

| Bring over | What it unlocks in procurement-agent | Effort |
|---|---|---|
| **Real actuation (Amazon/Playwright)** | A working backend behind the `CommerceBackend` interface today, not just stubs | M |
| **Supabase persistence** | Swap the in-memory repos for the Postgres the data-model doc already sketches | M |
| **Conversational UX (Telegram + 2FA relay)** | A human-approval channel that exists, vs the Slack stub | M |
| **Estimated-depletion + par levels** | A richer consumption model than pure purchase-cadence inference | S |
| **In-transit lifecycle (this change)** | "ordered but not settled/arrived" as first-class state — see §6 | — (mirrored) |

## 5. The replenishment-math convergence (the smoking gun)

Both agents, built separately, arrived at the same idea: *don't reorder based on
on-hand alone — project consumption over a lead time and act before you run out.*

- grocery-buddy: `predictor.is_low()` → `days_left ≤ lead_time + buffer`.
- procurement-agent: `consumption.should_reorder()` → `on_hand − rate × lead_time ≤ threshold`.

Same shape, two codebases. When two independent designs converge on one algorithm,
it's strong evidence the design is right. With the keep-separate decision (§3) the two
implementations stay independent on purpose — but the convergence is exactly why
grocery-buddy can confidently reimplement procurement-agent's authority model: it's
already been validated twice.

## 6. What shipped now (the first convergence step)

This change adds an **in-transit replenishment lifecycle** to grocery-buddy:

```
confirm order → in-transit (eta = ordered_at + lead_time) → reconcile on arrival → on-hand
```

This is deliberately the **consumer-grocery mirror of procurement-agent's purchase
lifecycle**:

| procurement-agent | grocery-buddy (now) |
|---|---|
| open mandate (authority granted, not yet settled) | in-transit replenishment (ordered, not yet arrived) |
| settled `Purchase` feeds consumption history | reconciled arrival logs a `purchase` consumption event |
| consumption model reads settled purchases | predictor reads in-transit as covered stock |

It gives grocery-buddy the missing memory that *a confirmed order is not a need*,
and it does so with the same "a purchase is a time-aware state, not a fire-and-forget
action" philosophy procurement-agent is built on. See
[SYSTEM_REFERENCE.md](SYSTEM_REFERENCE.md) §4.1 and `replenishment.py`.

## 7. Decisions (resolved 2026-06-05)

All three forward calls are now made (via the post-build review with George):

1. **Autonomy go-live → YES, pursue real auto-buy.** grocery-buddy will gain a
   risk-tiered auto-buy path: cheap, known staples under a cap get placed
   automatically under a (locally-built) signed mandate + a spend-capped card rail;
   novel/expensive items still hit the human gate. Needs a real card account
   (Privacy.com merchant-locked or Lithic ASA) + a spend-cap/category risk decision +
   an Amazon-ToS call before money goes live. Build behind a flag, money-live last.
2. **Repo strategy → KEEP SEPARATE** (§3). Reimplement the authority layer in
   grocery-buddy; no shared `agent-core` dependency, no repo merge.
3. **Real delivery tracking → YES, via Gmail.** Replace estimated ETAs + the
   "did you order it?" tap with real order data: parse Amazon order-confirmation
   emails (read-only Gmail) to auto-confirm purchases and land arrivals on the true
   delivery date. Needs Gmail OAuth setup.

**Sequenced next build** (details + the no-new-service quick wins in
[FEATURES_AND_ROADMAP.md](FEATURES_AND_ROADMAP.md)):
queue the greenlit cheap wins (budget envelopes → learn-rate-from-purchases →
perishable-lite) while standing up the auto-buy gate in safe-mode, then wire Gmail
delivery tracking, then a real card rail behind a flag, money-live last.
