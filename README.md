# Mystica Tagging Bridge

**Status: BUILT + Render side VALIDATED against live data (2026-07-13). Only the OmniSend API key remains to run the diff + write.** Service under `src/bridge/` with a full unit-test suite (39 tests, all green, no network). Dry-run by default; nothing has written to OmniSend.

**Phase 0 result (see `FINDINGS.md`):** Render has **35,798 customer app-users** (vs ~600 tagged in OmniSend today), all with email. **Architecture change:** chats + daily draws live in Render keyed by `user_id`, so the interaction sync runs **from Render, not PostHog** — a lossless `user_id → email` join (100% resolution on 5,767 interaction users), which removes the identity-join loss that was the whole problem. **PostHog is now optional.**

This README is the problem + architecture context; `EXECUTION_PLAN.md` is the build/run plan and `FINDINGS.md` is the Phase-0 log the CLI appends run reports to. To run once the OmniSend key is set: `bridge baseline` → `bridge membership` (dry-run) → review → live.

A dedicated service (the "identity + tagging bridge") that gets **reliable, complete app-behavioral and membership data into OmniSend as tags/custom fields**, so the Mystica Lifecycle Engine's segments route people correctly.

---

## 1. Why this exists (the problem)

The Mystica Lifecycle Engine (`~/Desktop/mystica-engine/`) routes every contact into a **journey-stage segment** (T2 Adopter, T2 Non-adopter, New Buyer, Subscriber, the pre-purchase lead buckets…). Those segments are OmniSend segments defined by **tags + custom fields** on each contact. The engine is only as accurate as those tags.

Today the app-behavioral tagging (installed / signed up / chatted / engagement depth → OmniSend) is **lossy**:

- It currently lives in an **n8n** workflow ("NAN") that is unreliable/incomplete — dropped payloads, no retries, rate limits.
- Only **~600 contacts are tagged `app_install`**, which is known to be **far lower than reality**.
- The consequence is a real routing bug: a person who *is* active in the app but never got tagged is classified **T2 Non-adopter** instead of **T2 Adopter**, so the engine sends them "come into the app" copy they don't need, and withholds the adopter/chat content they should get. **Segment accuracy is a go-live blocker.**

### Root cause: an identity join
- **PostHog** identifies app users by an internal `distinct_id` / `user_id` — most interaction events **carry no email**.
- **OmniSend** contacts are keyed by **email**.
- An event with no email can't find its OmniSend contact → the tag silently drops. That's the bulk of the loss.

The proven counter-example: the **free-question funnel → OmniSend** push works reliably, *because it has the email at opt-in.* The fix is to give the app pipeline the same thing — a reliable email for every event.

---

## 2. The architecture (the fix)

Three systems, and the **join is the whole trick**:

```
   RENDER (app DB — source of truth)          POSTHOG (interaction analytics)
   ┌─────────────────────────────┐            ┌──────────────────────────────┐
   │ users: user_id, EMAIL,      │            │ events by user_id/distinct_id│
   │        installed_at,        │            │ (chats, daily draws,          │
   │        signed_up_at, …      │            │  engagement) — usually NO email│
   └──────────────┬──────────────┘            └───────────────┬──────────────┘
                  │  user_id → email (identity bridge)         │ user_id (no email)
                  │                                            │
                  └───────────────┬────────────────────────────┘
                                  │  resolve email via Render
                                  ▼
                        THE TAGGING BRIDGE (this project)
                      batch + idempotent · nightly or faster
                                  │  upsert by EMAIL
                                  ▼
                        OMNISEND (tags + custom fields)
                 app_install · app_signup · app_chat · engagement · engine_substage
                                  │
                                  ▼
                MYSTICA ENGINE segments (T2 Adopter / Non-adopter / …)
```

- **Render is the source of truth** for app users and the **identity bridge** (`user_id ↔ email`) *and* the hard membership facts (installed, signed up).
- **PostHog is the interaction depth** (chats, daily draws, engagement / leading indicators) — keyed by `user_id`, usually email-less.
- **The bridge** joins PostHog events to Render (`user_id → email`), so an email-less event becomes an OmniSend tag on the right contact. Render also feeds the membership tags directly.

### Division of source
| Signal | Source of truth | Why |
|---|---|---|
| Installed / signed up (membership) | **Render** | Hard facts + has the email; no lossy webhook needed |
| `user_id → email` identity | **Render** | The join key that makes PostHog usable |
| Chats / daily draws / engagement depth (leading indicators) | **PostHog** | Where the rich interaction analytics live (3-chats / 11-interactions / 2+ daily-draws) |
| The tags/segments themselves | **OmniSend** | The destination the engine reads |

---

## 3. Systems inventory + access

| System | What it is | What we need from it | Access status |
|---|---|---|---|
| **Render** | The app's Postgres DB (source of truth: users, installs, signups, chats) | user roster: `user_id`, **email**, install/signup timestamps, chat records | **NEED CREDS** — Tom to provide a connection string / read creds. (Engine only has Supabase creds today; Render is separate.) |
| **PostHog** | Product analytics (project **67576**) | interaction events keyed by `user_id` (chats, daily draws, engagement); the leading-indicator definitions | Personal API key needed (or reuse an existing analysis key — see [[project_mystica_highvalue_indicators]]) |
| **OmniSend** | Email/SMS platform, the tag/segment destination | write tags + custom properties on contacts by email; read segments | **HAVE IT** — OmniSend MCP is connected (`get_contacts`, `patch_contacts`, `post_contacts_tags`, `get_segments`, …). Contacts keyed by email; custom props already rich (see the engine's `engine_substage` + fq fields). |
| **n8n** ("NAN") | The current, lossy PostHog→OmniSend workflow to be replaced | trace WHAT it does + WHY it drops, so we don't repeat its mistakes | n8n MCP is connected (`n8n-mcp`: list/get workflows). Trace it in Phase 0. |
| **Mystica Engine** | The consumer of these tags (`~/Desktop/mystica-engine/`) | its segment definitions (`segments.py` STAGE_SEGMENTS keyed on `engine_substage`; the app-adoption signal for the `adopter` substage) | Local repo. |

---

## 4. What the bridge must produce (outputs into OmniSend)

Tags / custom properties on each contact, keyed by email:

- **Membership (from Render):** `app_install` (+ `installed_at`), `app_signup` (+ `signed_up_at`).
- **Interaction (from PostHog, email resolved via Render):** `app_chat` (has chatted + first/last chat), engagement level (e.g. the 3-chats / 11-interactions / 2+ daily-draws leading indicators), `last_active_at`.
- **Derived stage input:** whatever the engine's `compute_substage` needs to flip a contact to `adopter` vs `non_adopter` (app-adoption signal) — so `engine_substage` lands correctly and the OmniSend stage segment is accurate.

These must be **idempotent** (re-running never double-tags) and **fresh** (a nightly-or-faster cadence so a new app user becomes an adopter quickly, not days late).

---

## 5. How this feeds the engine (why it matters)

The engine's `segments.py` `STAGE_SEGMENTS` are keyed on the **`engine_substage`** custom property, which the engine computes from purchase + **app-adoption** + subscription + days-since. The app-adoption input is exactly what this bridge makes reliable. Get this right and:
- **T2 Adopter** correctly captures everyone active in the app (not just the 600).
- **T2 Non-adopter** stops over-counting real app users.
- The adopter's app-activation / chat content + pushes fire for the right people.

There is also a downstream tie-in: once app activity is reliably known, the **Clarity-driven daily note** (Tier-2, `note_cycle`) has a trustworthy "is this contact app-active / engaged" signal to decide morning/evening/push generation.

---

## 6. Where it lives

**A focused microservice (this folder), not inside the send engine.** Rationale: it spans three external systems (Render + PostHog → OmniSend), it's a distinct concern from the two-slot *send* logic, and it needs its own reliability + cadence. It could be folded into the engine as a cron if consolidation is preferred later — Tom's call at build time.

---

## 7. Pointers

- Engine (the consumer): `~/Desktop/mystica-engine/` — `src/engine/segments.py` (STAGE_SEGMENTS, `engine_substage`, `compute_lead_segment`), `sources.py` (OmniSend field mapping), `notes.py`/`note_cycle.py` (the Clarity note).
- Memory: the two-slot reframe note (`project_mystica_engine_two_slot_reframe`) — the full lifecycle-engine + segment context; `project_mystica_highvalue_indicators` (PostHog 67576, the leading indicators); `reference_omnisend_engagement_cpel` (OmniSend has no per-email opens; engagement derived from tags/status/posthog).
- **Build plan + open questions: `BUILD_PLAN.md` in this folder.**
