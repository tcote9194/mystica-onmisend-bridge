"""Dataclasses shared across the bridge — the shapes the pipeline passes around.

Deliberately plain: sources produce these, ``desired`` computes the target set,
``plan`` diffs, ``apply`` executes. No behavior beyond small helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ------------------------------------------------------------------ source rows
@dataclass(frozen=True)
class RenderUser:
    """One app user from Render (the source of truth for membership + identity)."""

    user_id: str
    email: Optional[str]
    installed_at: Optional[str] = None   # raw ISO string as returned by Postgres
    signed_up_at: Optional[str] = None
    last_seen_at: Optional[str] = None   # Render tracks app recency natively (bonus)
    name: Optional[str] = None           # display name (→ firstName on contact creation)
    login_active: bool = False           # last_seen within the login window → app: login

    @property
    def has_email(self) -> bool:
        return bool(self.email)

    @property
    def first_name(self) -> Optional[str]:
        """First whitespace token of the display name (for OmniSend firstName)."""
        if not self.name:
            return None
        token = str(self.name).strip().split()
        return token[0] if token else None


@dataclass(frozen=True)
class InteractionAggregate:
    """Per-user interaction rollup — the shared shape produced by BOTH the Render-
    native source (chats/draws/readings keyed by user_id) and the optional PostHog
    source. ``distinct_id`` is the join key (Render user_id or PostHog distinct_id)."""

    distinct_id: str
    chat_count: int = 0
    draw_count: int = 0
    reading_count: int = 0
    interaction_count: int = 0
    first_chat_at: Optional[str] = None
    last_chat_at: Optional[str] = None
    last_seen_at: Optional[str] = None


# Back-compat alias (the shape predates the Render source; kept so nothing breaks).
PostHogAggregate = InteractionAggregate


# ----------------------------------------------------------------- desired state
@dataclass
class DesiredContact:
    """What the bridge WANTS true on an OmniSend contact, keyed by (lowercased) email.

    ``tags`` is add-only (the bridge never removes tags — install/signup/chat are
    monotonic facts, and removal risks touching foreign namespaces). ``props`` are
    set-to-value (last write wins). ``first_name`` is only used when the contact must
    be CREATED (--create-missing); it's ignored for existing contacts.
    """

    email: str
    tags: set[str] = field(default_factory=set)
    props: dict[str, Any] = field(default_factory=dict)
    first_name: Optional[str] = None

    def merge(self, other: "DesiredContact") -> None:
        self.tags |= other.tags
        self.props.update({k: v for k, v in other.props.items() if v is not None})
        if other.first_name and not self.first_name:
            self.first_name = other.first_name


@dataclass(frozen=True)
class CurrentContact:
    """The live OmniSend state we diff against."""

    contact_id: str
    email: str
    tags: frozenset[str]
    props: dict[str, Any]


# ----------------------------------------------------------------- the change plan
@dataclass(frozen=True)
class ContactChange:
    """One contact's delta: tags to add + props to set. Never a removal."""

    email: str
    contact_id: str
    add_tags: tuple[str, ...]
    set_props: dict[str, Any]

    @property
    def is_empty(self) -> bool:
        return not self.add_tags and not self.set_props


@dataclass(frozen=True)
class CreateContact:
    """A NEW OmniSend contact to create (--create-missing) — a desired email that has
    no existing contact. Carries everything to create + tag it in one call."""

    email: str
    tags: tuple[str, ...]
    props: dict[str, Any]
    first_name: Optional[str] = None


@dataclass
class ChangePlan:
    """The full diff for a run + the numbers the report and guardrail need."""

    changes: list[ContactChange] = field(default_factory=list)
    creates: list[CreateContact] = field(default_factory=list)
    # Bookkeeping (populated by plan.build_plan).
    desired_count: int = 0
    matched_count: int = 0          # desired emails that exist as OmniSend contacts
    unresolved_emails: int = 0      # desired emails with NO OmniSend contact (and not created)
    audience_size: int = 0          # denominator for the guardrail fraction

    @property
    def touched(self) -> int:
        """Existing contacts changed + new contacts created (the run's blast radius)."""
        return len(self.changes) + len(self.creates)

    @property
    def diff_fraction(self) -> float:
        if not self.audience_size:
            return 0.0
        return self.touched / self.audience_size


@dataclass
class RunReport:
    """End-of-run summary (logged, Slack-posted, and appended to FINDINGS.md)."""

    phase: str
    mode: str                       # "dry_run" | "live"
    render_users: int = 0
    render_with_email: int = 0
    posthog_rows: int = 0
    resolved_ids: int = 0
    unresolved_ids: int = 0
    omnisend_contacts: int = 0
    plan_touched: int = 0
    plan_creates: int = 0
    applied: int = 0
    created: int = 0
    errors: int = 0
    diff_fraction: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "mode": self.mode,
            "render_users": self.render_users,
            "render_with_email": self.render_with_email,
            "posthog_rows": self.posthog_rows,
            "resolved_ids": self.resolved_ids,
            "unresolved_ids": self.unresolved_ids,
            "omnisend_contacts": self.omnisend_contacts,
            "plan_touched": self.plan_touched,
            "plan_creates": self.plan_creates,
            "applied": self.applied,
            "created": self.created,
            "errors": self.errors,
            "diff_fraction": round(self.diff_fraction, 4),
            "notes": self.notes,
        }
