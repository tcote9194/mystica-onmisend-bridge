# Deploy — Railway (GitHub source)

The bridge runs as a **Railway cron service** that reconciles Render → OmniSend tags
every 15 minutes. Railway builds from this GitHub repo; the `.env` is **not** committed,
so the environment is configured in the Railway dashboard.

## 1. Create the service
- New Railway service → **Deploy from GitHub repo** → `tcote9194/mystica-onmisend-bridge`.
- Build is automatic: `railpack.json` / `nixpacks.toml` run `pip install .` and set the
  start command to `python -m bridge.cli run --live`.

## 2. Environment variables (Railway → Variables)

**Required** (the only things not safely defaulted in code):

| Variable | Value |
|---|---|
| `DRY_RUN` | `false` — the master gate. Without this the run **refuses to write** (safe). |
| `RENDER_DATABASE_URL` | the read-only Render Postgres URL (include `?sslmode=require`) |
| `OMNISEND_API_KEY` | the OmniSend API key |

**Optional:**

| Variable | Default | Notes |
|---|---|---|
| `OMNISEND_VERSION` | `2026-03-15` | OmniSend API version header |
| `RENDER_ACTIVE_WITHIN_DAYS` | `180` | recency cutoff (6 months). `0` = tag full base |
| `WRITE_DISTINCT_ID_ON_INSTALL` | `false` | flip to `true` only once Render id == PostHog distinct_id is confirmed |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_BRIDGE` | — | failure / anomaly alerts |
| `MAX_DIFF_FRACTION` | `0.30` | guardrail: abort if a run would change > this share of the audience |

> The confirmed Render schema (table `user`, the `user_role='user' AND deleted_at IS NULL`
> customer filter, `created_at`/`last_seen_at` columns, the reading-stage tables) is baked
> into the code defaults, so a bare deploy with just the three required vars is correct and
> **cannot** accidentally tag advisors or the full dormant base. Override any of them via the
> matching `RENDER_*` env var only if the schema changes (see `.env.example`).

## 3. Cron schedule (Railway dashboard)
Service → **Settings → Cron Schedule** → `*/15 * * * *` (every 15 minutes).
Railway runs the start command on that schedule; the process exits when the run finishes.

## Two-gate write safety
A live write needs BOTH `DRY_RUN=false` (env) AND the `--live` flag (baked into the start
command). If `DRY_RUN` is unset/true, the cron run logs "Refusing --live…" and exits without
writing — so a misconfigured deploy fails safe.

## Notes
- The initial ~6,300-contact backfill was run manually; ongoing cron runs write only what
  actually changed (near-zero), so they're fast and won't hit the write rate-limit the
  one-time backfill did.
- Render access is read-only (enforced at the connection). The service only reads Render and
  writes tags/custom-properties to OmniSend — it never creates OmniSend contacts.
