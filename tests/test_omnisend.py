from __future__ import annotations

import json

import httpx
import pytest

from bridge import config
from bridge.omnisend import OmniSendClient, OmnisendAuthError, OmnisendError


def _client(handler, **kw):
    return OmniSendClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        **kw,
    )


def test_iter_contacts_follows_v5_next_url():
    page1 = {
        "contacts": [{"id": "1", "email": "a@x.com", "tags": ["app: install"]}],
        "paging": {"next": "https://api.omnisend.com/v5/contacts?page=2"},
    }
    page2 = {
        "contacts": [{"id": "2", "email": "b@x.com"}],
        "paging": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = dict(request.url.params).get("page")
        return httpx.Response(200, json=page2 if page == "2" else page1)

    got = list(_client(handler).iter_contacts())
    assert [c["id"] for c in got] == ["1", "2"]


def test_load_current_indexes_by_normalized_email():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "contacts": [
                {"id": "1", "email": "Alice@X.com", "tags": ["app: install"],
                 "customProperties": {"posthog_distinct_id": "u1"}},
                {"id": "2", "email": None},  # skipped
            ],
            "paging": {},
        })

    current = _client(handler).load_current()
    assert set(current) == {"alice@x.com"}
    c = current["alice@x.com"]
    assert c.contact_id == "1"
    assert "app: install" in c.tags
    assert c.props["posthog_distinct_id"] == "u1"


def test_write_is_gated_in_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)  # must never be hit for a write in dry-run
        return httpx.Response(200, json={})

    res = _client(handler).apply_change("cid", add_tags=["app: install"], set_props={"x": 1})
    assert res["tags"]["dry_run"] is True
    assert res["props"]["dry_run"] is True
    assert calls == []  # no HTTP write issued


def test_write_hits_api_when_enabled(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"contactID": "cid"})

    _client(handler).apply_change("cid", add_tags=["app: install"], set_props={"x": 1})
    methods = {m for m, _ in seen}
    assert "POST" in methods and "PATCH" in methods


def test_add_tags_payload_and_version_header(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        seen["version"] = request.headers.get("Omnisend-Version")
        return httpx.Response(200, json={})

    _client(handler).add_tags(["c1", "c2"], ["app: installed"])
    assert seen["body"] == {"contactIDs": ["c1", "c2"], "tags": ["app: installed"]}
    assert seen["version"]  # the required version header is present


def test_auth_error_raises(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(OmnisendAuthError):
        _client(handler).add_tags(["cid"], ["app: install"])


def test_retry_then_success(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"contactID": "cid"})

    _client(handler).add_tags(["cid"], ["app: install"])
    assert state["n"] == 3  # two retries then success


def test_get_contact_by_email_returns_first_or_none():
    def handler(request: httpx.Request) -> httpx.Response:
        email = dict(request.url.params).get("email")
        if email == "found@x.com":
            return httpx.Response(200, json={"contacts": [{"id": "1", "email": email}]})
        return httpx.Response(200, json={"contacts": []})

    client = _client(handler)
    assert client.get_contact_by_email("Found@X.com")["id"] == "1"
    assert client.get_contact_by_email("missing@x.com") is None
