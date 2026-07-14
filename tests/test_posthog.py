from __future__ import annotations

import httpx
import pytest

from bridge.posthog import PostHogClient, PostHogError


def _client(handler):
    return PostHogClient(
        api_key="phx-test",
        project="67576",
        transport=httpx.MockTransport(handler),
    )


def test_unconfigured_returns_empty():
    client = PostHogClient(api_key="", project="67576")
    assert client.configured is False
    assert client.aggregate() == []


def test_aggregate_parses_positional_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/67576/query"
        body = request.read().decode()
        assert "HogQLQuery" in body and "GROUP BY distinct_id" in body
        return httpx.Response(200, json={
            "results": [
                ["u1", 4, 1, 12, "2026-01-05", "2026-01-20", "2026-01-21"],
                ["u2", 0, 0, 2, None, None, "2026-02-10"],
                [None, 0, 0, 0, None, None, None],  # dropped (no distinct_id)
            ],
        })

    aggs = _client(handler).aggregate()
    assert len(aggs) == 2
    assert aggs[0].distinct_id == "u1"
    assert aggs[0].chat_count == 4
    assert aggs[0].last_seen_at == "2026-01-21"
    assert aggs[1].chat_count == 0


def test_http_error_raises_posthog_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    with pytest.raises(PostHogError):
        _client(handler).aggregate()


def test_since_days_adds_interval():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "INTERVAL 30 DAY" in body
        return httpx.Response(200, json={"results": []})

    _client(handler).aggregate(since_days=30)
