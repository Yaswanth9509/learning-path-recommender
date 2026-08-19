"""The SQLite/Postgres seam.

None of this touches a network: the translation helpers are pure functions, and
the round-trip tests run against SQLite, which is the engine every test uses.
Postgres itself is exercised by pointing `DATABASE_URL` at a real server and
running the suite, not from here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend import sqlstore
from backend.db import Database
from backend.models import LearnerProfile

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- placeholders
def test_question_marks_become_postgres_placeholders():
    assert sqlstore._to_pg_placeholders(
        "SELECT * FROM users WHERE id = ? AND email = ?", escape_percent=False
    ) == "SELECT * FROM users WHERE id = %s AND email = %s"


def test_a_question_mark_inside_a_literal_is_left_alone():
    """A naive replace would corrupt the first query with `?` in a string."""
    sql = "SELECT * FROM t WHERE note = 'why? because' AND id = ?"
    assert sqlstore._to_pg_placeholders(sql, escape_percent=False) == (
        "SELECT * FROM t WHERE note = 'why? because' AND id = %s"
    )


def test_percent_is_doubled_only_when_parameters_are_bound():
    """psycopg reads `%` as a placeholder, but only when it interpolates.

    A statement sent without parameters is passed through verbatim, so doubling
    there would leave a literal `%%` in the SQL.
    """
    sql = "SELECT * FROM t WHERE name LIKE 'a%' AND id = ?"
    assert "LIKE 'a%'" in sqlstore._to_pg_placeholders(sql, escape_percent=False)
    # Inside a quoted literal the percent is left alone either way.
    bound = sqlstore._to_pg_placeholders("SELECT 100 % ? AS r", escape_percent=True)
    assert bound == "SELECT 100 %% %s AS r"


# -------------------------------------------------------------------- schema
def test_autoincrement_is_rewritten_for_postgres():
    ddl = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, x TEXT)"
    assert "SERIAL PRIMARY KEY" in sqlstore._to_pg_schema(ddl)
    assert "AUTOINCREMENT" not in sqlstore._to_pg_schema(ddl)


def test_a_semicolon_inside_a_comment_cannot_split_a_statement():
    """The real schema has one, and splitting first sent English to the server."""
    script = (
        "-- a comment; with a semicolon in it\n"
        "CREATE TABLE a (id TEXT);\n"
        "CREATE TABLE b (id TEXT);\n"
    )
    stripped = sqlstore._strip_sql_comments(script)
    statements = [chunk.strip() for chunk in stripped.split(";") if chunk.strip()]
    assert statements == ["CREATE TABLE a (id TEXT)", "CREATE TABLE b (id TEXT)"]


# --------------------------------------------------------- channel binding
def test_dropping_channel_binding_keeps_the_rest_of_the_url():
    """Only that one parameter goes; TLS and credentials must be untouched."""
    url = (
        "postgresql://user:secret@host.neon.tech/db"
        "?sslmode=require&channel_binding=require"
    )
    relaxed = sqlstore._without_channel_binding(url)
    assert "channel_binding" not in relaxed
    assert "sslmode=require" in relaxed
    assert "user:secret@host.neon.tech" in relaxed
    assert relaxed.endswith("/db?sslmode=require")


def test_a_url_without_channel_binding_is_returned_unchanged():
    """Equality is how the caller decides there is no fallback worth trying."""
    url = "postgresql://user@host/db?sslmode=require"
    assert sqlstore._without_channel_binding(url) == url


def test_only_a_channel_binding_error_triggers_the_fallback():
    """Any other failure must surface, not be retried into a worse config."""
    assert sqlstore._is_channel_binding_failure(
        Exception("SCRAM: channel binding required but not supported")
    )
    assert not sqlstore._is_channel_binding_failure(
        Exception("could not translate host name")
    )


def test_a_logged_url_never_carries_the_password():
    url = "postgresql://user:hunter2@host.neon.tech/neondb?sslmode=require"
    assert sqlstore.redact(url) == "host.neon.tech/neondb"
    assert "hunter2" not in sqlstore.redact(url)


NEON_STYLE_URL = (
    "postgresql://u:p@host.neon.tech/db?sslmode=require&channel_binding=require"
)


def test_a_host_that_refuses_channel_binding_is_retried_without_it(monkeypatch):
    """Neon issues `channel_binding=require`; not every host can honour it."""
    psycopg = pytest.importorskip("psycopg")
    attempted: list[str] = []

    def fake_open(self, url):
        attempted.append(url)
        if "channel_binding" in url:
            raise psycopg.OperationalError(
                "SCRAM: channel binding required but not supported"
            )
        return object()

    monkeypatch.setattr(sqlstore.Store, "_open", fake_open)
    store = sqlstore.Store(url=NEON_STYLE_URL)

    assert len(attempted) == 2, "the relaxed URL was not tried"
    assert "channel_binding" in attempted[0]
    assert "channel_binding" not in attempted[1]
    # A later reconnect must use the form that worked, not the one that failed.
    assert "channel_binding" not in store.url


def test_any_other_connection_failure_is_raised_not_retried(monkeypatch):
    """Retrying a DNS or auth failure would only hide it behind a worse config."""
    psycopg = pytest.importorskip("psycopg")
    attempted: list[str] = []

    def fake_open(self, url):
        attempted.append(url)
        raise psycopg.OperationalError("could not translate host name")

    monkeypatch.setattr(sqlstore.Store, "_open", fake_open)
    with pytest.raises(psycopg.OperationalError, match="translate host name"):
        sqlstore.Store(url=NEON_STYLE_URL)
    assert len(attempted) == 1, "a non-channel-binding failure was retried"


# ------------------------------------------------------------ engine choice
def test_no_database_url_means_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    database = Database(tmp_path / "plain.db")
    try:
        assert database.is_postgres is False
        assert (tmp_path / "plain.db").exists()
    finally:
        database.close()


def test_an_explicit_path_wins_over_the_environment(tmp_path, monkeypatch):
    """The tests and a local run must stay on SQLite whatever is configured."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@nowhere.invalid/db")
    database = Database(tmp_path / "explicit.db")
    try:
        assert database.is_postgres is False
    finally:
        database.close()


def test_the_health_endpoint_names_the_engine(client):
    """A deployment silently on SQLite loses every account when it sleeps."""
    assert client.get("/api/health").json()["storage"] == "sqlite"


# --------------------------------------------------------------- round trip
def test_the_upsert_keeps_one_row_per_learner(tmp_path, monkeypatch):
    """`ON CONFLICT ... DO UPDATE` is spelled the same for both engines."""
    monkeypatch.setenv("DATABASE_URL", "")
    database = Database(tmp_path / "upsert.db")
    try:
        profile = LearnerProfile(learner_id="abc", name="First")
        database.save_profile(profile, user_id="owner-1")
        profile.name = "Second"
        database.save_profile(profile)

        assert database.get_profile("abc").name == "Second"
        assert len(database.list_profiles()) == 1
        # A later write without a user_id must not orphan the learner.
        assert database.owner_of("abc") == "owner-1"
    finally:
        database.close()


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="set POSTGRES_TEST_URL to run the suite against a real Postgres",
)
def test_postgres_round_trip():
    """Opt-in: the same assertions against a real server."""
    database = Database(url=os.environ["POSTGRES_TEST_URL"])
    try:
        assert database.is_postgres is True
        profile = LearnerProfile(learner_id="pgtest001", name="First")
        database.save_profile(profile, user_id="pg-owner")
        profile.name = "Second"
        database.save_profile(profile)
        assert database.get_profile("pgtest001").name == "Second"
        assert database.owner_of("pgtest001") == "pg-owner"
    finally:
        database.delete_learner("pgtest001")
        database.close()


def test_postgres_connections_use_autocommit():
    """A read must not leave the connection idle inside a transaction.

    With `autocommit=False` psycopg opens a transaction on the first statement
    and holds it until an explicit commit, which the read paths never issue.
    Neon terminates such a session, and the next write then fails with
    `IdleInTransactionSessionTimeout` — a 500 that looks random because it
    depends on how long the app sat quiet.
    """
    source = (ROOT / "backend" / "sqlstore.py").read_text(encoding="utf-8")
    assert "autocommit=True" in source
    assert "autocommit=False" not in source


def test_a_terminated_session_counts_as_a_dropped_connection():
    """`IdleInTransactionSessionTimeout` is an InternalError, not Operational.

    The first version of the retry only caught `OperationalError`, so the very
    failure that Neon produces was the one it could not recover from.
    """
    psycopg = pytest.importorskip("psycopg")
    assert not issubclass(
        psycopg.errors.IdleInTransactionSessionTimeout, psycopg.OperationalError
    ), "psycopg changed the hierarchy; the drop check needs revisiting"

    store = sqlstore.Store.__new__(sqlstore.Store)
    store.is_postgres = True
    store._psycopg = psycopg
    store._conn = None
    assert store._is_dropped(
        psycopg.errors.IdleInTransactionSessionTimeout("terminating connection")
    )
    assert store._is_dropped(psycopg.OperationalError("connection lost"))
    assert not store._is_dropped(ValueError("unrelated"))
