"""The identity bridge — the whole trick of this service.

PostHog events are keyed by ``distinct_id``/``user_id`` and usually carry no email;
OmniSend is keyed by email. This module turns a Render roster into the
``user_id -> email`` map that lets an email-less PostHog row find its OmniSend
contact, and normalizes emails so both sides of every join agree.

Pure + side-effect-free: construct from an already-fetched roster (the roster is
small enough to pull whole once per run — see render_db.fetch_roster).
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, Optional

from bridge.models import RenderUser


def normalize_email(value: object) -> str:
    """Lowercase + trim + drop invisible Unicode — the canonical OmniSend join key.

    Matches the engine's Clarity source idiom (``str(...).strip().lower()``) so the
    two services key contacts identically, and additionally strips Unicode control
    (Cc) and format (Cf) characters — bidi isolates (e.g. ``\\u2069``), zero-width
    spaces, BOM — that app-collected emails sometimes carry. Such an invisible char
    makes the *same* address fail to match between Render and OmniSend, so the contact
    looks perpetually "uncreated" and re-churns on every cron run. Returns "" for falsy
    input. NOTE: keep this in lockstep with any normalization the engine applies.
    """
    if value in (None, ""):
        return ""
    s = str(value).strip().lower()
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in s):
        s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    return s


def normalize_user_id(value: object) -> str:
    """Canonical form for a Render user_id / PostHog distinct_id (trim only — ids are
    case-sensitive opaque tokens; we do NOT lowercase them)."""
    if value in (None, ""):
        return ""
    return str(value).strip()


class IdentityBridge:
    """A per-run, in-memory ``user_id -> email`` resolver built from the roster.

    Built once from ``fetch_roster()`` output; ``resolve_emails`` then answers the
    PostHog join without further DB calls. Unknown ids resolve to nothing (never
    raise) and are counted by the caller as the join-loss metric.
    """

    def __init__(self, users: Iterable[RenderUser]) -> None:
        self._by_id: dict[str, str] = {}
        for u in users:
            uid = normalize_user_id(u.user_id)
            email = normalize_email(u.email)
            if uid and email:
                # First non-empty email wins; a later blank never overwrites a good one.
                self._by_id.setdefault(uid, email)

    def __len__(self) -> int:
        return len(self._by_id)

    def email_for(self, user_id: object) -> Optional[str]:
        """The normalized email for one ``user_id`` (``None`` if unknown/emailless)."""
        return self._by_id.get(normalize_user_id(user_id))

    def resolve_emails(self, user_ids: Iterable[object]) -> dict[str, str]:
        """``{user_id -> email}`` for the ids we can resolve. Unknown ids are omitted
        (so ``len(result)`` vs ``len(input)`` is the resolution rate the report logs)."""
        out: dict[str, str] = {}
        for uid in user_ids:
            norm = normalize_user_id(uid)
            email = self._by_id.get(norm)
            if email:
                out[norm] = email
        return out

    @property
    def known_emails(self) -> frozenset[str]:
        return frozenset(self._by_id.values())
