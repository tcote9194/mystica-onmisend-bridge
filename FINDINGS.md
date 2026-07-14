# FINDINGS — Mystica Tagging Bridge

The living Phase-0 investigation log + the run paper-trail. Read `EXECUTION_PLAN.md`
for the plan; this file collects what we LEARN and what each run DID. The `bridge`
CLI appends a `## Run · …` block here after every sync (unless `--no-findings`).

**Phase 0 status: Render side COMPLETE (2026-07-13). Only the OmniSend-side diff +
live write remain, blocked on the OmniSend API key.**

---

## 0.1 Engine field contract — ✅ DONE (verified in engine code)

- `app_adopted` (engine) = **any tag starting with `app:`** OR non-empty
  **`posthog_distinct_id`** custom property. Source: `mystica-engine/src/engine/state.py:648`.
- `posthog_last_seen` custom property = engine recency evidence (`state.py:604–623`).
- `engine_substage`, `track:`, `tier:`, `substage:` are **engine-owned** — the bridge
  never writes them.
- Legacy n8n tag `app_install` (underscore) is **not** read by the engine. Bridge writes
  `app: install` / `app: signup` / `app: chat` (colon-prefixed, engine-readable).

## 0.2 Render schema — ✅ DONE

- **DB:** PostgreSQL 14 on Render (`relationship_psychic_r5a3`, Oregon). Connected
  read-only (`default_transaction_read_only=on` enforced at the connection). 51 public tables.
- **Users table is `user`** (singular — a **reserved word**; the client double-quotes it).
  49 columns; it holds **customers AND psychic advisors AND admins** in one table.
  - `id` uuid (PK, not null) — the identity key (PostHog distinct_id candidate, see 0.4).
  - `email` varchar (**not null** — every user has one), unique among non-deleted.
  - `created_at` timestamp (not null) — registration = install/signup timestamp.
  - `onboarding_completed_at` — **unused (0 rows set)**, so `created_at` is the anchor.
  - `last_seen_at` — **populated for all users** (app recency, tracked natively).
  - `user_role`, `deleted_at` — the customer filter (below).
  - subscription fields (`has_subscription` etc.), `firebase_id`, `expo_push_token`.
- **Row counts:** 39,644 total; 3,053 soft-deleted; **36,591 non-deleted (unique emails).**
- **By role (non-deleted):** `user` = **35,798** (customers), `psychic` = 783, `admin` = 10.
- **Customer filter (config `RENDER_ROSTER_WHERE`):** `user_role = 'user' AND deleted_at IS NULL`
  → **35,798 addressable customer app-users**, all with email + last_seen + created_at.

**Config (in `.env`):** `RENDER_USERS_TABLE=user`, `RENDER_USER_ID_COL=id`,
`RENDER_EMAIL_COL=email`, `RENDER_INSTALLED_AT_COL=created_at`,
`RENDER_SIGNED_UP_AT_COL=created_at`, `RENDER_LAST_SEEN_COL=last_seen_at`.

## 0.3 n8n "NAN" workflow trace — ⬜ TODO (needs n8n MCP)
Not yet traced. The Render evidence already proves the undercount's SIZE (35,798 vs ~600);
the n8n trace remains useful to (a) confirm the exact legacy tag spelling so we know whether
to dual-write, and (b) know which OmniSend segments filter on it. Do before decommissioning.

## 0.4 Identity model — ⬜ PARTIAL (Render side done; PostHog side needs the key)
- Render `id` (uuid) is the identity key and joins losslessly to email in-DB.
- **Render is now the interaction source (see 0.5b), so the PostHog identity question is
  no longer a blocker.** It only matters for *whether we write `posthog_distinct_id`*: until
  it's confirmed that PostHog's distinct_id == the Render uuid, `WRITE_DISTINCT_ID_ON_INSTALL`
  stays `false` (the `app:*` tags carry adoption regardless). Confirm if/when PostHog is wired.

## 0.5a Membership join (Render → OmniSend) — ⬜ needs OmniSend key
Render fully proven: `RenderDB.fetch_roster()` returns 35,798 customers → membership
desired-state = 35,798 contacts, each `app: install` + `app: signup` + `installed_at` +
`signed_up_at` + `posthog_last_seen` (from Render `last_seen_at`). The remaining step is the
diff against live OmniSend contacts (needs the OmniSend API key) to see how many EXIST there.

## 0.5b Interaction join (Render → OmniSend) — ✅ Render side DONE, **100% resolution**
**Key architecture finding: chats + daily draws live in Render, keyed by `user_id`, so the
interaction sync runs from Render with ZERO identity-join loss — the exact loss that made n8n
lossy. PostHog is NOT required.** (`INTERACTION_SOURCE=render`, the default.)
- `chat_message(user_id, chat_session_id, created_at)` — 642,932 rows; agg runs in ~0.4s.
- `daily_card_draw(user_id, drawn_at)` — 14,386 rows.
- `RenderDB.fetch_interaction_aggregates()` (live): **5,767 interaction users, 100% resolved
  to email, 0 unresolved** (vs the email-less-event loss PostHog would incur).
  - `app: chat` → **4,864** (customers who have chatted).
  - `app_engagement_level`: **2,606 engaged** (≥3 chats OR ≥2 draws OR ≥11 interactions),
    3,161 active.
  - Also sets `app_chat_count`, `app_first_chat_at`, `app_last_chat_at`, `posthog_last_seen`.

## 0.6 Baseline snapshot — ⬜ OmniSend side needs the key
Render truth (the "should be" numbers the fix targets):
| Signal | Render count | OmniSend today |
|---|---|---|
| App users (`app: install`) | **35,798** | ~600 tagged |
| Have chatted (`app: chat`) | **4,864** | — |
| Engaged (high-value indicators) | **2,606** | — |
Run `bridge baseline` once the OmniSend key is set to fill the right column + current
T2_ADOPTER / T2_NONADOPTER segment sizes.

## 0.7 Decisions (Tom)
- **Tag transition / dual-write** — pending 0.3 (which OmniSend segments use the legacy tag).
  Set `LEGACY_DUAL_WRITE_TAGS` if any must keep working during transition.
- **Adopter semantics / `posthog_distinct_id`** — currently `WRITE_DISTINCT_ID_ON_INSTALL=false`
  (don't assert an unconfirmed PostHog id; `app:*` tags carry adoption). Revisit if PostHog is wired.
- **Cadence** — nightly recommended.
- **Hosting** — Railway cron worker (recommended).



---

_Run reports (with per-contact detail) are written to the gitignored `data/runs/` directory, not appended here — this file stays a PII-free investigation log._
