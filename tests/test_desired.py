from __future__ import annotations

from bridge import config
from bridge.desired import (
    combine,
    interaction_desired,
    membership_desired,
)
from bridge.identity import IdentityBridge
from bridge.models import InteractionAggregate


def test_membership_desired_tags_and_props(monkeypatch, roster):
    monkeypatch.setenv("WRITE_DISTINCT_ID_ON_INSTALL", "true")  # hermetic; don't rely on .env
    desired = membership_desired(roster)
    # u3 has no email and is dropped from the desired (email-keyed) map.
    assert set(desired.keys()) == {"alice@example.com", "bob@example.com", "carol@example.com"}

    alice = desired["alice@example.com"]
    assert config.TAG_INSTALL in alice.tags
    assert config.TAG_SIGNUP in alice.tags               # has signed_up_at
    assert alice.props[config.PROP_RENDER_USER_ID] == "u1"  # identity anchor, always
    assert alice.props[config.PROP_INSTALLED_AT] == "2026-01-01"
    assert alice.props[config.PROP_SIGNED_UP_AT] == "2026-01-02"
    assert alice.props[config.PROP_DISTINCT_ID] == "u1"  # write_distinct_id_on_install default

    bob = desired["bob@example.com"]
    assert config.TAG_INSTALL in bob.tags
    assert config.TAG_SIGNUP not in bob.tags             # no signup fact
    assert config.PROP_SIGNED_UP_AT not in bob.props


def test_membership_feeds_last_seen_from_render():
    from bridge.models import RenderUser

    desired = membership_desired([
        RenderUser("u1", "a@x.com", installed_at="2026-01-01", last_seen_at="2026-07-01"),
    ])
    assert desired["a@x.com"].props[config.PROP_LAST_SEEN] == "2026-07-01"


def test_write_distinct_id_off_but_render_id_still_written(monkeypatch, roster):
    monkeypatch.setenv("WRITE_DISTINCT_ID_ON_INSTALL", "false")
    desired = membership_desired(roster)
    alice = desired["alice@example.com"]
    assert config.PROP_DISTINCT_ID not in alice.props       # gated off
    assert alice.props[config.PROP_RENDER_USER_ID] == "u1"  # anchor unaffected by the gate


def test_legacy_dual_write(monkeypatch, roster):
    monkeypatch.setenv("LEGACY_DUAL_WRITE_TAGS", "app_install, installed")
    desired = membership_desired(roster)
    tags = desired["alice@example.com"].tags
    assert "app_install" in tags and "installed" in tags
    assert config.TAG_INSTALL in tags  # new spelling still written


def test_interaction_desired_join_and_counts(monkeypatch, roster):
    monkeypatch.setenv("WRITE_DISTINCT_ID_ON_INSTALL", "true")  # exercise the distinct-id path
    identity = IdentityBridge(roster)
    aggregates = [
        InteractionAggregate("u1", chat_count=4, draw_count=1, reading_count=2,
                             interaction_count=7, first_chat_at="2026-01-05",
                             last_chat_at="2026-01-20", last_seen_at="2026-01-21"),
        InteractionAggregate("u2", chat_count=0, draw_count=0, interaction_count=2,
                             last_seen_at="2026-02-10"),
        InteractionAggregate("ghost", chat_count=5, interaction_count=20),  # unresolvable id
    ]
    desired, resolved, unresolved = interaction_desired(aggregates, identity)
    assert resolved == 2 and unresolved == 1

    alice = desired["alice@example.com"]
    assert config.TAG_CHAT in alice.tags
    assert config.TAG_DAILY_DRAW in alice.tags           # drew >=1
    assert config.TAG_READING in alice.tags              # has a reading
    # milestone ladders: 4 chats -> _1/_2/_3 (capped at 3); 1 draw -> _1 only.
    assert {"app: chat_1", "app: chat_2", "app: chat_3"} <= alice.tags
    assert "app: daily_draw_1" in alice.tags
    assert "app: daily_draw_2" not in alice.tags         # only drew once
    assert "app: interaction_11" not in alice.tags       # 7 interactions < 11
    assert alice.props[config.PROP_CHATS] == 4
    assert alice.props[config.PROP_DAILY_DRAWS] == 1
    assert alice.props[config.PROP_INTERACTIONS] == 7
    assert alice.props[config.PROP_LAST_ACTIVE] == "2026-01-21"
    assert alice.props[config.PROP_LAST_SEEN] == "2026-01-21"   # mirrored for the engine
    assert alice.props[config.PROP_RENDER_USER_ID] == "u1"

    bob = desired["bob@example.com"]
    assert config.TAG_CHAT not in bob.tags               # never chatted
    assert config.TAG_DAILY_DRAW not in bob.tags         # never drew
    assert bob.props[config.PROP_INTERACTIONS] == 2      # raw count only; below every milestone


def test_milestone_ladders_cap_and_interaction_threshold(roster):
    identity = IdentityBridge(roster)
    # u1: 5 chats, 3 draws, 14 interactions -> full chat+draw ladders + interaction_11.
    desired, _, _ = interaction_desired(
        [InteractionAggregate("u1", chat_count=5, draw_count=3, interaction_count=14)],
        identity,
    )
    tags = desired["alice@example.com"].tags
    assert {"app: chat_1", "app: chat_2", "app: chat_3"} <= tags
    assert "app: chat_4" not in tags and "app: chat_5" not in tags  # capped at 3
    assert {"app: daily_draw_1", "app: daily_draw_2"} <= tags       # capped at 2
    assert "app: interaction_11" in tags                            # 14 >= 11


def test_milestones_env_tunable(monkeypatch, roster):
    monkeypatch.setenv("APP_CHAT_MILESTONES", "5,10")
    identity = IdentityBridge(roster)
    desired, _, _ = interaction_desired(
        [InteractionAggregate("u1", chat_count=6, interaction_count=6)], identity,
    )
    tags = desired["alice@example.com"].tags
    assert "app: chat_5" in tags and "app: chat_10" not in tags     # 6 clears 5 not 10
    assert "app: chat_1" not in tags                                # 1 not a milestone now


def test_combine_unions_tags_and_merges_props(roster):
    identity = IdentityBridge(roster)
    membership = membership_desired(roster)
    interaction, _, _ = interaction_desired(
        [InteractionAggregate("u1", chat_count=3, interaction_count=3, last_seen_at="2026-01-21")],
        identity,
    )
    merged = combine(membership, interaction)
    alice = merged["alice@example.com"]
    assert config.TAG_INSTALL in alice.tags and config.TAG_CHAT in alice.tags
    assert alice.props[config.PROP_INSTALLED_AT] == "2026-01-01"
    assert alice.props[config.PROP_LAST_SEEN] == "2026-01-21"
