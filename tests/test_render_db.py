"""Render client tests — SQL shape + interaction aggregation, with a fake connection.

No live database: a fake connection factory returns canned rows based on which query
(roster / chat-agg / draw-agg) is executed.
"""

from __future__ import annotations

from datetime import datetime

from bridge.render_db import RenderDB, _clean_dsn


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        self._last = sql

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, router):
        self._router = router

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _factory(router):
    """Return a connect(dsn) that routes by SQL content to canned rows."""
    def connect(dsn):
        conn = _FakeConn(router)

        class Cur:
            def execute(self, sql, params=()):
                conn._rows = router(sql)

            def fetchall(self):
                return conn._rows

        conn._cursor = Cur()
        return conn

    return connect


def test_clean_dsn_unmangles_railway_paste_variants():
    base = "postgresql://user:PW@dpg-x.oregon-postgres.render.com/db?sslmode=require"
    variants = {
        "clean": base,
        "leading_eq": "=" + base,                 # the actual Railway crash: invalid option ""
        "name_prefix": "RENDER_DATABASE_URL=" + base,
        "dquoted": '"' + base + '"',
        "squoted": "'" + base + "'",
        "psql_prefix": "psql " + base,
        "spaced": "  " + base + "  ",
        "crlf": "\r\n" + base + "\r\n",
        # the real Railway crash: a whole second dotenv line pasted onto the value
        "multiline_paste": "=" + base + "\nOMNISEND_API_KEY=69a359-secret",
    }
    for label, raw in variants.items():
        assert _clean_dsn(raw) == base, label
    # a value with no scheme is returned stripped (surfaces a clear libpq error, not a silent hang)
    assert _clean_dsn("  not-a-url  ") == "not-a-url"
    assert _clean_dsn(None) == ""


def test_roster_sql_quotes_reserved_table_and_filters(monkeypatch):
    monkeypatch.setenv("RENDER_USERS_TABLE", "user")
    monkeypatch.setenv("RENDER_INSTALLED_AT_COL", "created_at")
    monkeypatch.setenv("RENDER_SIGNED_UP_AT_COL", "created_at")
    monkeypatch.setenv("RENDER_LAST_SEEN_COL", "last_seen_at")
    monkeypatch.setenv("RENDER_ROSTER_WHERE", "user_role = 'user' AND deleted_at IS NULL")
    sql = RenderDB(dsn="x")._roster_sql()
    assert 'FROM "user"' in sql
    assert '"created_at"' in sql and '"last_seen_at"' in sql
    assert "user_role = 'user' AND deleted_at IS NULL" in sql


def test_recency_cutoff_applied_to_roster_and_interaction(monkeypatch):
    monkeypatch.setenv("RENDER_USERS_TABLE", "user")
    monkeypatch.setenv("RENDER_LAST_SEEN_COL", "last_seen_at")
    monkeypatch.setenv("RENDER_ROSTER_WHERE", "user_role = 'user' AND deleted_at IS NULL")
    monkeypatch.setenv("RENDER_ACTIVE_WITHIN_DAYS", "180")
    db = RenderDB(dsn="x")
    roster_sql = db._roster_sql()
    sub = db._customer_subquery()
    clause = "\"last_seen_at\" >= now() - interval '180 days'"
    assert clause in roster_sql               # roster honors the cutoff
    assert clause in sub                      # interaction scope honors the SAME cutoff
    assert "user_role = 'user'" in roster_sql  # combined with the role filter


def test_recency_cutoff_disabled_with_zero(monkeypatch):
    monkeypatch.setenv("RENDER_ROSTER_WHERE", "user_role = 'user'")
    monkeypatch.setenv("RENDER_ACTIVE_WITHIN_DAYS", "0")  # explicitly disable the cutoff
    sql = RenderDB(dsn="x")._roster_sql()
    assert "interval" not in sql


def test_fetch_roster_maps_columns(monkeypatch):
    rows = [("u1", "a@x.com", "2026-01-01", "2026-01-01", "2026-07-01", "Alice Smith")]
    db = RenderDB(dsn="x", connect=_factory(lambda sql: rows))
    roster = db.fetch_roster()
    assert roster[0].user_id == "u1"
    assert roster[0].email == "a@x.com"
    assert roster[0].last_seen_at == "2026-07-01"
    assert roster[0].name == "Alice Smith"
    assert roster[0].first_name == "Alice"  # first token of the display name


def test_fetch_interaction_aggregates_merges_chats_draws_readings(monkeypatch):
    dt = datetime
    chat_rows = [("u1", 5, 24, dt(2026, 1, 5), dt(2026, 1, 20)),
                 ("u2", 1, 3, dt(2026, 2, 1), dt(2026, 2, 2))]
    draw_rows = [("u1", 4, dt(2026, 1, 25)),           # u1 last draw AFTER last chat
                 ("u3", 2, dt(2026, 3, 1))]             # u3 only drew, never chatted
    reading_rows = [("u1", 1, dt(2026, 1, 10)),
                    ("u4", 3, dt(2026, 4, 1))]          # u4 only read

    def router(sql):
        # Route by table: chat has "distinct"; draw/reading differ by table name.
        if "distinct" in sql:
            return chat_rows
        if "daily_card_draw" in sql:
            return draw_rows
        return reading_rows

    db = RenderDB(dsn="x", connect=_factory(router))
    aggs = {a.distinct_id: a for a in db.fetch_interaction_aggregates()}
    assert set(aggs) == {"u1", "u2", "u3", "u4"}

    u1 = aggs["u1"]
    assert u1.chat_count == 5 and u1.draw_count == 4 and u1.reading_count == 1
    assert u1.interaction_count == 10                   # 5 + 4 + 1
    assert u1.last_seen_at.startswith("2026-01-25")     # latest of chat / draw / reading

    u3 = aggs["u3"]
    assert u3.chat_count == 0 and u3.draw_count == 2
    assert u3.first_chat_at is None

    u4 = aggs["u4"]
    assert u4.reading_count == 3 and u4.chat_count == 0


def test_fetch_reading_stages_maps_abandoned_and_delivered(monkeypatch):
    abandoned_rows = [("Paid1@X.com",), ("paid2@x.com",)]
    delivered_rows = [("got1@x.com",), ("paid2@x.com",)]  # paid2 abandoned one, got another

    def router(sql):
        return abandoned_rows if "soulmate_reading_intent" in sql else delivered_rows

    from bridge import config
    db = RenderDB(dsn="x", connect=_factory(router))
    stages = db.fetch_reading_stages()
    assert stages["paid1@x.com"] == {config.TAG_READING_ABANDONED}      # normalized + tagged
    assert stages["got1@x.com"] == {config.TAG_READING_DELIVERED}
    assert stages["paid2@x.com"] == {config.TAG_READING_ABANDONED, config.TAG_READING_DELIVERED}


def test_reading_abandoned_sql_uses_configured_status(monkeypatch):
    monkeypatch.setenv("READING_ABANDONED_STATUS", "pending_inputs")
    sql = RenderDB(dsn="x")._reading_abandoned_sql()
    assert "soulmate_reading_intent" in sql and "'pending_inputs'" in sql
    assert RenderDB(dsn="x")._reading_delivered_sql().count("JOIN") == 1
