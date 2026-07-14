"""Shared fixtures — force dry-run for the whole suite so no test can ever write."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch):
    """Every test runs with writes disabled unless it explicitly flips DRY_RUN, and
    starts from CODE defaults (not the operator's real .env) for the behavioral flags
    — so a value in .env can never silently change a test's meaning."""
    monkeypatch.setenv("DRY_RUN", "true")
    # Neutralize real secrets + behavioral overrides that .env may set.
    for name in (
        "SLACK_BOT_TOKEN",
        "WRITE_DISTINCT_ID_ON_INSTALL",
        "LEGACY_DUAL_WRITE_TAGS",
        "MAX_DIFF_FRACTION",
        "RENDER_ROSTER_WHERE",
        "RENDER_LAST_SEEN_COL",
        "RENDER_ACTIVE_WITHIN_DAYS",
        "INTERACTION_SOURCE",
        "APP_CHAT_MILESTONES",
        "APP_DRAW_MILESTONES",
        "APP_INTERACTION_MILESTONES",
        "POSTHOG_CHAT_EVENTS",
        "POSTHOG_DRAW_EVENTS",
        "POSTHOG_ACTIVITY_EVENTS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def roster():
    from bridge.models import RenderUser

    return [
        RenderUser("u1", "Alice@Example.com ", installed_at="2026-01-01", signed_up_at="2026-01-02"),
        RenderUser("u2", "bob@example.com", installed_at="2026-02-01"),
        RenderUser("u3", None, installed_at="2026-03-01"),  # no email — feeds identity only via id, dropped
        RenderUser("u4", "carol@example.com", installed_at="2026-04-01", signed_up_at="2026-04-03"),
    ]
