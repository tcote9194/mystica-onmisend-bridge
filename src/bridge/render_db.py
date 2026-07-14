"""Read-only Render (Postgres) client — the roster + membership facts.

Read-only is enforced two ways, belt-and-suspenders:
  1. the connection forces ``default_transaction_read_only=on`` (any write raises), and
  2. we only ever issue SELECT.
Please ALSO issue a role that only has SELECT (Phase 0 ask) so it's enforced at the
server independent of this code.

``psycopg`` is imported lazily inside methods so the module (and the rest of the
bridge's unit tests) import cleanly on a box without the driver installed. Column
and table names come from ``config`` (env-overridable) so Phase 0 schema discovery
repoints the bridge without a code edit.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from bridge import config
from bridge.identity import normalize_email
from bridge.models import InteractionAggregate, RenderUser

log = logging.getLogger("bridge.render_db")


class RenderConfigError(RuntimeError):
    """Raised when Render is asked for data without a connection string."""


class RenderDB:
    """Thin read-only accessor over the Render users table.

    ``connect`` is injectable (a fake connection factory) so tests exercise
    ``fetch_roster`` without a live database.
    """

    def __init__(self, dsn: Optional[str] = None, *, connect=None) -> None:
        self._dsn = dsn if dsn is not None else config.render_database_url()
        self._connect = connect  # injectable for tests; None -> real psycopg

    # ------------------------------------------------------------- connection
    def _open(self):
        if not self._dsn:
            raise RenderConfigError("RENDER_DATABASE_URL is not set")
        if self._connect is not None:
            return self._connect(self._dsn)
        import psycopg  # lazy — not needed for unit tests / import

        # ``.strip()`` guards against a stray newline/space in the env value. Read-only
        # is enforced with a session SET *after* connecting rather than the libpq
        # ``options=`` connect kwarg — that kwarg (its value contains a space) mis-
        # serialized on the deploy host's psycopg build and raised "invalid connection
        # option". The SET is version-independent and equally strict (writes raise).
        conn = psycopg.connect(self._dsn.strip(), autocommit=True)
        conn.execute("SET default_transaction_read_only TO on")
        return conn

    # ------------------------------------------------------------------ reads
    def _select(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return list(cur.fetchall())
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    @staticmethod
    def _qi(identifier: str) -> str:
        """Double-quote a SQL identifier so reserved words (e.g. the ``user`` table)
        and mixed-case names are safe. Supports a dotted ``schema.table`` form."""
        return ".".join('"' + part.replace('"', '""') + '"' for part in identifier.split("."))

    def _customer_where(self) -> str:
        """The combined WHERE that defines an addressable customer: the configured
        roster filter (role / not-deleted) AND — if set — the recency cutoff
        (``last_seen_at >= now() - interval 'N days'``). Applied identically to the
        roster and the interaction scope so the recency rule is consistent everywhere.
        Returns "" when neither is configured."""
        conditions: list[str] = []
        where = config.render_roster_where().strip()
        if where:
            conditions.append(f"({where})")
        days = config.render_active_within_days()
        last_seen = config.render_last_seen_at_col().strip()
        if days > 0 and last_seen:
            # ``days`` is an int from config (not user input) — safe to inline.
            conditions.append(
                f"{self._qi(last_seen)} >= now() - interval '{days} days'"
            )
        return " AND ".join(conditions)

    def _roster_sql(self) -> str:
        parts = [
            self._qi(config.render_user_id_col()),
            self._qi(config.render_email_col()),
            self._qi(config.render_installed_at_col()),
            self._qi(config.render_signed_up_at_col()),
        ]
        # 5th column: last_seen (app recency) if configured, else a NULL placeholder
        # so fetch_roster can always unpack a fixed tuple.
        last_seen = config.render_last_seen_at_col().strip()
        parts.append(self._qi(last_seen) if last_seen else "NULL")
        # 6th column: display name (for firstName on contact creation).
        name = config.render_name_col().strip()
        parts.append(self._qi(name) if name else "NULL")
        sql = f"SELECT {', '.join(parts)} FROM {self._qi(config.render_users_table())}"
        where = self._customer_where()
        if where:
            sql += f" WHERE {where}"
        return sql

    def fetch_roster(self) -> list[RenderUser]:
        """Every app user with id/email/install/signup facts.

        The roster is small enough to pull whole once per run (the identity bridge
        caches it in memory). Rows are returned raw — normalization happens in
        :class:`~bridge.identity.IdentityBridge`.
        """
        rows = self._select(self._roster_sql())
        out: list[RenderUser] = []
        for uid, email, installed_at, signed_up_at, last_seen_at, name in rows:
            out.append(
                RenderUser(
                    user_id=str(uid) if uid is not None else "",
                    email=(str(email) if email not in (None, "") else None),
                    installed_at=(str(installed_at) if installed_at is not None else None),
                    signed_up_at=(str(signed_up_at) if signed_up_at is not None else None),
                    last_seen_at=(str(last_seen_at) if last_seen_at is not None else None),
                    name=(str(name) if name not in (None, "") else None),
                )
            )
        log.info("render roster: %d users", len(out))
        return out

    def iter_roster(self) -> Iterator[RenderUser]:
        yield from self.fetch_roster()

    # ------------------------------------------------- interaction (Render-native)
    def _customer_subquery(self) -> str:
        """``(SELECT "id" FROM "user" WHERE <customer filter>)`` — the scoped user-id
        set both interaction aggregates restrict to, so we never count advisor chats
        AND we honor the same recency cutoff as the roster (pure recency everywhere)."""
        base = (
            f"SELECT {self._qi(config.render_user_id_col())} "
            f"FROM {self._qi(config.render_users_table())}"
        )
        where = self._customer_where()
        if where:
            base += f" WHERE {where}"
        return f"({base})"

    def _chat_agg_sql(self) -> str:
        uid = self._qi(config.render_chat_user_col())
        sess = self._qi(config.render_chat_session_col())
        ts = self._qi(config.render_chat_time_col())
        tbl = self._qi(config.render_chat_table())
        return (
            f"SELECT {uid}, count(distinct {sess}), count(*), min({ts}), max({ts}) "
            f"FROM {tbl} WHERE {uid} IN {self._customer_subquery()} GROUP BY {uid}"
        )

    def _draw_agg_sql(self) -> str:
        uid = self._qi(config.render_draw_user_col())
        ts = self._qi(config.render_draw_time_col())
        tbl = self._qi(config.render_draw_table())
        return (
            f"SELECT {uid}, count(*), max({ts}) "
            f"FROM {tbl} WHERE {uid} IN {self._customer_subquery()} GROUP BY {uid}"
        )

    def _reading_agg_sql(self) -> str:
        uid = self._qi(config.render_reading_user_col())
        ts = self._qi(config.render_reading_time_col())
        tbl = self._qi(config.render_reading_table())
        return (
            f"SELECT {uid}, count(*), max({ts}) "
            f"FROM {tbl} WHERE {uid} IN {self._customer_subquery()} GROUP BY {uid}"
        )

    def fetch_interaction_aggregates(self) -> list[InteractionAggregate]:
        """Per-user interaction rollups from Render, keyed by ``user_id`` (the same id
        the identity bridge maps to email — so resolution is lossless, unlike PostHog).

        Produces the shared :class:`~bridge.models.InteractionAggregate`. ``chat_count``
        = distinct chat sessions; ``draw_count`` = daily draws; ``reading_count`` =
        delivered readings; ``interaction_count`` = their sum (meaningful app actions);
        ``last_seen_at`` = the latest of last chat / draw / reading.
        """
        chats = {r[0]: r for r in self._select(self._chat_agg_sql())}
        draws = {r[0]: r for r in self._select(self._draw_agg_sql())}
        readings = {r[0]: r for r in self._select(self._reading_agg_sql())}
        out: list[InteractionAggregate] = []
        for uid in set(chats) | set(draws) | set(readings):
            c = chats.get(uid)
            d = draws.get(uid)
            r = readings.get(uid)
            chat_sessions = int(c[1]) if c else 0
            draw_count = int(d[1]) if d else 0
            reading_count = int(r[1]) if r else 0
            last_dts = [x for x in (
                c[4] if c else None,
                d[2] if d else None,
                r[2] if r else None,
            ) if x is not None]
            last_seen_dt = max(last_dts) if last_dts else None
            out.append(
                InteractionAggregate(
                    distinct_id=str(uid),
                    chat_count=chat_sessions,
                    draw_count=draw_count,
                    reading_count=reading_count,
                    interaction_count=chat_sessions + draw_count + reading_count,
                    first_chat_at=(str(c[3]) if c and c[3] is not None else None),
                    last_chat_at=(str(c[4]) if c and c[4] is not None else None),
                    last_seen_at=(str(last_seen_dt) if last_seen_dt is not None else None),
                )
            )
        log.info("render interaction: %d users with chats/draws/readings", len(out))
        return out

    # ------------------------------------------- soulmate reading fulfillment stages
    def _reading_abandoned_sql(self) -> str:
        tbl = self._qi(config.render_reading_intent_table())
        email = self._qi(config.render_reading_intent_email_col())
        status = self._qi(config.render_reading_intent_status_col())
        st = config.reading_abandoned_status().replace("'", "''")  # literal, from config
        return (
            f"SELECT {email} FROM {tbl} "
            f"WHERE {status} = '{st}' AND \"deleted_at\" IS NULL AND {email} IS NOT NULL"
        )

    def _reading_delivered_sql(self) -> str:
        reading = self._qi(config.render_reading_table())
        users = self._qi(config.render_users_table())
        r_user = self._qi(config.render_reading_user_col())
        delivered = self._qi(config.render_reading_delivered_col())
        u_id = self._qi(config.render_user_id_col())
        u_email = self._qi(config.render_email_col())
        return (
            f"SELECT DISTINCT u.{u_email} "
            f"FROM {reading} r JOIN {users} u ON r.{r_user} = u.{u_id} "
            f"WHERE r.{delivered} IS NOT NULL AND r.\"deleted_at\" IS NULL "
            f"AND u.{u_email} IS NOT NULL"
        )

    def fetch_reading_stages(self) -> dict[str, set[str]]:
        """``{normalized_email -> {reading tags}}`` from the Soulmate reading funnel.

        ``app: reading_abandoned`` for pending-inputs intents (paid, form not filled);
        ``app: reading_delivered`` for buyers whose reading was delivered. Keyed by
        email directly (no roster join, no recency gate) — a paid-but-abandoned buyer
        gets the nudge regardless of how recently they opened the app.
        """
        out: dict[str, set[str]] = {}
        for (email,) in self._select(self._reading_abandoned_sql()):
            e = normalize_email(email)
            if e:
                out.setdefault(e, set()).add(config.TAG_READING_ABANDONED)
        for (email,) in self._select(self._reading_delivered_sql()):
            e = normalize_email(email)
            if e:
                out.setdefault(e, set()).add(config.TAG_READING_DELIVERED)
        log.info("render reading stages: %d contacts", len(out))
        return out
