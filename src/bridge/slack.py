"""Minimal Slack alerting — a run summary chip on success-with-anomaly + failure.

Degrades to a log line when no token is configured, so local/dry runs never fail
for want of Slack. Never raises into the caller (alerting must not break a run).
"""

from __future__ import annotations

import logging

import httpx

from bridge import config

log = logging.getLogger("bridge.slack")

_POST_URL = "https://slack.com/api/chat.postMessage"


def alert(text: str, *, transport: httpx.BaseTransport | None = None) -> None:
    """Post ``text`` to the bridge channel. No-op (log only) without a token."""
    token = config.slack_bot_token()
    if not token:
        log.warning("[slack:no-token] %s", text)
        return
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            r = client.post(
                _POST_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": config.slack_channel(), "text": text},
            )
            body = r.json() if r.content else {}
            if not body.get("ok"):
                log.warning("slack post failed: %s", body.get("error", r.status_code))
    except Exception as exc:  # pragma: no cover - alerting must never break a run
        log.warning("slack post errored: %s", exc)
