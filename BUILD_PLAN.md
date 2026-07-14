# Build Plan — Mystica Tagging Bridge

**Read `README.md` first for the problem + architecture.** This is the phased build, the open questions, and the exact first steps for a future session. **Nothing here is built yet.**

---

## Guiding principles

- **Render is the source of truth + identity bridge.** Never treat PostHog as authoritative for identity or membership.
- **Idempotent everywhere.** Every write to OmniSend must be safe to re-run — tag-set, not tag-append-blindly; upsert custom props by email.
- **Reliable over real-time.** A dependable nightly (or hourly) full-ish sync beats a fragile event webhook. This is why we're replacing n8n.
- **Don't write to Render.** Read-only. Render is the app's production DB; the bridge only reads users + facts from it.
- **Don't break the engine's contract.** The engine reads specific OmniSend custom props/tags (`engine_substage`, the fq_* fields, tier tags). Confirm exact names against `~/Desktop/mystica-engine/src/engine/segments.py` + `sources.py` before writing, so we set the fields the engine actually reads.
- **Everything stays reviewable / reversible.** Dry-run mode first (log what *would* be tagged, tag nothing) before any live write.

---

## Phase 0 — Investigate (do this before writing any code)

The build is blocked on facts we don't have yet. Resolve these first:

1. **Render schema.** Get read creds (connection string). Find the users table: confirm it has `user_id` (the same id PostHog uses as `distinct_id`/`user_id`), **email**, install timestamp, signup timestamp. Find the chats/sessions table(s). Document the exact table + column names.
2. **Trace the current n8n workflow.** Use the n8n MCP (`n8n-mcp` — `search_nodes`/`get`/list workflows) to pull the existing PostHog→OmniSend workflow. Document: what triggers it, what it reads, how it matches to OmniSend, and *where it drops records* (the hypothesis: it only fires on events that happen to carry an email, or it matches on a field that's often blank). Confirm the ~600 undercount cause.
3. **PostHog identity model.** Confirm how app users are identified in project 67576 — is `distinct_id` the Render `user_id`? Is there ever an email on the person? Which events represent the leading indicators (chats, daily draws, 11-interactions)? See [[project_mystica_highvalue_indicators]] and [[reference_mystica_stripe_posthog_analysis]] for the event vocabulary already mapped.
4. **Validate the join is possible.** Take 10 PostHog active users → look up `user_id` in Render → get email → confirm that email exists as an OmniSend contact. If this round-trips, the architecture is proven. If Render emails don't match OmniSend emails (casing, aliases, multiple emails), that's a normalization sub-task to scope here.
5. **Confirm the engine's field contract.** Read `segments.py` + `sources.py`: what exact tag/prop does `compute_substage` read to decide `adopter` vs `non_adopter`? What are the exact tier tags? Write the target field list against reality, not assumption.

**Phase 0 output:** a short `FINDINGS.md` in this folder — Render schema, n8n failure mode, PostHog event map, join-validation result, engine field contract. That doc unblocks Phase 1.

---

## Phase 1 — Identity bridge (Render `user_id → email`)

- Build the read-only Render client + a `resolve_emails(user_ids) -> {user_id: email}` function (batched).
- Normalize emails (lowercase/trim) to match OmniSend's keying.
- Cache the roster per-run (the full user table is small enough to pull once).
- **Test:** a known set of user_ids resolves to the right emails; unknown ids return cleanly (no crash).

## Phase 2 — Membership sync (Render → OmniSend), the quick win

This alone likely fixes most of the 600 undercount, because Render *has* the emails — no PostHog join needed.

- Pull all users with install/signup facts from Render.
- Upsert OmniSend contacts by email with `app_install` / `app_signup` tags + `installed_at` / `signed_up_at` custom props (idempotent set).
- Dry-run first: report "would tag N contacts `app_install`, currently 600 tagged" — that number is the proof the fix works.
- Then live-write. **Segment accuracy jumps here.**

## Phase 3 — Interaction sync (PostHog + Render → OmniSend)

- Pull the leading-indicator events from PostHog (chats, daily draws, engagement) keyed by `user_id`.
- Resolve `user_id → email` via the Phase 1 bridge; drop (and log-count) any that don't resolve.
- Upsert `app_chat`, engagement level, `last_active_at` onto the OmniSend contact by email.
- Idempotent; dry-run then live.

## Phase 4 — Engine integration + go-live

- Ensure the fields written are exactly what `compute_substage` reads so `engine_substage` recomputes correctly and the OmniSend stage segments (T2 Adopter/Non-adopter) refill accurately.
- Re-check the four-customer / journey routing against real tags — confirm no one is mis-stage'd.
- Schedule (nightly first; tighten to hourly if adopter-freshness matters). Add retries + a failure alert (Slack chip) so it never silently degrades the way n8n did.
- Decommission the n8n workflow once the bridge is proven.

---

## Open questions (for Tom / to resolve in Phase 0)

- **Render access** — connection string / read creds? (Blocker for everything.)
- **PostHog key** — reuse the existing analysis key or a fresh personal API key?
- **`user_id` identity match** — is Render `user_id` literally the PostHog `distinct_id`? If not, what maps them?
- **Email match rate** — do Render emails cleanly match OmniSend contacts? Any normalization needed?
- **Hosting** — Railway (same as the other Mystica services) vs a Render cron vs local scheduled? (Lean Railway for parity + the deploy playbook we already have — see [[feedback_railway_deployment]].)
- **Cadence** — is nightly fresh enough for adopter routing, or do we want hourly?
- **Scope creep guard** — this service is *tagging only*. Analytics/reporting stays in PostHog + the attribution service; don't absorb them here.

---

## First steps for the next session (start here)

1. Ask Tom for **Render read creds** + confirm the **PostHog key**.
2. Read `~/Desktop/mystica-engine/src/engine/segments.py` + `sources.py` → write the exact target field list into `FINDINGS.md`.
3. Trace the n8n workflow via the n8n MCP → document the drop cause in `FINDINGS.md`.
4. Once Render creds land: run the Phase-0 **join-validation** (10 users round-trip) → if green, start **Phase 2 (membership sync)** as the fast win.

Keep the main window for planning; spin up **Opus subagents** for the build work (per [[feedback_opus_subagents_planning_window]]). Nothing writes to OmniSend live until a dry-run has been reviewed.
