from __future__ import annotations

import httpx
import pytest

from bridge import config
from bridge.apply import GuardrailTripped, apply_plan
from bridge.models import ChangePlan, ContactChange
from bridge.omnisend import OmniSendClient, OmnisendAuthError


def _plan(n_changes, audience_size):
    changes = [
        ContactChange(email=f"{i}@x.com", contact_id=f"c{i}",
                      add_tags=(config.TAG_INSTALL,), set_props={})
        for i in range(n_changes)
    ]
    return ChangePlan(changes=changes, audience_size=audience_size)


def _client(handler):
    return OmniSendClient(api_key="k", transport=httpx.MockTransport(handler),
                          sleep=lambda _: None)


def test_guardrail_trips_over_fraction(monkeypatch):
    monkeypatch.setenv("MAX_DIFF_FRACTION", "0.30")
    plan = _plan(40, 100)  # 40% > 30%
    with pytest.raises(GuardrailTripped):
        apply_plan(plan, client=_client(lambda r: httpx.Response(200, json={})))


def test_force_bypasses_guardrail(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    plan = _plan(40, 100)
    applied, created, errors = apply_plan(
        plan, client=_client(lambda r: httpx.Response(200, json={})), force=True
    )
    assert applied == 40 and errors == 0  # dry-run: gated no-ops, still "applied"


def test_per_contact_error_isolated(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    plan = _plan(3, 100)

    def handler(request: httpx.Request) -> httpx.Response:
        # For tag writes the contact id rides in the request BODY (POST /contacts/tags),
        # not the URL — so inspect both.
        blob = request.url.raw_path.decode() + request.content.decode()
        if '"c1"' in blob or "/c1" in blob:
            return httpx.Response(400, json={"error": "bad"})
        return httpx.Response(200, json={})

    applied, created, errors = apply_plan(plan, client=_client(handler))
    assert applied == 2 and errors == 1  # c1 failed, others fine


def test_auth_error_aborts_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    plan = _plan(3, 100)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    with pytest.raises(OmnisendAuthError):
        apply_plan(plan, client=_client(handler))
