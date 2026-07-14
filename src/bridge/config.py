"""Configuration + the single write predicate for the tagging bridge.

Mirrors the mystica-engine ``config.py`` idioms: every value comes from the
environment through an accessor (so tests flip them with ``monkeypatch.setenv``),
``DRY_RUN`` defaults TRUE, and ``external_writes_enabled()`` is the ONE predicate
the OmniSend write client checks before any mutation.

No secrets live here. ``.env.example`` documents every name.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # dotenv is a hard dep, but keep import defensive for bare test envs.
    from dotenv import load_dotenv

    REPO_ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(REPO_ROOT / ".env")
except Exception:  # pragma: no cover - only trips if dotenv is truly absent
    REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised by :func:`validate` when configuration is unsafe for the requested run."""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def get(name: str, default: str = "") -> str:
    """Return a stripped environment value (or ``default`` if unset)."""
    return os.environ.get(name, default).strip()


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean env var; unknown values fall back to the SAFE ``default`` so a
    typo can never silently disable a safety flag."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = get(name)
    if not raw:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _int_csv(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Parse a comma-separated list of ints (e.g. milestone thresholds). Falls back
    to ``default`` on an unset/garbage value; sorted + de-duplicated."""
    raw = get(name)
    if not raw:
        return default
    vals = sorted({int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()})
    return tuple(vals) or default


# ------------------------------------------------------------------ secrets/access
def render_database_url() -> str:
    return get("RENDER_DATABASE_URL")


def posthog_api_key() -> str:
    return get("POSTHOG_API_KEY")


def posthog_project() -> str:
    return get("POSTHOG_PROJECT") or "67576"


def posthog_host() -> str:
    return get("POSTHOG_HOST") or "https://us.posthog.com"


def omnisend_api_key() -> str:
    return get("OMNISEND_API_KEY")


def omnisend_version() -> str:
    """The required ``Omnisend-Version`` request header (date-based API versioning).
    Env-overridable so a future API version bump is a config change, not a code edit."""
    return get("OMNISEND_VERSION") or "2026-03-15"


# ---- Contact creation (--create-missing) --------------------------------------
# Only used when the operator opts in with --create-missing: in-scope app users whose
# email is NOT already an OmniSend contact get CREATED (then tagged in the same call).
_CREATE_STATUS = {"subscribed": "subscribed", "unsubscribed": "unsubscribed",
                  "nonsubscribed": "nonSubscribed", "non_subscribed": "nonSubscribed"}


def create_missing_status() -> str:
    """Email channel status for CREATED contacts. Default ``subscribed`` (Tom: app /
    email signup carries the opt-in). Set ``CREATE_MISSING_STATUS=nonSubscribed`` to
    create-and-tag without emailing until they opt in."""
    return _CREATE_STATUS.get(get("CREATE_MISSING_STATUS").lower(), "subscribed")


# Source tag + consent source recorded on created contacts (OmniSend strongly advises a
# source tag; consent source is stored for compliance since we assert the subscription).
TAG_SOURCE_APP = "source: app"
CREATE_CONSENT_SOURCE = "Mystica app / email signup (Render)"


def slack_bot_token() -> str:
    return get("SLACK_BOT_TOKEN")


def slack_channel() -> str:
    return get("SLACK_CHANNEL_BRIDGE") or "#mystica-bridge"


# --------------------------------------------------------------- Render schema map
# Table/column names are env-overridable so Phase 0 schema discovery can point the
# bridge at the real Render schema WITHOUT a code change (see .env.example).
def render_users_table() -> str:
    return get("RENDER_USERS_TABLE") or "users"


def render_user_id_col() -> str:
    return get("RENDER_USER_ID_COL") or "id"


def render_email_col() -> str:
    return get("RENDER_EMAIL_COL") or "email"


def render_installed_at_col() -> str:
    return get("RENDER_INSTALLED_AT_COL") or "created_at"


def render_name_col() -> str:
    """Display-name column on the users table (first token → OmniSend firstName when
    creating a missing contact, so new contacts keep personalization)."""
    return get("RENDER_NAME_COL") or "name"


def render_signed_up_at_col() -> str:
    # Confirmed schema: onboarding_completed_at is unused, so created_at anchors signup.
    return get("RENDER_SIGNED_UP_AT_COL") or "created_at"


def render_last_seen_at_col() -> str:
    """Optional column for app-recency (Render tracks ``last_seen_at`` natively). When
    set, the membership sync feeds ``posthog_last_seen`` from it — giving the engine a
    recency signal without waiting on PostHog. Empty = don't pull it. Default is the
    confirmed production column so a bare deploy still feeds recency."""
    return get("RENDER_LAST_SEEN_COL") or "last_seen_at"


def render_login_within_days() -> int:
    """Window for the ``app: login`` tag: a rostered user gets it when their last-seen
    is within this many days (a "currently active / recently logged in" signal, distinct
    from ``app: installed`` which every account has). Default 30. Set ``=0`` to disable
    the login tag. Independent of ``render_active_within_days`` (the roster cutoff)."""
    raw = get("RENDER_LOGIN_WITHIN_DAYS")
    try:
        return max(0, int(raw)) if raw else 30
    except ValueError:
        return 30


def render_roster_where() -> str:
    """A trusted SQL WHERE fragment appended to the roster query (NOT user input —
    it's our own config). Scopes the roster to real customers and excludes advisors /
    deleted rows. Defaults to the confirmed production filter (a SAFETY default: a bare
    deploy must NOT tag psychics/admins). Override via ``RENDER_ROSTER_WHERE``."""
    return get("RENDER_ROSTER_WHERE") or "user_role = 'user' AND deleted_at IS NULL"


def render_active_within_days() -> int:
    """Recency cutoff: only tag app users whose last-seen is within this many days
    (pure recency — applied to BOTH the roster and the interaction customer scope, so
    a past-window user is excluded everywhere). Defaults to 180 (6 months) as a SAFETY
    default so a bare deploy can't tag the full dormant base. Set ``=0`` to disable."""
    raw = get("RENDER_ACTIVE_WITHIN_DAYS")
    try:
        return max(0, int(raw)) if raw else 180
    except ValueError:
        return 180


# --------------------------------------------- interaction source + Render schema
def interaction_source() -> str:
    """Where the interaction signals (chats, daily draws) come from: ``render``
    (default — keyed by user_id, joined to email via the roster, NO identity-join
    loss) or ``posthog``. Render is preferred because it dodges the email-less-event
    loss that made the old n8n workflow lossy. PostHog stays available as a supplement."""
    return "posthog" if get("INTERACTION_SOURCE").lower() == "posthog" else "render"


# Render interaction schema — CONFIRMED via Phase 0 discovery (2026-07-13):
# chat_message(user_id, chat_session_id, created_at) + daily_card_draw(user_id, drawn_at).
def render_chat_table() -> str:
    return get("RENDER_CHAT_TABLE") or "chat_message"


def render_chat_user_col() -> str:
    return get("RENDER_CHAT_USER_COL") or "user_id"


def render_chat_session_col() -> str:
    return get("RENDER_CHAT_SESSION_COL") or "chat_session_id"


def render_chat_time_col() -> str:
    return get("RENDER_CHAT_TIME_COL") or "created_at"


def render_draw_table() -> str:
    return get("RENDER_DRAW_TABLE") or "daily_card_draw"


def render_draw_user_col() -> str:
    return get("RENDER_DRAW_USER_COL") or "user_id"


def render_draw_time_col() -> str:
    return get("RENDER_DRAW_TIME_COL") or "drawn_at"


def render_reading_table() -> str:
    return get("RENDER_READING_TABLE") or "reading"


def render_reading_user_col() -> str:
    return get("RENDER_READING_USER_COL") or "user_id"


def render_reading_time_col() -> str:
    return get("RENDER_READING_TIME_COL") or "created_at"


# ---- Soulmate reading fulfillment stages (NOT the app-usage lifecycle) ---------
# Sourced from soulmate_reading_intent (status) + the reading table (delivered_at),
# keyed by email — NOT recency-gated (a paid-but-abandoned buyer deserves the nudge
# regardless of app-login recency). Tags:
#   app: reading_abandoned = paid, form not filled (status=pending_inputs) -> no reading.
#   app: reading_delivered = the reading was generated & delivered (Tom filters out
#                            app: chat to get "got the reading, hasn't chatted").
TAG_READING_ABANDONED = "app: reading_abandoned"
TAG_READING_DELIVERED = "app: reading_delivered"


def render_reading_intent_table() -> str:
    return get("RENDER_READING_INTENT_TABLE") or "soulmate_reading_intent"


def render_reading_intent_email_col() -> str:
    return get("RENDER_READING_INTENT_EMAIL_COL") or "email"


def render_reading_intent_status_col() -> str:
    return get("RENDER_READING_INTENT_STATUS_COL") or "status"


def reading_abandoned_status() -> str:
    """The soulmate_reading_intent.status value meaning 'paid but form incomplete'."""
    return get("READING_ABANDONED_STATUS") or "pending_inputs"


def render_reading_delivered_col() -> str:
    return get("RENDER_READING_DELIVERED_COL") or "delivered_at"


# --------------------------------------------------------------------- mode flags
def is_dry_run() -> bool:
    """The master safety switch. Default TRUE — no external write path may run."""
    return _bool_env("DRY_RUN", True)


def external_writes_enabled() -> bool:
    """The ONE predicate the OmniSend write client checks before a mutation.

    True only when ``DRY_RUN`` is explicitly false. The CLI adds a SECOND gate
    (the ``--live`` flag) so a stray ``DRY_RUN=false`` in the environment still
    can't write without an operator explicitly asking for it on the command line.
    """
    return not is_dry_run()


def validate(*, require_render: bool = True, require_posthog: bool = False) -> None:
    """Fail-fast configuration check. Raises :class:`ConfigError` on unsafe config.

    ``require_render`` — membership sync needs the Render URL.
    ``require_posthog`` — interaction sync additionally needs the PostHog key.
    OmniSend key is required whenever external writes are enabled.
    """
    problems: list[str] = []
    if require_render and not render_database_url():
        problems.append("RENDER_DATABASE_URL is required")
    if require_posthog and not posthog_api_key():
        problems.append("POSTHOG_API_KEY is required for interaction sync")
    if external_writes_enabled() and not omnisend_api_key():
        problems.append("OMNISEND_API_KEY is required when DRY_RUN=false")
    if problems:
        raise ConfigError("; ".join(problems))


# --------------------------------------------------------------------- constants
# API bases. NOTE: OmniSend exposes contacts read under the v5 host and the proven
# tag/property write endpoints under the /api host — this mirrors exactly what the
# live mystica-engine client does (verified against the MCP surface 2026-07-02).
OMNISEND_READ_BASE = "https://api.omnisend.com/v5"
OMNISEND_WRITE_BASE = "https://api.omnisend.com/api"

# HTTP retry policy (exponential backoff on 5xx/429), same shape as the engine.
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE_SEC = 0.5

# Batch sizes.
OMNISEND_PAGE_SIZE = 250
RENDER_FETCH_CHUNK = 5000

# ---- The tag / custom-property NAMES the bridge writes -------------------------
# The names are the CROSS-SERVICE AGREEMENT in TAG_TAXONOMY.md (Render / OmniSend /
# engine). Engine reads app-adoption from: any tag starting with "app:" OR a
# non-empty posthog_distinct_id (mystica-engine state.py:648). Existing OmniSend
# spellings are `app: installed` / `app: signup` / `app: chat` — confirm live via
# `bridge baseline` before the backfill; a mismatch would create a parallel tag.
TAG_INSTALL = "app: installed"
TAG_SIGNUP = "app: signup"
TAG_LOGIN = "app: login"  # recently-active login (last_seen within RENDER_LOGIN_WITHIN_DAYS)
TAG_CHAT = "app: chat"
TAG_DAILY_DRAW = "app: daily_draw"
TAG_READING = "app: reading"
# Milestone base for the total-interactions ladder (no bare activity tag of its own).
TAG_INTERACTION = "app: interaction"

# ---- Count MILESTONE tags (the leading indicators, as tags) --------------------
# OmniSend segments filter reliably on tags (anyOf/noneOf) but NOT on numeric custom
# properties — so each count threshold is encoded as a tag. Each activity emits a
# CUMULATIVE ladder: a user with >=N of the activity gets ``<base>_<N>`` for every
# milestone N reached, capped at the leading-indicator threshold so the tag set stays
# bounded (chats -> _1/_2/_3, draws -> _1/_2, interactions -> _11). Segment on the top
# rung for the indicator (e.g. ``app: chat_3`` = the 3-chats indicator). Env-tunable.
def app_chat_milestones() -> tuple[int, ...]:
    return _int_csv("APP_CHAT_MILESTONES", (1, 2, 3))


def app_draw_milestones() -> tuple[int, ...]:
    return _int_csv("APP_DRAW_MILESTONES", (1, 2))


def app_interaction_milestones() -> tuple[int, ...]:
    return _int_csv("APP_INTERACTION_MILESTONES", (11,))

# Legacy tag spellings the old n8n workflow wrote (e.g. "app_install"). If Phase 0
# finds OmniSend segments/automations still filtering on these, list them here to
# DUAL-WRITE during the transition. Empty by default (decided in Phase 0.7).
def legacy_dual_write_tags() -> tuple[str, ...]:
    return _csv("LEGACY_DUAL_WRITE_TAGS", ())


# Custom-property NAMES (TAG_TAXONOMY.md §A/§D/§E). render_user_id is the identity
# anchor (always safe to write — it IS the Render id). posthog_distinct_id +
# posthog_last_seen are what the engine reads today; app_last_active is the new
# canonical recency (mirrored into posthog_last_seen for engine compat). The count
# props power the leading-indicator segment filters. Property names: Latin letters /
# digits / underscore, <=128 chars.
PROP_RENDER_USER_ID = "render_user_id"
PROP_DISTINCT_ID = "posthog_distinct_id"
PROP_LAST_SEEN = "posthog_last_seen"
PROP_LAST_ACTIVE = "app_last_active"
PROP_INSTALLED_AT = "installed_at"
PROP_SIGNED_UP_AT = "signed_up_at"
PROP_CHATS = "app_chats"
PROP_DAILY_DRAWS = "app_daily_draws"
PROP_INTERACTIONS = "app_interactions"

# ---- Interaction thresholds (the high-value leading indicators) -----------------
# From project_mystica_highvalue_indicators: 3-chats / 11-interactions / 2+ daily-draws.
ENGAGED_MIN_CHATS = 3
ENGAGED_MIN_INTERACTIONS = 11
ENGAGED_MIN_DRAWS = 2


def posthog_chat_events() -> tuple[str, ...]:
    return _csv("POSTHOG_CHAT_EVENTS", ("chat_session_start",))


def posthog_draw_events() -> tuple[str, ...]:
    return _csv("POSTHOG_DRAW_EVENTS", ("card_draw",))


def posthog_activity_events() -> tuple[str, ...]:
    return _csv(
        "POSTHOG_ACTIVITY_EVENTS",
        ("app_login", "card_draw", "chat_session_start", "reading_view"),
    )


def max_diff_fraction() -> float:
    """Guardrail: abort (unless --force) if a run would change more than this
    fraction of the addressable audience. Default 0.30."""
    raw = get("MAX_DIFF_FRACTION")
    try:
        return float(raw) if raw else 0.30
    except ValueError:
        return 0.30


# Gates EVERY write of the ``posthog_distinct_id`` custom property (both the
# membership path — install-alone — and the interaction path). Off until Phase 0.4
# confirms the Render user_id IS the PostHog distinct_id: writing an unconfirmed id
# would poison future joins, and the engine's app_adopted already flips on the
# ``app:*`` tags we set regardless. When True on the membership path it also makes
# the engine treat install-alone as adopted (adopter semantics — Phase 0.7). Default
# True (code default / test default); the live ``.env`` sets it False until confirmed.
def write_distinct_id_on_install() -> bool:
    # Default False: don't assert an unconfirmed posthog_distinct_id (the app:* tags
    # carry adoption). Flip to true only once Render id == PostHog distinct_id is confirmed.
    return _bool_env("WRITE_DISTINCT_ID_ON_INSTALL", False)
