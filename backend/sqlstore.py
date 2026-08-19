"""One connection interface over SQLite and Postgres.

The application is written against SQLite and stays that way: no `DATABASE_URL`
means the same single-file database as before, with no new dependency and no
service to run. Set `DATABASE_URL` and the identical SQL is sent to Postgres
instead, which is what a deployment needs — Render's free tier has no
persistent disk, so a SQLite file is erased whenever the instance sleeps and
registered accounts would vanish with it.

Only three things actually differ between the two, and they are all handled
here rather than being smeared across every query:

* placeholders — `?` in SQLite, `%s` in Postgres;
* autoincrement — `INTEGER PRIMARY KEY AUTOINCREMENT` against `SERIAL`;
* dropped connections — Neon suspends an idle database, so a long-lived
  connection has to be re-established rather than assumed good.

Everything else, `ON CONFLICT ... DO UPDATE SET ... excluded.x` included, is
spelled the same way by both engines.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config  # noqa: F401  — importing loads .env before we read it

log = logging.getLogger("api")

#: A deployment that cannot reach its database should say so, not hang.
CONNECT_TIMEOUT_SECONDS = 10


def database_url() -> str:
    """The Postgres URL, or empty when this instance should use SQLite."""
    return (os.environ.get("DATABASE_URL") or "").strip()


def _without_channel_binding(url: str) -> str:
    """The same URL with `channel_binding` removed, or unchanged if absent.

    Neon hands out connection strings carrying `channel_binding=require`. It is
    a genuine protection — it binds the SCRAM handshake to the TLS channel, so
    a proxy that terminates TLS cannot replay the authentication — and it works
    wherever libpq supports it. Some hosts and poolers do not, and there the
    requirement is the only thing standing between the app and its database.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _is_channel_binding_failure(exc: Exception) -> bool:
    """Whether a failed connection is blaming channel binding specifically."""
    return "channel binding" in str(exc).lower()


def redact(url: str) -> str:
    """A URL safe to log: host and database only, never the password."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        return f"{host}{parts.path}"
    except ValueError:  # pragma: no cover — a URL too broken to parse
        return "?"


def _to_pg_placeholders(sql: str, escape_percent: bool) -> str:
    """`?` -> `%s`, leaving anything inside a quoted literal alone.

    A bare `str.replace` would be fine for the queries in this project today,
    but it would corrupt the first query that contains a `?` in a string.

    `escape_percent` doubles any literal `%`, which psycopg requires — but only
    when parameters are actually bound, since a statement sent without them is
    passed through verbatim and `%%` would survive into the SQL.
    """
    out = []
    quote: Optional[str] = None
    for char in sql:
        if quote:
            if char == quote:
                quote = None
            out.append(char)
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%" and escape_percent:
            out.append("%%")
        else:
            out.append(char)
    return "".join(out)


def _strip_sql_comments(statement: str) -> str:
    """Drop `--` comment lines so a leading comment cannot hide a statement."""
    return "\n".join(
        line for line in statement.splitlines() if not line.strip().startswith("--")
    ).strip()


def _to_pg_schema(sql: str) -> str:
    """Rewrite the SQLite DDL as the Postgres equivalent."""
    return re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "SERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )


class _Rows:
    """A finished result set, so no cursor has to outlive its statement."""

    __slots__ = ("_rows", "rowcount")

    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Store:
    """A database connection that speaks whichever engine is configured.

    Callers use `execute`, `executescript` and `commit` exactly as they used
    the raw `sqlite3.Connection` before, and read rows by column name.
    """

    def __init__(
        self, path: Optional[Path | str] = None, url: Optional[str] = None
    ) -> None:
        # `None` means "decide from the environment"; an empty string is a
        # caller saying "SQLite, whatever DATABASE_URL happens to be set to".
        # Collapsing the two would let a stray env var redirect the tests.
        self.url = database_url() if url is None else url.strip()
        self.is_postgres = bool(self.url)
        self.path = None if self.is_postgres else Path(path or ":memory:")
        self._conn: Any = None
        self._connect()

    # ------------------------------------------------------------- lifecycle
    def _connect(self) -> None:
        if self.is_postgres:
            import psycopg

            self._psycopg = psycopg
            try:
                self._conn = self._open(self.url)
            except psycopg.OperationalError as exc:
                relaxed = _without_channel_binding(self.url)
                if relaxed == self.url or not _is_channel_binding_failure(exc):
                    raise
                # Drop the requirement rather than the connection. Without the
                # parameter libpq negotiates channel binding anyway wherever
                # both ends support it — what is lost is the guarantee, not the
                # protection, and the alternative here is no database at all.
                log.warning(
                    "postgres at %s refused channel binding (%s); retrying "
                    "without it. Authentication is still over TLS.",
                    redact(self.url), exc,
                )
                self._conn = self._open(relaxed)
                # Reconnects must not keep retrying the form that just failed.
                self.url = relaxed
        else:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _open(self, url: str) -> Any:
        """Open one Postgres connection.

        The timeout is deliberate: a deployment that cannot reach its database
        should fail fast and legibly, not hang a request on a TCP timeout.
        """
        from psycopg.rows import dict_row

        return self._psycopg.connect(
            url,
            row_factory=dict_row,
            # Autocommit, deliberately. Every operation here is a single
            # statement run under `Database._lock`, so there is no multi-
            # statement transaction to protect. Without it psycopg opens a
            # transaction on the first read and holds it until an explicit
            # commit — which the read paths never issue — leaving the
            # connection idle-in-transaction until the server kills it.
            # Neon does exactly that, and the next write then fails with a
            # 500 that looks random.
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )

    @property
    def integrity_error(self) -> tuple[type[Exception], ...]:
        """The exception a duplicate key raises, whichever engine is in use."""
        if self.is_postgres:
            return (self._psycopg.errors.UniqueViolation, self._psycopg.IntegrityError)
        return (sqlite3.IntegrityError,)

    def _is_dropped(self, exc: Exception) -> bool:
        """Whether the connection is gone and a retry should re-establish it.

        Not just `OperationalError`: a server that terminates the session
        raises by SQLSTATE class, and `IdleInTransactionSessionTimeout` is an
        `InternalError`. Checking the connection itself is the reliable test —
        the exception type alone missed this and turned a recoverable drop
        into a 500.
        """
        if not self.is_postgres:
            return False
        if isinstance(exc, (self._psycopg.OperationalError,
                            self._psycopg.InterfaceError,
                            self._psycopg.errors.IdleInTransactionSessionTimeout,
                            self._psycopg.errors.AdminShutdown)):
            return True
        return bool(getattr(self._conn, "closed", False))

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — it is already unusable
            pass
        self._connect()

    # ------------------------------------------------------------ statements
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Rows:
        if not self.is_postgres:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall() if cursor.description else []
            return _Rows(rows, cursor.rowcount)

        statement = _to_pg_placeholders(sql, escape_percent=bool(params))
        try:
            return self._execute_pg(statement, params)
        except Exception as exc:  # noqa: BLE001 — re-raised unless recoverable
            if not self._is_dropped(exc):
                raise
            self._reconnect()
            return self._execute_pg(statement, params)

    def _execute_pg(self, statement: str, params: tuple[Any, ...]) -> _Rows:
        with self._conn.cursor() as cursor:
            cursor.execute(statement, params or None)
            rows = cursor.fetchall() if cursor.description else []
            return _Rows(rows, cursor.rowcount)

    def executescript(self, sql: str) -> None:
        """Run several statements. Used only for schema creation."""
        if not self.is_postgres:
            self._conn.executescript(sql)
            return
        # Comments come out first: one of them contains a semicolon, and
        # splitting before stripping would cut it in half and leave the tail
        # of an English sentence being sent to the server as SQL.
        script = _strip_sql_comments(_to_pg_schema(sql))
        for chunk in script.split(";"):
            if chunk.strip():
                self.execute(chunk.strip())

    def commit(self) -> None:
        try:
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            if not self._is_dropped(exc):
                raise
            self._reconnect()

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001 — nothing useful to do about it
            pass

    def close(self) -> None:
        self._conn.close()
