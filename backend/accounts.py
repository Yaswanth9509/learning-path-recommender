"""Accounts and sessions.

Deliberately small: PBKDF2 from `hashlib` for passwords, `secrets` for session
tokens, and the same database that already holds learners — a SQLite file
locally, Postgres once deployed. Nothing here needs an external service, a key,
or a network call beyond the database itself, so the app still runs offline and
deploys as one container.

Recovery works without a mail service: each account carries a question the
holder wrote themselves and an answer hashed like a password. It is a speed
bump rather than authentication — an answer has less entropy than a password —
but it raises the cost from knowing an email address to knowing an email
address and one fact about that person, and unlike a recovery code it is
something people actually remember. There is still no email verification.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

#: Cost factor. High enough that a leaked database is expensive to attack,
#: low enough that signing in stays instant on a free instance.
PBKDF2_ROUNDS = 210_000
SALT_BYTES = 16
TOKEN_BYTES = 32

SESSION_COOKIE = "lpr_session"
SESSION_DAYS = 30

#: A learner is a persona ("me", "my sister"), not an account. Five is plenty
#: for one person and keeps a shared demo instance from being filled by one user.
MAX_LEARNERS_PER_USER = 5

MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_CHARS = 200
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    """Raised for anything a caller did wrong: bad email, weak password, …"""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    display_name: str
    is_guest: bool
    created_at: str


def normalise_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not _EMAIL_RE.match(cleaned) or len(cleaned) > 254:
        raise AuthError("that does not look like an email address")
    return cleaned


def check_password_strength(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_CHARS:
        raise AuthError(
            f"password must be at least {MIN_PASSWORD_CHARS} characters"
        )
    if len(password) > MAX_PASSWORD_CHARS:
        raise AuthError("password is too long")


def hash_password(password: str) -> str:
    """`pbkdf2$rounds$salt$hash`, all hex — self-describing, so the cost factor
    can be raised later without invalidating existing accounts."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    )
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, expected = stored.split("$")
        if algorithm != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def normalise_answer(answer: str) -> str:
    """Fold the differences that should never fail a recall.

    Case, surrounding space and repeated inner spaces are not part of what
    somebody remembers about their own life, so they are not part of the
    secret either.
    """
    return " ".join((answer or "").strip().lower().split())


MIN_ANSWER_CHARS = 2
MAX_QUESTION_CHARS = 160


def check_recovery(question: str, answer: str) -> tuple[str, str]:
    """Validate a question and answer, returning them cleaned."""
    text = (question or "").strip()
    if not text:
        raise AuthError("choose a recovery question")
    if len(text) > MAX_QUESTION_CHARS:
        raise AuthError("that question is too long")
    folded = normalise_answer(answer)
    if len(folded) < MIN_ANSWER_CHARS:
        raise AuthError("your answer is too short to be useful")
    return text, folded


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Accounts:
    """User and session storage, over the application's SQLite connection."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        display_name  TEXT NOT NULL DEFAULT '',
        is_guest      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL,
        -- Account recovery. The question is shown back to whoever is asking,
        -- so it is only ever the account holder's own words; the answer is
        -- hashed exactly like a password, because that is what it is.
        recovery_question TEXT NOT NULL DEFAULT '',
        recovery_answer   TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    """

    def __init__(self, connection, lock) -> None:
        self._conn = connection
        self._lock = lock
        with self._lock:
            self._conn.executescript(self.SCHEMA)
            self._conn.commit()
            self._add_recovery_columns()

    def _add_recovery_columns(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` does not add columns to an existing one."""
        existing = self._user_columns()
        for column in ("recovery_question", "recovery_answer"):
            if column in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE users ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()

    def _user_columns(self) -> set:
        if getattr(self._conn, "is_postgres", False):
            rows = self._conn.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            ).fetchall()
        else:
            rows = self._conn.execute("PRAGMA table_info(users)").fetchall()
        return {row["name"] for row in rows}

    # ----------------------------------------------------------------- users
    def create_user(
        self, email: str, password: str, display_name: str = "",
        is_guest: bool = False, recovery_question: str = "",
        recovery_answer: str = "",
    ) -> User:
        address = normalise_email(email)
        check_password_strength(password)
        # A guest has no password worth resetting and nothing to recover.
        question, folded = "", ""
        if not is_guest:
            question, folded = check_recovery(recovery_question, recovery_answer)
        user = User(
            id=secrets.token_hex(8),
            email=address,
            display_name=(display_name or address.split("@")[0])[:80],
            is_guest=is_guest,
            created_at=_now().isoformat(timespec="seconds"),
        )
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users (id, email, password_hash, display_name, "
                    "is_guest, created_at, recovery_question, recovery_answer) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user.id, user.email, hash_password(password), user.display_name,
                     int(is_guest), user.created_at, question,
                     hash_password(folded) if folded else ""),
                )
                self._conn.commit()
            except self._conn.integrity_error as exc:
                self._conn.rollback()
                raise AuthError("an account with that email already exists") from exc
        return user

    def create_guest(self) -> tuple[User, str]:
        """A throwaway account so someone can try the product immediately."""
        handle = secrets.token_hex(4)
        password = secrets.token_urlsafe(24)
        user = self.create_user(
            f"guest-{handle}@guest.local", password, f"Guest {handle[:4]}", is_guest=True
        )
        return user, password

    def _row_to_user(self, row) -> User:
        return User(
            id=row["id"], email=row["email"], display_name=row["display_name"],
            is_guest=bool(row["is_guest"]), created_at=row["created_at"],
        )

    def authenticate(self, email: str, password: str) -> User:
        address = (email or "").strip().lower()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE email = ?", (address,)
            ).fetchall()
        # Hash regardless, so a missing account and a wrong password take the
        # same time and cannot be told apart by timing.
        stored = rows[0]["password_hash"] if rows else hash_password("no-such-user")
        if not verify_password(password or "", stored) or not rows:
            raise AuthError("email or password is incorrect")
        return self._row_to_user(rows[0])

    def get_user(self, user_id: str) -> Optional[User]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchall()
        return self._row_to_user(rows[0]) if rows else None

    def upgrade_guest(self, user_id: str, email: str, password: str) -> User:
        """Turn a throwaway account into a real one, in place.

        The user id never changes, so every learner, path and progress row that
        already points at it stays exactly where it is. That is the whole point:
        someone who tried the product as a guest keeps their work.
        """
        address = normalise_email(email)
        check_password_strength(password)
        with self._lock:
            rows = self._conn.execute(
                "SELECT is_guest FROM users WHERE id = ?", (user_id,)
            ).fetchall()
            if not rows:
                raise AuthError("no such account")
            if not rows[0]["is_guest"]:
                raise AuthError("this account is already registered")
            try:
                self._conn.execute(
                    "UPDATE users SET email = ?, password_hash = ?, is_guest = 0, "
                    "display_name = ? WHERE id = ?",
                    (address, hash_password(password), address.split("@")[0][:80],
                     user_id),
                )
                self._conn.commit()
            except self._conn.integrity_error as exc:
                self._conn.rollback()
                raise AuthError("an account with that email already exists") from exc
        user = self.get_user(user_id)
        assert user is not None
        return user

    def set_display_name(self, user_id: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET display_name = ? WHERE id = ?",
                ((name or "").strip()[:80], user_id),
            )
            self._conn.commit()

    def recovery_question_for(self, email: str) -> str:
        """The question to put back to whoever is asking, or "" if there is none."""
        address = (email or "").strip().lower()
        with self._lock:
            rows = self._conn.execute(
                "SELECT recovery_question, is_guest FROM users WHERE email = ?",
                (address,),
            ).fetchall()
        if not rows or rows[0]["is_guest"]:
            return ""
        return rows[0]["recovery_question"] or ""

    def reset_password(self, email: str, answer: str, replacement: str) -> User:
        """Set a new password, gated on the account's recovery answer.

        This is not authentication. An answer carries far less entropy than a
        password and somebody who knows the person could guess it. What it does
        is raise the cost from "know an email address" to "know an email
        address and one fact about them", which is proportionate to what this
        application holds. The caller rate limits it — see `RECOVERY_PATHS` in
        security.py.
        """
        address = normalise_email(email)
        check_password_strength(replacement)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, is_guest, recovery_answer FROM users WHERE email = ?",
                (address,),
            ).fetchall()

        # Hash either way, so a missing account and a wrong answer take the
        # same time and cannot be told apart by timing.
        stored = rows[0]["recovery_answer"] if rows else hash_password("no-such-user")
        correct = verify_password(normalise_answer(answer), stored) if stored else False
        if not rows or rows[0]["is_guest"] or not stored or not correct:
            raise AuthError("that answer does not match our records")

        with self._lock:
            self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(replacement), rows[0]["id"]),
            )
            self._conn.commit()
        user = self.get_user(rows[0]["id"])
        assert user is not None
        return user

    def set_recovery(self, user_id: str, question: str, answer: str) -> None:
        """Add or change the recovery question on an existing account."""
        text, folded = check_recovery(question, answer)
        with self._lock:
            self._conn.execute(
                "UPDATE users SET recovery_question = ?, recovery_answer = ? "
                "WHERE id = ?",
                (text, hash_password(folded), user_id),
            )
            self._conn.commit()

    def change_password(self, user_id: str, current: str, replacement: str) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchall()
        if not rows or not verify_password(current or "", rows[0]["password_hash"]):
            raise AuthError("current password is incorrect")
        check_password_strength(replacement)
        with self._lock:
            self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(replacement), user_id),
            )
            self._conn.commit()

    # -------------------------------------------------------------- sessions
    def start_session(self, user_id: str) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = _now()
        expires = now + timedelta(days=SESSION_DAYS)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, user_id, now.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds")),
            )
            self._conn.commit()
        return token, expires

    def user_for_session(self, token: str) -> Optional[User]:
        if not token:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,)
            ).fetchall()
        if not rows:
            return None
        try:
            expires = datetime.fromisoformat(rows[0]["expires_at"])
        except ValueError:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= _now():
            self.end_session(token)
            return None
        return self.get_user(rows[0]["user_id"])

    def end_session(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._conn.commit()

    def purge_expired(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (_now().isoformat(timespec="seconds"),),
            )
            self._conn.commit()
        return cursor.rowcount
