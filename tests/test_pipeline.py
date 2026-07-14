from __future__ import annotations

import httpx

from bridge import config, pipeline
from bridge.models import RenderUser
from bridge.omnisend import OmniSendClient
from bridge.posthog import PostHogClient


class FakeRender:
    def __init__(self, users, aggregates=None, reading_stages=None):
        self._users = users
        self._aggregates = aggregates or []
        self._reading = reading_stages or {}

    def fetch_roster(self):
        return list(self._users)

    def fetch_interaction_aggregates(self):
        return list(self._aggregates)

    def fetch_reading_stages(self):
        return dict(self._reading)


def _omnisend(contacts):
    """An OmniSendClient whose read returns ``contacts`` and whose writes are gated."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"contacts": contacts, "paging": {}})
        return httpx.Response(200, json={})

    return OmniSendClient(api_key="k", transport=httpx.MockTransport(handler),
                          sleep=lambda _: None)


def test_membership_dry_run_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    render = FakeRender([
        RenderUser("u1", "alice@example.com", installed_at="2026-01-01", signed_up_at="2026-01-02"),
        RenderUser("u2", "bob@example.com", installed_at="2026-02-01"),
    ])
    # OmniSend has both contacts, but neither is tagged yet -> both should be in the plan.
    omnisend = _omnisend([
        {"id": "c1", "email": "alice@example.com", "tags": [], "customProperties": {}},
        {"id": "c2", "email": "bob@example.com", "tags": [], "customProperties": {}},
    ])

    report, plan = pipeline.run_membership(
        live=False, render=render, omnisend=omnisend, write_findings=False,
    )
    assert report.mode == "dry_run"
    assert report.render_users == 2
    assert plan.touched == 2
    # Alice gets install+signup, Bob gets install only.
    by_email = {c.email: c for c in plan.changes}
    assert config.TAG_SIGNUP in by_email["alice@example.com"].add_tags
    assert config.TAG_SIGNUP not in by_email["bob@example.com"].add_tags


def test_full_run_render_interaction_default(monkeypatch):
    """Default source = render: interaction aggregates come from Render (lossless join)."""
    monkeypatch.setenv("DRY_RUN", "true")
    from bridge.models import InteractionAggregate

    render = FakeRender(
        [RenderUser("u1", "alice@example.com", installed_at="2026-01-01")],
        aggregates=[InteractionAggregate("u1", chat_count=4, draw_count=2, reading_count=1,
                                         interaction_count=7, first_chat_at="2026-01-05",
                                         last_chat_at="2026-01-20", last_seen_at="2026-01-21")],
    )
    omnisend = _omnisend([
        {"id": "c1", "email": "alice@example.com", "tags": [], "customProperties": {}},
    ])

    report, plan = pipeline.run_all(
        live=False, render=render, omnisend=omnisend, write_findings=False,
    )
    assert any("interaction source: render" in n for n in report.notes)
    assert report.posthog_rows == 1  # (row count, whatever the source)
    assert report.resolved_ids == 1 and report.unresolved_ids == 0
    change = plan.changes[0]
    assert config.TAG_INSTALL in change.add_tags
    assert config.TAG_CHAT in change.add_tags
    assert config.TAG_DAILY_DRAW in change.add_tags
    assert config.TAG_READING in change.add_tags
    # milestone ladders present in the actual write plan (4 chats, 2 draws)
    assert "app: chat_3" in change.add_tags and "app: daily_draw_2" in change.add_tags
    assert change.set_props[config.PROP_CHATS] == 4
    assert change.set_props[config.PROP_INTERACTIONS] == 7


def test_full_run_includes_reading_stage_tags(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    render = FakeRender(
        [RenderUser("u1", "alice@example.com", installed_at="2026-01-01")],
        reading_stages={"alice@example.com": {config.TAG_READING_DELIVERED},
                        "abandoner@example.com": {config.TAG_READING_ABANDONED}},
    )
    omnisend = _omnisend([
        {"id": "c1", "email": "alice@example.com", "tags": [], "customProperties": {}},
        {"id": "c2", "email": "abandoner@example.com", "tags": [], "customProperties": {}},
    ])
    report, plan = pipeline.run_all(
        live=False, render=render, omnisend=omnisend, write_findings=False,
    )
    by_email = {c.email: c for c in plan.changes}
    # alice: app-usage tags + reading_delivered; abandoner: only reading_abandoned.
    assert config.TAG_READING_DELIVERED in by_email["alice@example.com"].add_tags
    assert config.TAG_INSTALL in by_email["alice@example.com"].add_tags
    assert by_email["abandoner@example.com"].add_tags == (config.TAG_READING_ABANDONED,)


def test_full_run_posthog_source_when_selected(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("INTERACTION_SOURCE", "posthog")
    render = FakeRender([RenderUser("u1", "alice@example.com", installed_at="2026-01-01")])
    omnisend = _omnisend([
        {"id": "c1", "email": "alice@example.com", "tags": [], "customProperties": {}},
    ])

    def ph_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "results": [["u1", 4, 2, 15, "2026-01-05", "2026-01-20", "2026-01-21"]],
        })

    posthog = PostHogClient(api_key="phx", project="67576",
                            transport=httpx.MockTransport(ph_handler))
    report, plan = pipeline.run_all(
        live=False, render=render, posthog=posthog, omnisend=omnisend, write_findings=False,
    )
    assert any("interaction source: posthog" in n for n in report.notes)
    assert config.TAG_CHAT in plan.changes[0].add_tags


def test_interaction_degrades_when_source_errors(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("INTERACTION_SOURCE", "posthog")
    render = FakeRender([RenderUser("u1", "alice@example.com")])
    omnisend = _omnisend([{"id": "c1", "email": "alice@example.com", "tags": [], "customProperties": {}}])

    def ph_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    posthog = PostHogClient(api_key="phx", project="67576",
                            transport=httpx.MockTransport(ph_handler))

    report, plan = pipeline.run_interaction(
        live=False, render=render, posthog=posthog, omnisend=omnisend, write_findings=False,
    )
    assert any("DEGRADED" in n for n in report.notes)
    assert plan.touched == 0  # no interaction data -> nothing to write (safe)
