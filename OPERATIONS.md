# Mystica Tagging Bridge — Operations & Reference

**Status: LIVE (2026-07-14).** Deployed on Railway, running a full Render → OmniSend tag
reconciliation every 15 minutes. This is the operator's system-of-record: what the service
does, exactly what it writes, how it's configured, how to run/verify/troubleshoot it, and the
design decisions (and hard-won gotchas) behind it.

For the *why* (the original problem + architecture), see `README.md`. For the tag naming
agreement across services, see `TAG_TAXONOMY.md`. For the Phase-0 data findings, see `FINDINGS.md`.

---

## 1. What it does (in one paragraph)

The bridge makes app-behavioral + membership data in **Render** (the app's Postgres DB) show up
reliably in **OmniSend** as tags + custom properties, so the Mystica Lifecycle Engine's segments
route people correctly. It replaces a lossy n8n workflow that had only ~600 contacts tagged
`app_install` against a reality of ~35k app users. It works by **desired-state reconciliation**:
each run pulls the current facts from Render, computes what *should* be true on each OmniSend
contact, diffs that against what *is* true, and applies **only the delta**. Tags are **add-only**
(it never removes tags); properties are set-to-value.

---

## 2. Where it runs

| | |
|---|---|
| **Platform** | Railway, project **courageous-charisma**, service **`mystica-onmisend-bridge`**, environment `production` |
| **Source** | GitHub `github.com/tcote9194/mystica-onmisend-bridge` (auto-deploys on push to `main`) |
| **Schedule** | Cron `*/15 * * * *` (every 15 min), set in the Railway dashboard (Settings → Cron Schedule) |
| **Start command** | `PYTHONPATH=src python -m bridge.cli run --live --create-missing` (in `railpack.json`) |
| **Build** | Railpack; `pip install -r requirements.txt` |

**Cron behavior note:** a Railway cron service **re-runs the same deployment** on each tick — it
does *not* create a new deployment entry per run. So to inspect a cron run, read the deploy logs
by timestamp (`railway logs -d`), not `railway deployment list`. If a run is still going when the
next tick fires, Railway **skips** that tick (no overlap). A fresh deploy runs on the next tick,
not immediately.

---

## 3. How it works (the pipeline)

```
Render (source of truth)                         OmniSend (destination)
  ├─ roster:        user_id, email, created_at,     current tags + custom
  │                 last_seen_at, name              properties per contact
  ├─ interaction:   chats / daily draws / readings          │
  │                 keyed by user_id                         │
  └─ reading stages: soulmate_reading_intent,                │
                     reading.delivered_at                    │
        │                                                    │
        ▼                                                    ▼
   DESIRED STATE  ─────────── diff (add-only) ──────────► APPLY delta
   (what should be true)                                  (tags + props)
```

- **Identity is lossless.** Interaction data lives in Render keyed by the same `user_id` the
  roster carries, so `user_id → email` is a 100% in-DB join. (This is the fix for the old n8n
  loss, where PostHog events had no email.) PostHog is optional and off by default
  (`INTERACTION_SOURCE=render`).
- **Two-gate write safety.** A live write requires **both** `DRY_RUN=false` (env) **and** the
  `--live` flag (in the start command). Missing either → the run computes the diff and logs it but
  writes nothing. A misconfigured deploy fails safe.
- **Guardrail.** A single run aborts (unless `--force`) if it would change more than
  `MAX_DIFF_FRACTION` (default 0.30) of the addressable audience — a blast-radius stop.
- **Read-only on Render.** The DB connection forces `default_transaction_read_only=on`; the
  service only ever SELECTs from Render and only writes to OmniSend.

---

## 4. What it writes to OmniSend (the complete map)

All tags use the `app: ` (colon-space) namespace to match the live OmniSend spelling. **Tags are
add-only** — the bridge never removes a tag.

### Lifecycle tags (from the Render roster)
| Tag | When it's set | Notes |
|---|---|---|
| `app: installed` | every rostered account (`created_at` present) | Bridge-owned. |
| `app: signup` | every rostered account (`created_at` present) | Bridge-owned. Same population as installed (Render has no separate signup event). |
| `app: login` | last-seen within `RENDER_LOGIN_WITHIN_DAYS` (default 30d) | "Recently active." Add-only, so it means "was active in 30d at some point," **not** a live rolling window — for a true rolling-active segment, filter on the `app_last_active` date property instead. |
| `app: updated` | **NOT written** | ⚠️ No app-version/update field exists in Render, so the bridge **cannot** reproduce this n8n tag. See §10. |

### Activity tags (from Render interaction rollups)
| Tag | When it's set |
|---|---|
| `app: chat` | ≥1 chat session |
| `app: daily_draw` | ≥1 daily card draw |
| `app: reading` | ≥1 delivered reading |

### Leading-indicator milestone tags (counts → segmentable tags)
OmniSend can't reliably threshold a numeric property, so each count is emitted as a **cumulative
tag ladder** (a user with ≥N gets the `_N` tag). Segment on the top rung.
| Ladder | Rungs (default) | Env |
|---|---|---|
| `app: chat_1` / `_2` / `_3` | 1,2,3 | `APP_CHAT_MILESTONES` |
| `app: daily_draw_1` / `_2` | 1,2 | `APP_DRAW_MILESTONES` |
| `app: interaction_11` | 11 | `APP_INTERACTION_MILESTONES` |

### Reading-fulfillment tags (from the Soulmate funnel)
| Tag | When it's set |
|---|---|
| `app: reading_abandoned` | `soulmate_reading_intent.status = 'pending_inputs'` (paid, form not filled) |
| `app: reading_delivered` | `reading.delivered_at` set (joined to the user's email) |

### Source tag
| Tag | When |
|---|---|
| `source: app` | on contacts **created** by `--create-missing` (app/email opt-in) |

### Custom properties
| Property | Meaning |
|---|---|
| `render_user_id` | Identity anchor (the Render `user_id`) — always written |
| `installed_at` / `signed_up_at` | Membership timestamps |
| `app_last_active` (+ mirrored `posthog_last_seen`) | Canonical last-activity date — drives adopter→lapsed; use for rolling-active segments |
| `app_chats` / `app_daily_draws` / `app_interactions` | Raw counts (reference; segments use the milestone tags) |

### Never written (engine-owned)
`engine_substage`, `track: *`, `tier: *`, `engagement_tier` — the engine derives these; the bridge
must never touch them. (`posthog_distinct_id` is only written once `WRITE_DISTINCT_ID_ON_INSTALL`
is confirmed true — default false.)

---

## 5. Configuration (Railway → Variables)

**Required** (everything else has a safe baked-in default):

| Variable | Value |
|---|---|
| `DRY_RUN` | `false` — the master write gate. Unset/true = never writes. |
| `RENDER_DATABASE_URL` | the read-only Render Postgres URL, ending `…/relationship_psychic_r5a3?sslmode=require` |
| `OMNISEND_API_KEY` | the OmniSend API key |

> ⚠️ **Paste `RENDER_DATABASE_URL` as a single clean line** starting with `postgresql://`. A stray
> leading `=`, wrapping quotes, or a second variable line pasted into the value crashed the service
> historically (see §9). The code now self-heals these, but keep the stored value clean.

**Common tunables** (defaults in parentheses):

| Variable | Default | Purpose |
|---|---|---|
| `RENDER_ACTIVE_WITHIN_DAYS` | `180` | Roster recency cutoff (6 mo). Only tag users seen within this window. `0` = full base. |
| `RENDER_LOGIN_WITHIN_DAYS` | `30` | `app: login` window (recently-active). `0` = disable the login tag. |
| `CREATE_MISSING_STATUS` | `subscribed` | Email status for contacts created by `--create-missing` (`subscribed` \| `nonSubscribed`). |
| `OMNISEND_VERSION` | `2026-03-15` | OmniSend API version header (required for writes). |
| `MAX_DIFF_FRACTION` | `0.30` | Abort if a run would change more than this share of the audience (unless `--force`). |
| `WRITE_DISTINCT_ID_ON_INSTALL` | `false` | Flip true only once Render `user_id` == PostHog `distinct_id` is confirmed. |
| `INTERACTION_SOURCE` | `render` | `render` (lossless, default) or `posthog`. |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_BRIDGE` | — | Optional failure/auth alerts. |

The confirmed Render schema (table `user`, the `user_role='user' AND deleted_at IS NULL` customer
filter, column names, the reading-stage tables) is baked in as code defaults — a bare deploy with
just the three required vars is correct and **cannot** accidentally tag advisors or the full
dormant base. Full annotated list: `.env.example`. Schema overrides: the `RENDER_*` vars.

---

## 6. Contact creation (`--create-missing`)

The cron start command includes `--create-missing`, so each run also **creates** in-scope app
users who aren't in OmniSend yet (as `subscribed`, per the app/email opt-in), tagging them in the
same pass. Brand-new app users get created + tagged automatically — not just existing contacts.

- The recency cutoff (`RENDER_ACTIVE_WITHIN_DAYS=180`) caps who's eligible, so this creates a few
  hundred at most over time, never the full ~35k base.
- To make the cron **tag-only** again (never create), remove `--create-missing` from the start
  command. Creation then becomes a deliberate manual run.
- To create without emailing until opt-in: `CREATE_MISSING_STATUS=nonSubscribed`.

---

## 7. Rate-limit handling

OmniSend rate-limits writes (~400/min; `x-rate-limit-limit` header). On a `429` the client honors
OmniSend's `Retry-After` (from the JSON body `retryAfter` or the header, +1s cushion, capped 60s)
and retries — so a run **paces to the limit** instead of hammering and failing. A large backfill
(e.g. the initial one-time `app: login` fill of ~1,400 writes) therefore drains in a single paced
pass with **0 failures**, though the run takes longer while it waits out rate windows. Steady-state
runs write almost nothing and never hit the limit. 5xx errors use plain exponential backoff.

---

## 8. Operations runbook

All commands assume the repo is linked to the service:
```
railway link -p courageous-charisma -e production
railway service mystica-onmisend-bridge
```

**Is it healthy / did the last run work?**
```
railway logs -d | grep -E "render connect|roster:|Applied:|Errors|Traceback"
```
A healthy run looks like:
```
render connect: host=dpg-…render.com dsn_len=167     ← clean connection
render roster: ~6800 users
plan: N changes + M creates over ~6800 matched (0 unresolved, <30% of audience)
Applied: N  Created: M  Errors: 0
```
Steady-state N/M are near-zero. A spike in `Applied` is normal right after a schema/tag change
(a one-time backfill) and drains over one or more ticks.

**Run a manual sync from your machine** (the repo auto-loads `.env`, which holds both keys):
```
# dry-run first (no --live = no writes):
PYTHONPATH=src python -m bridge.cli run --create-missing
# then live:
DRY_RUN=false PYTHONPATH=src python -m bridge.cli run --live --create-missing
```
Other CLI subcommands: `validate` (check config/mode), `probe` (read-only join check),
`baseline` (OmniSend snapshot), `membership` / `interaction` (each half alone).

**Read the stored Railway variables** (mask the password before sharing):
```
railway variables --service mystica-onmisend-bridge --json
```

**Tune a tag population** without a code change — set the env var in Railway and it applies next
tick: `RENDER_LOGIN_WITHIN_DAYS` (login breadth), `RENDER_ACTIVE_WITHIN_DAYS` (roster breadth),
the `APP_*_MILESTONES` ladders.

**Run the test suite** (no network):
```
PYTHONPATH=src python -m pytest -q
```

### Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `invalid connection option ""` | `RENDER_DATABASE_URL` has a leading `=` (paste artifact). Code self-heals it now; also fix the stored value. |
| `extra key/value separator "=" in "sslmode"` | A second `VAR=…` line was pasted into `RENDER_DATABASE_URL`. Same fix — clean the value. |
| `Refusing --live while DRY_RUN is true` | Set `DRY_RUN=false` in Railway. |
| `CONFIG ERROR: RENDER_DATABASE_URL is required` | The var isn't set. |
| Flood of `apply failed … 429` | Old code behavior; the current client honors `Retry-After` (§7). If seen, confirm the deploy is current. |
| One contact "created" every run forever | An invisible Unicode char in a Render email (`normalize_email` now strips control/format chars). |
| A run changes >30% of the audience and aborts | Intended guardrail. Re-run with `--force` if the large change is expected (e.g. a deliberate backfill). |

---

## 9. Design decisions & hard-won lessons

- **Add-only tags.** The bridge never removes a tag (removal risks touching foreign namespaces and
  monotonic facts stay true). Consequence: `app: login` accumulates and won't auto-expire — use the
  `app_last_active` property for live "active now" segments.
- **`app: login` = recently active (30d).** Chosen over "has ever logged in" (100% of roster,
  redundant with installed) and "returned after signup" (~3.8k). Tunable via `RENDER_LOGIN_WITHIN_DAYS`.
- **DSN self-heal.** `render_db._clean_dsn()` slices the real `postgres://…` URL out of any wrapping
  (leading `=`, quotes, `KEY=` prefix, `psql ` prefix, whitespace/CRLF) and truncates at the first
  whitespace, so a mangled Railway paste can't crash the connect. It logs the resolved host (never
  the password).
- **Email normalization strips invisible Unicode.** `normalize_email` removes Unicode control (Cc)
  and format (Cf) chars (bidi isolates, zero-width spaces, BOM) so the same address matches on both
  sides — otherwise one junk email churns as "uncreated" every run.
- **OmniSend write requirements.** Writes need the `Omnisend-Version` header, and the tag-add
  payload uses **`contactIDs`** (plural array), not `contactID`. (The mystica-engine OmniSend client
  has the singular bug — flag for its go-live.)
- **Read-only Render.** Enforced at the connection via a session `SET`, independent of a read-only
  role. Please also grant the DB role SELECT-only server-side.

---

## 10. Deprecating n8n (checklist)

The bridge now owns the app lifecycle + activity + reading tags. Before turning off the n8n
("NAN") workflow:

- [x] `app: installed`, `app: signup`, `app: login` — **bridge-owned**, spellings match live OmniSend.
- [x] `app: chat` / `app: daily_draw` / `app: reading` + milestone ladders — bridge-owned.
- [x] Reading stages (`app: reading_abandoned` / `app: reading_delivered`) — bridge-owned.
- [ ] **`app: updated`** — the bridge **cannot** produce this (no Render source field). If any
  OmniSend segment/automation filters on `app: updated`, either keep that one n8n path, or drop the
  tag from those segments first.
- [ ] Repoint any segment still filtering the old underscore spelling (`app_install`) to the
  namespaced tag (`app: installed`). New tags land only under the namespaced spelling.
