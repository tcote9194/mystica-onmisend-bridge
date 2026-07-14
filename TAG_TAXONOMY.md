# Tag & Property Taxonomy — the cross-service naming agreement

**Status: proposed spec, 2026-07-13.** This is the single source of naming truth shared by **Render (the bridge that writes tags)**, **OmniSend (where tags live + segments read them)**, and the **Mystica Lifecycle Engine (which consumes them to route sends)**. Nothing here changes engine behavior; it defines *what gets tagged and what it's called* so the three systems agree.

The guiding rule: **one source of truth per field.** Render writes app/engagement facts. The funnel writes lead facts. CheckoutChamp writes order **and subscription** facts. The **engine** derives lifecycle state — and no one else writes engine-owned fields.

---

## Why this matters now

Render turns out to hold a far richer dataset than OmniSend sees — **~35,000 active signed-up app users vs. the ~600 currently tagged `app_install` in OmniSend.** The engine's adopter test is already `any app:* tag OR posthog_distinct_id`, so the instant Render backfills `app:*` tags for those 35k, the **App Adopter segment auto-expands from 5,279 → ~35k with zero segment edits.** Same mechanism fills the currently-missing **Subscriber** stage. Get the names right once, and the segmentation corrects itself.

---

## Naming conventions

| Kind | Convention | Examples |
|---|---|---|
| **State / membership / activity** → **tags** | `namespace: value` — lowercase, `snake_case` value, colon-space separator | `app: signup`, `app: daily_draw`, `subscription: active`, `purchase: soulmate_reading` |
| **Counts, dates, ids** → **custom properties** | `snake_case`, prefixed by domain (`app_`, `subscription_`, `render_`) | `app_chats`, `app_last_active`, `subscription_started`, `render_user_id` |

> Tags answer "is this true?" (segmentable as anyOf/noneOf). Properties answer "how many / when / which" (segmentable as moreThan / inTheLast / equals). Use a **count property**, not a tag, whenever a threshold matters (e.g. "≥3 chats").

---

## The taxonomy

### A. Identity — the join keys (properties)
| Field | Type | Writer | Status | Purpose |
|---|---|---|---|---|
| `render_user_id` | prop | **Render** | **ADD** | Anchor that ties an OmniSend contact ↔ Render user ↔ PostHog. The backbone of the whole bridge. |
| `posthog_distinct_id` | prop | Render/PostHog | ✅ exists | Already counts as an adopter signal in the engine. Keep. |

### B. App membership — "has an account / installed" (tags)
| Tag | Writer | Status | Notes |
|---|---|---|---|
| `app: signup` | **Bridge** (Render) | ✅ emitted | On `created_at` (every account). Bridge owns it. |
| `app: installed` | **Bridge** (Render) | ✅ emitted | On `created_at` (every account). Bridge owns it. |
| `app: login` | **Bridge** (Render) | ✅ emitted | Recently active: `last_seen` within `RENDER_LOGIN_WITHIN_DAYS` (default 30d, ~2,196). Add-only, so it marks "was active in 30d at some point" — for a live rolling segment, filter the `app_last_active` property. |
| `app: updated` | n8n only | ⚠️ **not reproducible** | No app-version/update field in Render — the bridge can't derive this. If a segment depends on it, keep its source or drop the tag before fully retiring n8n. |

### C. App activity — "has ever done" (tags)
| Tag | Writer | Status | Notes |
|---|---|---|---|
| `app: chat` | **Render** | ✅ exists | Keep. |
| `app: daily_draw` | **Render** | **ADD** | Daily draw is one of the strongest free-engagement signals (2+ daily-draws indicator). |
| `app: reading` | Render | **ADD** (if distinct) | In-app reading completed. |
| `app: purchase` | Render | ✅ exists | In-app purchase. Keep. |

### D. Engagement depth — the leading indicators (count properties)
| Field | Type | Writer | Status | Purpose |
|---|---|---|---|---|
| `app_chats` | number | **Render** | **ADD** | Powers the "≥3 chats" indicator as a segment filter. |
| `app_daily_draws` | number | **Render** | **ADD** | "≥2 daily draws" indicator. |
| `app_interactions` | number | **Render** | **ADD** | "≥11 interactions" indicator (total meaningful actions). |

> **Update (2026-07-13): counts are ALSO emitted as milestone TAGS.** OmniSend segments can't reliably threshold a numeric custom property, so each count is encoded as a cumulative tag ladder — `app: chat_1/_2/_3`, `app: daily_draw_1/_2`, `app: interaction_11` (a user with ≥N gets the `_N` tag, capped at the indicator). Segment on the top rung (e.g. `app: chat_3` = the ≥3-chats indicator). The raw count props above are kept as reference data. Still do **not** add engagement *tier* tags — the engine computes `engagement_tier` from these raw signals.

### E. Recency — drives adopter → lapsed (date properties)
| Field | Type | Writer | Status | Purpose |
|---|---|---|---|---|
| `app_last_active` | date | **Render** | **ADD** | Canonical last-activity timestamp. This is what separates a live **adopter** from a **lapsed** one, and covers the "jumped in once but didn't continue" gap you raised. |
| `posthog_last_seen` | date | Render/PostHog | ✅ exists | The engine reads this today for engagement recency. Bridge should keep it set (or mirror `app_last_active` into it) so the engine keeps working during migration. |

### F. Subscription — fills the missing Subscriber stage (tag + properties)
**Source: CheckoutChamp** (not Render) — subscriptions live in the billing platform, matching the engine's existing rebill-order logic. Tom pulls these from CheckoutChamp.
| Field | Type | Writer | Status | Purpose |
|---|---|---|---|---|
| `subscription: active` | tag | **CheckoutChamp** | **ADD** | The recurring-member signal. The engine currently infers subscribers from rebill orders; this exposes it to OmniSend **segments** so the Subscriber stage becomes buildable. |
| `subscription: cancelled` | tag | CheckoutChamp | **ADD** | Churned — different lifecycle track. |
| `subscription: past_due` / `subscription: trialing` | tag | CheckoutChamp | **ADD** (if applicable) | Dunning / trial handling. |
| `subscription_status` | prop | CheckoutChamp | **ADD** | Raw status string for detail. |
| `subscription_started` | date | CheckoutChamp | **ADD** | Tenure / anniversary logic. |

### G. Purchase — already good (tags + order properties)
| Field | Writer | Status | Notes |
|---|---|---|---|
| `purchase: soulmate_reading` / `soul_tie` / `love-blockage` / `ex-lover ritual` | CheckoutChamp | ✅ exists | Keep. A purchase tag = a purchaser (confirmed). |
| `orderDate`, `orderID`, `orderType`, `value`, `billingCycleNumber` | CheckoutChamp | ✅ exists | Keep. `orderDate ≤7d` = **new buyer**; `orderType=rebill` = the subscriber source. |
| `purchase_count` | prop | CheckoutChamp/bridge | optional | Repeat-buyer signal, if easy. |

### H. Lead / funnel — already good, funnel-owned (properties + tag)
| Field | Writer | Status | Notes |
|---|---|---|---|
| `fqPath`, `situationStatus`, `scenarioType`, `personalSituation`, `julesHook`, `teaserPromise1-3`, … | Free-question funnel | ✅ exists | Keep. Powers the 4 lead buckets + Tier-1 personalization. Render does **not** write these. |
| `source: free question` | funnel | ✅ exists | Keep. |

### I. Engine-OWNED — Render/bridge must **NEVER** write these
| Field | Owner | Why off-limits |
|---|---|---|
| `engine_substage` | **Engine** | The engine derives the single lifecycle stage from all the above. If the bridge writes it too, the two systems fight. |
| `track: *` | **Engine** | Send-track assignment. |
| `tier: *` | **Engine** | Heat/frequency tier. |
| `engagement_tier` (as a tag) | **Engine** | Derived from the §D counts + §E recency. Bridge supplies the raw numbers only. |

---

## How the engine consumes this (so we tag the right things)

| Engine concept | Derived from | Taxonomy fields |
|---|---|---|
| `app_adopted` | any app tag OR posthog id | §B/§C tags, `posthog_distinct_id` |
| `subscriber` | rebill order in active window | `orderType`, or `subscription: active` (§F) |
| `new_buyer` | Soulmate purchase ≤7d | `orderDate` (§G) |
| `lapsed` | adopter with stale recency | `app_last_active` / `posthog_last_seen` (§E) |
| `engagement_tier` | app activity + recency | §D counts + §E recency |
| lead bucket | funnel situation | `fqPath` + `situationStatus` (§H) |

---

## Sequencing

1. **Agree this spec** (Render team + engine) — lock names.
2. **Render backfills + goes live** — `app:*`, activity, counts, recency, subscription for all ~35k.
3. **Segments auto-correct** — App Adopter → ~35k; Subscriber stage becomes buildable; Non-adopter shrinks to the real "bought, never engaged" set.
4. **Then** finalize the pilot cohort sample — against accurate populations, so adopters/subscribers aren't under-represented. (Sampling before the backfill would draw from the 5,279 under-count and skew the pilot.)

> This is why the pilot cohort tagging is best done **after** the Render backfill, not before.

---

## Wired-ready segment registry (live in OmniSend, 2026-07-13/14)

All 11 `Pilot —` segments exist now. The ones marked **wired-ready** reference tags/props that don't flow yet — they sit near zero and **auto-fill the instant the writer starts emitting** (confirmed: OmniSend accepts segments referencing not-yet-existing custom properties).

Definitions updated 2026-07-14: closure-release routing + PARTIAL-order fix (buyer segments now key on the purchase TAG, applied only on a completed sale, not the raw `orderID` written at checkout-start).

| # | Segment | Segment ID | Now | Fills to | Depends on | Writer |
|---|---|---|---|---|---|---|
| 1 | Lead · Ex Connected | `6a555c14b5be627293adab1a` | 4,949 | — | `fqPath=ex` + `situationStatus∈{yes_regularly,occasionally,complicated}` + `scenarioType≠closure_release` | funnel ✅ |
| 2 | Lead · Ex Cold | `6a555c150214b2a445afd0e7` | 2,586 | — | `fqPath=ex` + `situationStatus=no_contact` + `scenarioType≠closure_release` | funnel ✅ |
| 3 | Lead · Soulmate Seeker | `6a555c160214b2a445afd0e8` | ~640 | — | `fqPath=soulmate` + `scenarioType≠closure_release` | funnel ✅ |
| 4 | General (everybody else) | `6a555c18b5be627293adab1b` | 47,811 | — | subscribed AND not in #1–3 (incl. `scenarioType=closure_release` catch) | funnel ✅ |
| 5 | New Buyer (soulmate ≤7d) | `6a555daa0214b2a445afd0e9` | 349 | — | tag `purchase: soulmate_reading` AND `orderDate`≤7d AND not `subscription: active` | CheckoutChamp ✅ |
| 6 | Buyer · Non-adopter | `6a555da8b5be627293adab1d` | 5,398 | shrinks | purchase tags[4] AND no `app:*`[8] AND no `posthog_distinct_id` | tags ✅ |
| 7 | App Adopter | `6a555c6bb5be627293adab1c` | 5,279 | **~35k** | any `app:*` tag OR `posthog_distinct_id` | Render (backfill) |
| 8 | Adopter · Active (≤7d) | `6a558ecb42da296472214a77` | 500 | more | #7 AND (`app_last_active` OR `posthog_last_seen`) inTheLast 7d | Render (`app_last_active`) |
| 9 | Subscriber | `6a558e61a39d78fb6fa9e3ed` | 1 | **real** | `subscription: active` | **CheckoutChamp** ⏳ |
| 10 | App · Activated (engaged) | `6a558e6342da296472214a76` | 0 | **real** | `app_chats>2` OR `app_daily_draws>1` OR `app_interactions>10` | Render ⏳ |

**PARTIAL-order fix impact:** #5 dropped 2,188→349 (real ~50/day new soulmate buyers) and #6 dropped 14,234→5,398 — ~8,836 abandoned/incomplete checkouts (an `orderID` written at checkout-start, never completed) were being mis-counted as buyers and are now correctly routed back to their lead/general segments.

**safetyHold suppression (approved, not yet wired):** null-safe approach = whatever sets `safetyHold=true` also writes a `suppress: hold` tag; every pilot segment excludes `noneOf [suppress: hold]` (absent tag = not excluded). Retroactive by construction. Needs a tag-writer before go-live.

**Documented, not yet created** (fragile null-handling on a NOT-recency filter — instantiate once `app_last_active` is live):
- **Adopter · Winback (app-inactive ≥7d)** = #7 AND `app_last_active` exists AND `app_last_active` **notInTheLast** 7d. Matches the engine's `WINBACK_APP_INACTIVE_DAYS = 7`. This is the "jumped in once, didn't continue" group.

**Engine staging map (Pilot cells → OmniSend segment ID → copy plan):** Ex Connected `…adab1a` → NEW_LEAD/ex_connected copy · Ex Cold `…afd0e7` → NEW_LEAD/ex_cold · Soulmate `…afd0e8` → NEW_LEAD/soulmate_seeker · General `…adab1b` → NEW_LEAD/general · New Buyer `…afd0e9` → T2_NEW_BUYER · Non-adopter `…adab1d` → T2_NONADOPTER · App Adopter `…adab1c` → T2_ADOPTER · Subscriber `…e3ed` → T2_SUBSCRIBER. The engine stages one campaign per cell against its segment ID; OmniSend owns who is in each.

---

## Heat / engagement tiers — the frequency throttle (2026-07-14)

**Two layers:** the **segment (stage) decides WHAT** a contact gets; the **tier (heat) decides HOW MANY** they get that day. **Email-only — does NOT affect OneSignal** (push has its own app-active gate). Signal = OmniSend-native `opened` / `clicked message` events (Email channel) — the reliable engagement signal that replaces the engine's broken `updatedAt` proxy. Clicks weighted over opens (opens are inflated by Apple Mail privacy).

| Tier | Measured by (last email open/click) | Segment ID | Band count | Emails/day |
|---|---|---|---|---|
| **Hot** | clicked ≤30d **OR** new lead (added ≤30d) | `6a56b6ca0214b2a445afd14c` | 20,962 | **2** (both slots) |
| **Warm** | opened/clicked ≤60d | `6a56b6cdc19ef5cfaa55e558` | 43,368 | **1** (Slot A) |
| **Cold** | engagement evidence ≤180d (→ 60–180d after precedence) | `6a56b6d2ecba2f618e76927a` | 54,680 | **~2–3/week** |
| **Dormant** | no open/click 180d+ | `6a56b6d5ecba2f618e76927b` | 6,820 | **0** — suppress (manual sales blasts only) |

Boundaries **30 / 60 / 180** (founder-set 2026-07-14; the Hot window may widen later — a one-field edit). The tiers are nested recency **bands** (Hot ⊆ Warm ⊆ Cold); **precedence Hot > Warm > Cold > Dormant** assigns each contact exactly one tier at send. **Rationale:** this *throttles* frequency rather than *excluding* warm/cold (the prior system cut them off entirely, killing engagement) — only 180-day dormant contacts leave the daily engine.

**How it layers in (the engine "stage ∩ tier" build, pending):** each slot's campaign audience = the stage segment intersected with the tier — Slot A (8am) → Hot+Warm (+Cold on reduced days); Slot B (6pm) → Hot only; Dormant excluded. Implementation notes: (a) OmniSend campaign audiences UNION their included segments, so `stage ∩ tier` is achieved via **exclusion layering** (include stage, exclude the tiers that shouldn't get this slot) or pre-intersected segments; (b) true suppress = Dormant AND added >180d — a recent-but-never-engaged contact resolves to **Cold** by precedence, not Dormant. The engine's internal `compute_tier` (broken `updatedAt` signal) is retired in favor of these segments.

**Stage → segment map for the pilot:** Lead → #1–4 (#4 default) · New Buyer → #5 · Non-adopter → #6 · Adopter → #7 (all-time) / #8 (active) · Subscriber → #9 · engagement refinement → #10. Overlaps resolve at **send time** via include/exclude layering (one segment per person).

**Thresholds pinned to the engine** (so services agree): app-active window **7d** (`WINBACK_APP_INACTIVE_DAYS`), new-buyer window **7d** (`SUBSTAGE_T2_NEW_BUYER_DAYS`), subscriber rebill window **45d** (`SUBSCRIBER_ACTIVE_REBILL_DAYS`).
