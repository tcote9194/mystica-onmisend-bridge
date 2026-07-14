"""OmniSend client — the read (current state) + the single gated write choke point.

Two hard rules live INSIDE the client so no caller can bypass them (mirrors the
mystica-engine OmniSend client):

  1. **Write gate.** Every mutating call checks :func:`config.external_writes_enabled`
     FIRST. In dry-run the intended change is logged and a synthetic response is
     returned — no HTTP write is issued.
  2. **Retries.** 5xx/429 back off exponentially up to ``HTTP_MAX_RETRIES``; an auth
     error (401/403) hard-fails with a Slack alert rather than silently degrading.

Bases: contacts are READ under the v5 host and tags/properties are WRITTEN under the
/api host — exactly what the live engine client does (verified against the MCP
surface 2026-07-02). The bridge only ever ADDS ``app:*`` tags and SETS its own
custom properties; it never removes tags.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from bridge import config, slack
from bridge.identity import normalize_email
from bridge.models import CurrentContact

log = logging.getLogger("bridge.omnisend")

_RETRY_STATUS = {429, 500, 502, 503, 504}
_AUTH_STATUS = {401, 403}
_RETRY_AFTER_CAP_SEC = 60.0  # OmniSend's rate window is <=60s; never sleep longer than this


def _retry_after_seconds(r: "httpx.Response", fallback: float) -> float:
    """Seconds to wait before retrying a 429, honoring OmniSend's own instruction.

    OmniSend puts the wait in the JSON body (``"retryAfter": N``) and/or the standard
    ``Retry-After`` header. We honor it (capped at ``_RETRY_AFTER_CAP_SEC``) so writes
    pace to the rate limit instead of hammering and failing; falls back to ``fallback``
    (the exponential-backoff delay) when neither is present/parseable."""
    val: Optional[float] = None
    try:
        body = r.json()
        if isinstance(body, dict) and body.get("retryAfter") is not None:
            val = float(body["retryAfter"])
    except Exception:  # non-JSON body
        pass
    if val is None:
        header = r.headers.get("Retry-After")
        if header:
            try:
                val = float(header)
            except ValueError:  # HTTP-date form — not worth parsing; use fallback
                val = None
    if val is None or val < 0:
        return fallback
    # +1s cushion so we retry just AFTER the window opens, not on its exact edge.
    return min(val + 1.0, _RETRY_AFTER_CAP_SEC)


class OmnisendAuthError(RuntimeError):
    """Raised (after a Slack alert) on 401/403 — a misconfigured/rotated key."""


class OmnisendError(RuntimeError):
    """Raised when a request keeps failing after the retry budget is spent."""


def _synthetic_id(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return "dryrun-" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 ``...Z`` string (OmniSend statusChangedAt)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OmniSendClient:
    """Thin OmniSend REST client. One instance per run.

    ``transport`` injects an httpx MockTransport for tests; ``sleep`` is injectable
    so retry tests don't actually wait.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        read_base: Optional[str] = None,
        write_base: Optional[str] = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep=time.sleep,
    ) -> None:
        self._api_key = api_key if api_key is not None else config.omnisend_api_key()
        self._read_base = read_base or config.OMNISEND_READ_BASE
        self._write_base = write_base or config.OMNISEND_WRITE_BASE
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep

    # ------------------------------------------------------------- transport
    def _client(self, base: str) -> httpx.Client:
        return httpx.Client(
            base_url=base,
            headers={
                "X-API-KEY": self._api_key,
                "Content-Type": "application/json",
                # Required on the /api write endpoints (date-based API versioning).
                "Omnisend-Version": config.omnisend_version(),
            },
            transport=self._transport,
            timeout=self._timeout,
        )

    def _request(
        self,
        base: str,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        delay = config.HTTP_BACKOFF_BASE_SEC
        last: Optional[httpx.Response] = None
        with self._client(base) as client:
            for attempt in range(config.HTTP_MAX_RETRIES + 1):
                r = client.request(
                    method,
                    path,
                    params=params,
                    content=json.dumps(json_body) if json_body is not None else None,
                )
                if r.status_code in _AUTH_STATUS:
                    slack.alert(
                        f"OmniSend auth error {r.status_code} on {method} {path} — "
                        f"check OMNISEND_API_KEY"
                    )
                    raise OmnisendAuthError(f"{r.status_code} on {method} {path}")
                if r.status_code in _RETRY_STATUS and attempt < config.HTTP_MAX_RETRIES:
                    last = r
                    # For 429s, wait as long as OmniSend asks (Retry-After) — the fixed
                    # exponential backoff is far shorter than the rate window, so without
                    # this the retry budget burns in seconds and the write fails.
                    wait = _retry_after_seconds(r, delay) if r.status_code == 429 else delay
                    self._sleep(wait)
                    delay *= 2
                    continue
                if r.status_code >= 400:
                    raise OmnisendError(f"{r.status_code} on {method} {path}: {r.text[:200]}")
                return r
        raise OmnisendError(
            f"exhausted retries on {method} {path} "
            f"(last status {last.status_code if last else '??'})"
        )

    # ================================================================== reads
    def _read(self, path: str, *, params: dict | None = None) -> dict:
        r = self._request(self._read_base, "GET", path, params=params)
        try:
            return r.json() if r.content else {}
        except json.JSONDecodeError:
            return {}

    def iter_contacts(
        self, *, limit: int = config.OMNISEND_PAGE_SIZE, max_contacts: int | None = None
    ) -> Iterator[dict]:
        """Yield raw contact dicts, following pagination to exhaustion.

        Handles both live paging shapes (verified in the engine 2026-07-02): the raw
        v5 API returns ``paging.next`` as a full URL; the MCP-normalized shape returns
        ``paging.cursors.after`` + ``hasMore``.
        """
        params: dict[str, Any] = {"limit": min(limit, config.OMNISEND_PAGE_SIZE)}
        n = 0
        url = "/contacts"
        send_params: Optional[dict[str, Any]] = params
        while True:
            body = self._read(url, params=send_params)
            contacts = body.get("contacts", []) or []
            for c in contacts:
                yield c
                n += 1
                if max_contacts is not None and n >= max_contacts:
                    return
            paging = body.get("paging") or {}
            next_url = paging.get("next")
            cursor_after = (paging.get("cursors") or {}).get("after")
            if not contacts:
                break
            if next_url:
                url, send_params = next_url, None
            elif paging.get("hasMore") and cursor_after:
                params["after"] = cursor_after
                url, send_params = "/contacts", params
            else:
                break

    def get_contact_by_email(self, email: str) -> Optional[dict]:
        """Fetch a single contact by email (used for join validation / spot checks).

        ``None`` when no contact matches. OmniSend keys contacts by email, so the
        ``email`` query param returns 0 or 1.
        """
        body = self._read("/contacts", params={"email": normalize_email(email)})
        contacts = body.get("contacts", []) or []
        return contacts[0] if contacts else None

    def load_current(
        self, *, max_contacts: int | None = None
    ) -> dict[str, CurrentContact]:
        """Index the whole contact book as ``{normalized_email -> CurrentContact}``.

        This is the "current OmniSend state" the plan diffs the desired state against.
        Contacts without an email are skipped (they can't be a join target).
        """
        out: dict[str, CurrentContact] = {}
        for c in self.iter_contacts(max_contacts=max_contacts):
            email = normalize_email(c.get("email"))
            if not email:
                continue
            out[email] = CurrentContact(
                contact_id=str(c.get("id") or c.get("contactID") or ""),
                email=email,
                tags=frozenset(str(t) for t in (c.get("tags") or [])),
                props=dict(c.get("customProperties") or {}),
            )
        log.info("omnisend current state: %d contacts with email", len(out))
        return out

    # ================================================================= writes
    def _mutate(self, method: str, path: str, payload: dict) -> dict:
        """A gated mutating call. In dry-run: log + synthetic response, no HTTP."""
        if not config.external_writes_enabled():
            log.info("omnisend %s %s no-op (dry_run)", method, path)
            return {"id": _synthetic_id(payload), "dry_run": True}
        r = self._request(self._write_base, method, path, json_body=payload)
        try:
            return r.json() if r.content else {}
        except json.JSONDecodeError:
            return {}

    def add_tags(self, contact_ids: list[str], tags: list[str]) -> dict:
        """Batch-ADD ``tags`` to ``contact_ids`` (POST /api/contacts/tags). Additive /
        merge — never replaces a contact's existing tags. Payload field is the plural
        ``contactIDs`` array (verified against the live API schema)."""
        return self._mutate(
            "POST", "/contacts/tags", {"contactIDs": list(contact_ids), "tags": tags}
        )

    def set_properties(self, contact_id: str, props: dict[str, Any]) -> dict:
        return self._mutate(
            "PATCH", f"/contacts/{contact_id}", {"customProperties": props}
        )

    def create_contact(
        self,
        email: str,
        *,
        tags: list[str],
        props: dict[str, Any],
        first_name: str | None = None,
        status: str | None = None,
        now: str | None = None,
    ) -> dict:
        """Create (upsert) a contact with the given email-channel ``status`` + tags +
        custom properties, in one call (POST /api/contacts). Records a consent source
        for compliance. Gated by ``external_writes_enabled`` like every mutation."""
        status = status or config.create_missing_status()
        now = now or _utc_now_iso()
        payload: dict[str, Any] = {
            "identifiers": [
                {
                    "type": "email",
                    "id": email,
                    "channels": {"email": {"status": status, "statusChangedAt": now}},
                    "consent": {"source": config.CREATE_CONSENT_SOURCE, "createdAt": now},
                }
            ],
            "tags": list(tags),
        }
        if props:
            payload["customProperties"] = props
        if first_name:
            payload["firstName"] = first_name
        return self._mutate("POST", "/contacts", payload)

    def apply_change(
        self, contact_id: str, *, add_tags: list[str], set_props: dict[str, Any]
    ) -> dict:
        """Apply one contact's delta: add tags (POST /contacts/tags) then set props
        (PATCH /contacts/{id}). Add-only tags + property merge; never a removal."""
        result: dict[str, Any] = {"ok": True}
        if add_tags:
            result["tags"] = self.add_tags([contact_id], add_tags)
        if set_props:
            result["props"] = self.set_properties(contact_id, set_props)
        return result
