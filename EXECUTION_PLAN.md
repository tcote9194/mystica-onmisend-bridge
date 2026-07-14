# Execution Plan — Mystica Tagging Bridge

**Written 2026-07-13 (planning session — nothing executed).** Read `README.md` (problem + architecture) and `BUILD_PLAN.md` (original phase sketch) first. This document supersedes `BUILD_PLAN.md`'s phase sketch with a concrete, execution-ready plan, incorporating the engine field contract verified against the live engine code this session.

> **UPDATE 2026-07-13 (post-build, post-Render-discovery) — ARCHITECTURE CHANGE:**
> Phase 0 discovery against live Render found that **chats and daily draws live in
> Render, keyed by `user_id`** (`chat_message`, `daily_card_draw`). The interaction
> sync therefore runs **from Render, not PostHog** (`INTERACTION_SOURCE=render`, the
> default) — joining `user_id → email` in-DB with **100% resolution and zero
> identity-join loss**, which was the entire risk this project was chartered to solve.
> **PostHog is now an optional supplement, not a dependency.** The `render→OmniSend`
> flow below replaces `PostHog(+Render)→OmniSend` for §3 Phase 3. Live numbers and the
> confirmed schema are in `FINDINGS.md`.

---

## 0. Verified this session: the engine's field contract (Phase-0 item 5 — DONE)

Read from `~/Desktop/mystica-engine/src/engine/` (`state.py`, `segments.py`, `sources.py`, `tags.py`):

1. **`app_adopted` is derived, not read directly** — `state.py:648`:
   ```python
   app_adopted = any(str(t).startswith("app:") for t in tags) or bool(parsed.get("posthog_distinct_id"))
   ```
   A contact flips to app-adopted when they have **any tag starting with `app:`** (colon prefix) **OR** a non-empty **`posthog_distinct_id` custom property**. Nothing else.

2. **⚠️ CRITICAL MISMATCH:** the current n8n workflow reportedly writes the tag **`app_install`** (underscore, no colon). `app_install` does **not** match `startswith("app:")`. If the live spelling really is `app_install`, then even the ~600 tagged contacts do not flip `app_adopted` via tags — the engine sees adopters only through `posthog_distinct_id`. **Phase 0 must inspect real contact tag spellings** (task 0.3/0.6). Regardless of what's live, the bridge writes `app:`-prefixed tags to satisfy the engine.

3. **`posthog_last_seen` custom property** is engine recency evidence (`state.py:604–623`): with an `app:*` tag present it counts as click-strength app activity; it's also a recency candidate for tier computation. The bridge should keep it fresh.

4. **`engine_substage` is engine-OWNED output.** The engine computes substage in `compute_substage()` (`state.py:206`) and pushes it to OmniSend itself; the 7 stage segments (`segments.py` `STAGE_SEGMENTS`) filter on it. **The bridge must never write `engine_substage`, `track:`, `tier:`, or `substage:` tags** — it writes only the raw app signals the engine derives from.

5. **`compute_substage` Track-2 priority order** (`state.py:226–236`): `subscriber` > engagement-`lapsed` > `new_buyer` (Soulmate ≤7d) > **`adopter` (app_adopted)** > `non_adopter`. So reliable app signals flip exactly the T2_ADOPTER/T2_NONADOPTER boundary this project exists to fix.

6. **Tag namespace convention** (`tags.py:63`): engine owns `track:`/`tier:`/`substage:`; `app:*` and `purchase:*` are foreign/passthrough. The **bridge owns the `app:*` namespace**.

7. **Engine's PostHog event vocabulary hint** (`sources.py` `PostHogSource.DEFAULT_EVENTS`): `app_login`, `card_draw`, `chat_session_start`, `reading_view` — candidate event names to verify against live project 67576 in Phase 0.

8. **Semantic flag for Tom:** because `posthog_distinct_id` presence *alone* flips `app_adopted`, writing it for every Render user (even install-only, never-active users) makes **adopter = installed**, not adopter = active. If Tom wants activity-gated adoption, that's an *engine-side* rule change; the bridge's job is to write granular facts (install/signup/activity timestamps) so either policy is computable. Decide in Phase 0 review.

---

## 1. Target outputs (what the bridge writes to OmniSend, by email)

| Output | Type | Source | Notes |
|---|---|---|---|
| `app: install` | tag | Render | Exact spelling decided in Phase 0 (must start `app:`) |
| `app: signup` | tag | Render | Same |
| `app: chat` | tag | PostHog (join via Render) | Has ever chatted |
| `installed_at`, `signed_up_at` | custom props | Render | ISO timestamps |
| `posthog_distinct_id` | custom prop | Render (user_id, if 0.4 confirms identity) | The prop the engine's adopted-check reads; also enables future direct joins |
| `posthog_last_seen` | custom prop | PostHog | Engine recency evidence |
| `app_chat_count`, `app_first_chat_at`, `app_last_chat_at` | custom props | PostHog | Interaction depth |
| `app_engagement_level` | custom prop | PostHog (derived) | Per the 3-chats / 11-interactions / 2+ daily-draws leading indicators |

Explicit **non-outputs**: `engine_substage`, `track:`/`tier:`/`substage:` tags (engine-owned), anything analytics/reporting (stays in PostHog + attribution service).

Legacy `app_install`-style tags: **inventory before touching** — if any existing OmniSend segments/automations filter on the old spellings, keep writing both old + new during transition, and only retire the old spelling after those segments are repointed.

---

## 2. Design decisions

- **Desired-state reconciliation, not event streaming.** Each run: pull Render roster + PostHog aggregates + current OmniSend contacts → compute the *desired* tag/prop set per email → **diff** against current → apply only the delta. Idempotency is structural (re-run ⇒ empty diff), dry-run is free (print the diff, apply nothing), and it's self-healing (a missed run just makes the next diff bigger). This is exactly why n8n's per-event webhook model was lossy — we don't rebuild that.
- **Read-only on Render, enforced mechanically**: connect with `default_transaction_read_only=on` and ask Tom for a read-only DB role — not just a convention.
- **Email normalization** on both sides of every join: lowercase + trim (OmniSend keys on email; engine's Clarity source already lowercases — same idiom).
- **PostHog aggregation server-side**: use the HogQL query endpoint (`POST /api/projects/67576/query`) to aggregate chats/draws/last-seen per `distinct_id` in one query, rather than paging raw events client-side.
- **Run ledger**: a small local state file/SQLite (cursor for incremental PostHog pulls, run reports, before/after counts). Full re-derive stays cheap enough that the ledger is an optimization, not a correctness dependency.
- **Dry-run first, always**: `--dry-run` is the default mode; `--live` is explicit. No live write until Tom has reviewed a dry-run diff report.
- **Stack**: Python 3.11+, httpx + dataclasses (mirror mystica-engine idioms), `psycopg` for Render Postgres, OmniSend REST v5 directly (API key from the engine's env — the MCP is for interactive sessions, not the service). Batched writes + retry/backoff for OmniSend rate limits.
- **Repo layout**:
  ```
  src/bridge/
    config.py       env + settings
    render_db.py    read-only Render client (roster, facts)
    identity.py     user_id→email resolution + normalization + roster cache
    posthog.py      HogQL aggregates per distinct_id
    omnisend.py     read (contacts+tags+props) & batched write client
    desired.py      desired-state computation (membership + interaction)
    plan.py         diff current vs desired → change plan + report
    apply.py        execute a change plan (rate-limited, resumable)
    report.py       run summary + Slack alert
    cli.py          run / dry-run / phase selection
  tests/            fixture-driven, no network
  ```
- **Hosting**: Railway cron worker (recommended — parity with other Mystica services and the existing deploy playbook; it's a batch job, no web server needed). Nightly first, tighten to hourly if adopter-freshness demands.
- **Observability**: per-run summary (contacts scanned, diff size, applied, unresolved-join count, error count) + Slack chip on failure AND on success-with-anomaly (e.g. diff size > guardrail). Never silently degrade — that's the n8n failure mode being replaced.
- **PII care**: emails are the join key; never log full rosters, log counts + samples only.
- **Write guardrail**: if a computed diff would modify more than a configurable share of the audience (e.g. >30% after the initial backfill), require an explicit `--force` — protects against a bad Render query or empty PostHog pull mass-mutating tags.

---

## 3. Phases

### Phase 0 — Investigate & prove the join (no service code; blocked on creds)
**Blockers to request from Tom first: Render read creds (read-only role + connection string), PostHog personal API key.**

| # | Task | Method | Output |
|---|---|---|---|
| 0.1 | Engine field contract | ✅ DONE (this doc, §0) | Target field list (§1) |
| 0.2 | Render schema discovery | psql with read-only creds: find users table (user_id, email, install/signup timestamps), chats/sessions tables; row counts | Exact table/column names in FINDINGS.md |
| 0.3 | Trace the n8n "NAN" workflow | n8n MCP: locate the PostHog→OmniSend workflow; document trigger, matching logic, **exact tag/prop spellings it writes**, and the drop cause | Failure-mode writeup; legacy-tag inventory |
| 0.4 | PostHog identity model | Project 67576: is `distinct_id` the Render `user_id`? Do persons ever carry email? Verify live event names for chats/draws/interactions (candidates: `app_login`, `card_draw`, `chat_session_start`, `reading_view`) | Event map + identity confirmation |
| 0.5 | Join validation | 10 PostHog-active `distinct_id`s → Render email → OmniSend contact lookup; measure match rate; note normalization issues | Join proven (or normalization sub-task scoped) |
| 0.6 | Baseline snapshot | Counts: contacts tagged with each legacy app tag (expect ~600), contacts with `posthog_distinct_id` set, current T2_ADOPTER vs T2_NONADOPTER segment sizes, Render totals (installed / signed up with email) | The before-numbers every later phase proves itself against |
| 0.7 | Decision review with Tom | Tag-format transition plan (§1), adopter semantics (§0.8), cadence, hosting | Decisions locked in FINDINGS.md |

**Exit criteria:** `FINDINGS.md` complete; 10-user join round-trips; tag-format + adopter-semantics decisions made. *Estimated: half a day once creds land.*

### Phase 1 — Service skeleton + identity bridge
- Scaffold repo (layout above), config/env handling, test harness with fixtures.
- `render_db.py`: read-only client, `fetch_roster()`; `identity.py`: `resolve_emails(user_ids) -> {user_id: email}`, normalization, per-run roster cache (roster is small enough to pull whole).
- Tests: known ids resolve; unknown ids return cleanly; read-only enforced.

**Exit criteria:** identity bridge green against live Render (read-only), zero writes anywhere. *Estimated: half a session.*

### Phase 2 — Membership sync (Render → OmniSend) — **the quick win**
No PostHog join needed (Render has emails), and it fixes most of the undercount.
- `desired.py` (membership half): for every Render user with install/signup facts → desired `app: install` / `app: signup` tags (+ legacy spellings if 0.3 says segments depend on them), `installed_at` / `signed_up_at`, and `posthog_distinct_id` (per the 0.7 adopter-semantics decision).
- `omnisend.py` write path: batched, rate-limited, retrying upserts by email.
- `plan.py` + `report.py`: dry-run diff report — **"would tag N contacts `app: install` (legacy baseline: ~600)"**. That N vs 600 is the headline proof.
- Tom reviews the dry-run → `--live` apply → re-snapshot 0.6 counts; run the engine's state recompute and confirm T2_NONADOPTER→T2_ADOPTER movement (the engine's own count-sum audit guards double-sends).

**Exit criteria:** OmniSend tagged count ≈ Render truth; stage segments visibly refill; before/after report archived. *Estimated: one session + Tom review.*

### Phase 3 — Interaction sync (PostHog + identity join → OmniSend)
- `posthog.py`: HogQL aggregate per `distinct_id` — chat count, first/last chat, daily-draw counts, interaction count, last-seen.
- Resolve `distinct_id → email` via Phase-1 bridge; **count + log unresolved ids** (resolution rate is a first-class report metric — it was the invisible loss in n8n).
- Desired-state: `app: chat`, `app_chat_count`, `app_first_chat_at`, `app_last_chat_at`, `app_engagement_level` (3-chats / 11-interactions / 2+ daily-draws rules), `posthog_last_seen`.
- Dry-run diff review → live.

**Exit criteria:** interaction props live on contacts; join resolution rate measured and acceptable; report archived. *Estimated: one session.*

### Phase 4 — Scheduling, alerting, engine go-live, decommission
- Deploy to Railway as a **cron worker** (nightly). Follow the Railway playbook (fetch+merge before push; no `[start]` in nixpacks.toml — and none needed, this is a cron job not a web service).
- Slack failure alert + daily run-summary chip; retries with backoff; the >30%-diff guardrail live.
- Engine verification: `engine_substage` recomputes correctly, the 7 stage segments refill, engine audit shows no double-sends; spot-check a handful of known app users land in T2_ADOPTER.
- Watch several days of runs; tighten to hourly only if adopter-freshness demands it.
- **Decommission n8n**: disable the NAN workflow (don't delete), keep 2 weeks for rollback, then archive. Repoint/retire any OmniSend segments still filtering on legacy tag spellings.

**Exit criteria:** unattended nightly runs for a week with clean reports; n8n disabled; segment accuracy no longer a go-live blocker for the engine. *Estimated: half a session + a week of passive watching.*

---

## 4. Open decisions for Tom (carried from BUILD_PLAN, updated)

1. **Render read creds** — read-only role + connection string. *(Blocker for everything.)*
2. **PostHog key** — reuse the existing analysis key or issue a fresh personal API key.
3. **Tag format transition** — bridge writes `app:`-prefixed (engine contract, non-negotiable); do we dual-write legacy spellings during transition, and which existing segments/automations must be repointed? (Answered by 0.3 inventory.)
4. **Adopter semantics** — is install alone "adopted" (current engine behavior once `posthog_distinct_id` is set), or should the engine gate on recent activity? Bridge writes granular facts either way; this is an engine-side policy call.
5. **Cadence** — nightly first (recommended); hourly only if adopter-freshness proves to matter.
6. **Hosting** — Railway cron worker (recommended, for parity).

Scope guard (unchanged): **tagging only.** Analytics/reporting stays in PostHog + the attribution service.

---

## 5. Execution notes for the build sessions

- Keep the main window for orchestration/planning; spawn **Opus subagents** for the build work.
- Nothing writes to OmniSend live until a dry-run diff has been reviewed — every phase.
- Each phase ends by appending its numbers (before/after counts, resolution rates) to `FINDINGS.md`, so the paper trail of "the fix worked" lives in the repo.
