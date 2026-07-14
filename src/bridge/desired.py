"""Desired-state computation — what the bridge WANTS true on each OmniSend contact.

Field names follow TAG_TAXONOMY.md (the cross-service naming agreement). Two halves,
deliberately independent so the membership sync (the quick win) can ship on its own:

- :func:`membership_desired` — from the Render roster: ``render_user_id`` (identity
  anchor), ``app: installed`` / ``app: signup`` tags, install/signup + ``app_last_active``
  (mirrored into ``posthog_last_seen`` for engine compat), and — only once confirmed —
  ``posthog_distinct_id``.
- :func:`interaction_desired` — from the interaction aggregates (Render-native by
  default, PostHog optional), joined to email via the identity bridge: ``app: chat`` /
  ``app: daily_draw`` / ``app: reading`` tags, the ``app_chats`` / ``app_daily_draws`` /
  ``app_interactions`` count props, and recency. Engagement *tiering* is left to the
  engine — the bridge supplies the raw counts only (TAG_TAXONOMY.md §D).

Both return ``{normalized_email -> DesiredContact}``. :func:`combine` merges them.
Pure functions of their inputs — no I/O, so they're trivially unit-tested.
"""

from __future__ import annotations

from typing import Iterable

from bridge import config
from bridge.identity import IdentityBridge, normalize_email, normalize_user_id
from bridge.models import DesiredContact, InteractionAggregate, RenderUser


def _set_recency(d: DesiredContact, when: str | None) -> None:
    """Set both the canonical ``app_last_active`` and the engine-read ``posthog_last_seen``
    from one activity timestamp (last-write-wins across sources via combine)."""
    if when:
        d.props[config.PROP_LAST_ACTIVE] = when
        d.props[config.PROP_LAST_SEEN] = when


def _milestone_tags(d: DesiredContact, base: str, count: int, milestones) -> None:
    """Add the cumulative ``<base>_<N>`` tag for every milestone N the count reaches.

    OmniSend segments filter on tags, not numeric props, so a count threshold ("has
    >=3 chats") only becomes segmentable once it's a tag. Capped by the milestone list
    so the tag set stays bounded (e.g. chats -> _1/_2/_3)."""
    for n in milestones:
        if count >= n:
            d.tags.add(f"{base}_{n}")


def membership_desired(roster: Iterable[RenderUser]) -> dict[str, DesiredContact]:
    """Desired membership state from Render, keyed by normalized email.

    A user with no email can't be a join target and is skipped here (they still feed
    the identity bridge). Every rostered user gets ``render_user_id`` (the identity
    anchor) + ``app: installed``; ``app: signup`` rides on the signup fact.
    """
    legacy = config.legacy_dual_write_tags()
    write_distinct = config.write_distinct_id_on_install()
    out: dict[str, DesiredContact] = {}
    for u in roster:
        email = normalize_email(u.email)
        if not email:
            continue
        uid = normalize_user_id(u.user_id)
        d = out.get(email) or DesiredContact(email=email)
        if u.first_name and not d.first_name:
            d.first_name = u.first_name  # only used if the contact must be created
        d.tags.add(config.TAG_INSTALL)
        d.tags.update(legacy)  # dual-write legacy spellings during transition (if any)
        if uid:
            # The identity anchor — always safe to write (it IS the Render id).
            d.props[config.PROP_RENDER_USER_ID] = uid
        if u.installed_at:
            d.props[config.PROP_INSTALLED_AT] = u.installed_at
        if u.signed_up_at:
            d.tags.add(config.TAG_SIGNUP)
            d.props[config.PROP_SIGNED_UP_AT] = u.signed_up_at
        # Render tracks app recency natively — feed the engine's recency signal now,
        # without waiting on PostHog.
        _set_recency(d, u.last_seen_at)
        if write_distinct and uid:
            d.props[config.PROP_DISTINCT_ID] = uid
        out[email] = d
    return out


def interaction_desired(
    aggregates: Iterable[InteractionAggregate], identity: IdentityBridge
) -> tuple[dict[str, DesiredContact], int, int]:
    """Desired interaction state, joined to email via ``identity``.

    Returns ``(desired_by_email, resolved, unresolved)`` where ``resolved`` /
    ``unresolved`` count how many aggregate rows found (or failed to find) an email —
    the join-resolution rate that was the invisible loss in the old n8n workflow (0 for
    the Render-native source, which is keyed by the same user_id the roster carries).
    """
    out: dict[str, DesiredContact] = {}
    resolved = 0
    unresolved = 0
    for agg in aggregates:
        email = identity.email_for(agg.distinct_id)
        if not email:
            unresolved += 1
            continue
        resolved += 1
        uid = normalize_user_id(agg.distinct_id)
        d = out.get(email) or DesiredContact(email=email)
        if uid:
            d.props[config.PROP_RENDER_USER_ID] = uid
        # posthog_distinct_id is an engine adopted-signal; gated behind the same
        # confirmation flag as the membership path (adoption still rides app:* tags).
        if config.write_distinct_id_on_install() and uid:
            d.props[config.PROP_DISTINCT_ID] = uid
        _set_recency(d, agg.last_seen_at)
        # Leading indicators as MILESTONE TAGS (segmentable) + the raw count as a
        # reference property. The tags are what OmniSend segments filter on.
        if agg.interaction_count > 0:
            d.props[config.PROP_INTERACTIONS] = agg.interaction_count
            _milestone_tags(d, config.TAG_INTERACTION, agg.interaction_count,
                            config.app_interaction_milestones())
        if agg.chat_count > 0:
            d.tags.add(config.TAG_CHAT)
            d.props[config.PROP_CHATS] = agg.chat_count
            _milestone_tags(d, config.TAG_CHAT, agg.chat_count,
                            config.app_chat_milestones())
        if agg.draw_count > 0:
            d.tags.add(config.TAG_DAILY_DRAW)
            d.props[config.PROP_DAILY_DRAWS] = agg.draw_count
            _milestone_tags(d, config.TAG_DAILY_DRAW, agg.draw_count,
                            config.app_draw_milestones())
        if agg.reading_count > 0:
            d.tags.add(config.TAG_READING)
        out[email] = d
    return out, resolved, unresolved


def reading_stages_desired(stage_map: dict[str, set[str]]) -> dict[str, DesiredContact]:
    """Desired reading-stage tags from ``render_db.fetch_reading_stages()`` output —
    ``{email -> {tags}}`` straight to DesiredContacts. Tags only (no props); these are
    a fulfillment-funnel concern independent of the app-usage lifecycle."""
    out: dict[str, DesiredContact] = {}
    for email, tags in stage_map.items():
        e = normalize_email(email)
        if not e or not tags:
            continue
        out[e] = DesiredContact(email=e, tags=set(tags))
    return out


def combine(*parts: dict[str, DesiredContact]) -> dict[str, DesiredContact]:
    """Merge desired-state maps by email (union tags, last-write-wins props)."""
    out: dict[str, DesiredContact] = {}
    for part in parts:
        for email, d in part.items():
            existing = out.get(email)
            if existing is None:
                out[email] = DesiredContact(
                    email=email, tags=set(d.tags), props=dict(d.props),
                    first_name=d.first_name)
            else:
                existing.merge(d)
    return out
