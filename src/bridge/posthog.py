"""PostHog interaction aggregates via a single server-side HogQL query.

Rather than page raw events client-side (what the lossy n8n workflow effectively
did), we ask PostHog to aggregate per ``distinct_id`` in one HogQL query: chat
count, daily-draw count, total interaction count, first/last chat, last-seen. That
one round-trip returns exactly what the bridge needs to compute interaction tags.

Failure policy: this is a batch job whose output only ADDS tags/props (never
removes), so an empty pull is *safe* — but a configured-yet-erroring PostHog must
not masquerade as "no activity". :meth:`aggregate` therefore RAISES on an HTTP/parse
error when configured; the CLI catches it, marks the run DEGRADED, and still lets
the (independent) membership phase proceed.

Event names are unverified until Phase 0.4 — they come from ``config`` (env-
overridable) with the engine's ``DEFAULT_EVENTS`` as the initial guess.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from bridge import config
from bridge.models import PostHogAggregate

log = logging.getLogger("bridge.posthog")


class PostHogError(RuntimeError):
    """Raised when a configured PostHog query fails (so the run reports DEGRADED)."""


def _hogql_in(values: tuple[str, ...]) -> str:
    """Render a tuple of event names as a HogQL IN-list literal, quotes escaped."""
    quoted = ", ".join("'" + v.replace("'", "\\'") + "'" for v in values)
    return f"({quoted})"


class PostHogClient:
    """One HogQL aggregate query against project ``POSTHOG_PROJECT``.

    ``transport`` injects an httpx MockTransport for tests. Personal API key +
    project id come from config; ``configured`` distinguishes 'no key' (skip the
    interaction phase entirely) from 'matched nothing' (a real empty result).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        project: Optional[str] = None,
        host: Optional[str] = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key if api_key is not None else config.posthog_api_key()
        self._project = project or config.posthog_project()
        self._host = (host or config.posthog_host()).rstrip("/")
        self._transport = transport
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._project)

    def _query(self, hogql: str) -> dict:
        url = f"/api/projects/{self._project}/query"
        with httpx.Client(
            base_url=self._host,
            headers={"Authorization": f"Bearer {self._api_key}"},
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            r = client.post(url, json={"query": {"kind": "HogQLQuery", "query": hogql}})
            r.raise_for_status()
            return r.json()

    def _build_hogql(self, *, since_days: Optional[int]) -> str:
        chats = _hogql_in(config.posthog_chat_events())
        draws = _hogql_in(config.posthog_draw_events())
        activity = _hogql_in(config.posthog_activity_events())
        where = f"WHERE event IN {activity}"
        if since_days:
            where += f" AND timestamp > now() - INTERVAL {int(since_days)} DAY"
        # One row per distinct_id with all the rollups the bridge needs.
        return (
            "SELECT distinct_id, "
            f"countIf(event IN {chats}) AS chat_count, "
            f"countIf(event IN {draws}) AS draw_count, "
            "count() AS interaction_count, "
            f"minIf(timestamp, event IN {chats}) AS first_chat_at, "
            f"maxIf(timestamp, event IN {chats}) AS last_chat_at, "
            "max(timestamp) AS last_seen_at "
            "FROM events "
            f"{where} "
            "GROUP BY distinct_id"
        )

    def aggregate(self, *, since_days: Optional[int] = None) -> list[PostHogAggregate]:
        """Per-``distinct_id`` interaction rollups. ``[]`` if unconfigured.

        ``since_days`` optionally bounds the window (None = all time). Raises
        :class:`PostHogError` on any transport/parse failure when configured.
        """
        if not self.configured:
            log.info("posthog not configured — skipping interaction aggregate")
            return []
        try:
            body = self._query(self._build_hogql(since_days=since_days))
        except (httpx.HTTPError, ValueError) as exc:
            raise PostHogError(f"PostHog aggregate query failed: {exc}") from exc

        results = body.get("results") or []
        out: list[PostHogAggregate] = []
        for row in results:
            # HogQL returns positional rows matching the SELECT order above.
            if not row:
                continue
            distinct_id = str(row[0]) if row[0] is not None else ""
            if not distinct_id:
                continue
            out.append(
                PostHogAggregate(
                    distinct_id=distinct_id,
                    chat_count=int(row[1] or 0),
                    draw_count=int(row[2] or 0),
                    interaction_count=int(row[3] or 0),
                    first_chat_at=(str(row[4]) if row[4] else None),
                    last_chat_at=(str(row[5]) if row[5] else None),
                    last_seen_at=(str(row[6]) if row[6] else None),
                )
            )
        log.info("posthog aggregate: %d distinct_ids", len(out))
        return out
