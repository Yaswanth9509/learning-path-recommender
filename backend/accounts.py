"""Accounts and sessions.

Deliberately small: PBKDF2 from `hashlib` for passwords, `secrets` for session
tokens, and the same database that already holds learners — a SQLite file
locally, Postgres once deployed. Nothing here needs an external service, a key,
or a network call beyond the database itself, so the app still runs offline and
deploys as one container.

Recovery is by emailed link. An account that has lost its password proves
control of its mailbox instead of answering a question about itself, which is
both stronger and the only mechanism that works for someone who never set a
question up. See `mailer.py` for the transport and `begin_password_reset`
below for the token.
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

#: Long enough to walk to a phone and find the mail, short enough that a link
#: sitting in an inbox months later is worthless.
RESET_TTL_MINUTES = 30

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


def hash_token(token: str) -> str:
    """A plain SHA-256, deliberately, where passwords get PBKDF2.

    Key stretching defends a secret that a human chose and an attacker can
    guess. A reset token is 32 random bytes from `secrets`, so there is nothing
    to guess and stretching would only buy latency. It also has to be looked up
    by value, which a per-row salt makes impossible.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
        created_at    TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    -- Password reset links. Only the hash of the token is kept, for the same
    -- reason passwords are: anyone who reads this table must not be able to
    -- take over an account with what they find. `used_at` makes a link
    -- single-use without deleting the evidence that it was spent.
    CREATE TABLE IF NOT EXISTS password_resets (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at    TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id);
    """

    def __init__(self, connection, lock) -> None:
        self._conn = connection
        self._lock = lock
        with self._lock:
            self._conn.executescript(self.SCHEMA)
            self._conn.commit()

    # ----------------------------------------------------------------- users
    def create_user(
        self, email: str, password: str, display_name: str = "",
        is_guest: bool = False,
    ) -> User:
        address = normalise_email(email)
        check_password_strength(password)
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
                    "is_guest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user.id, user.email, hash_password(password), user.display_name,
                     int(is_guest), user.created_at),
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

    # -------------------------------------------------------- reset by email
    def begin_password_reset(self, email: str) -> Optional[tuple[User, str]]:
        """Issue a reset token, or None when there is nothing to reset.

        Returns the raw token, which is the only moment it exists in readable
        form — the caller mails it and forgets it. None covers an unknown
        address and a guest alike; the endpoint answers identically either way,
        so the distinction never leaves this method.
        """
        address = normalise_email(email)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE email = ?", (address,)
            ).fetchall()
        if not rows or rows[0]["is_guest"]:
            return None
        user = self._row_to_user(rows[0])

        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = _now()
        with self._lock:
            # One live link per account: asking again silently retires the
            # previous mail, so an older message cannot be replayed later.
            self._conn.execute(
                "DELETE FROM password_resets WHERE user_id = ? AND used_at = ''",
                (user.id,),
            )
            self._conn.execute(
                "INSERT INTO password_resets "
                "(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (hash_token(token), user.id,
                 now.isoformat(timespec="seconds"),
                 (now + timedelta(minutes=RESET_TTL_MINUTES))
                 .isoformat(timespec="seconds")),
            )
            self._conn.commit()
        return user, token

    def complete_password_reset(self, token: str, replacement: str) -> User:
        """Spend a reset token and set the new password.

        Every existing session ends with it. Somebody resetting a password has
        either lost control of the account or lost the password; in both cases
        the sessions already out there are the thing you want gone.
        """
        check_password_strength(replacement)
        digest = hash_token((token or "").strip())
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, expires_at, used_at FROM password_resets "
                "WHERE token_hash = ?", (digest,)
            ).fetchall()
        if not rows or rows[0]["used_at"]:
            raise AuthError("that reset link is no longer valid")
        try:
            expires = datetime.fromisoformat(rows[0]["expires_at"])
        except ValueError as exc:
            raise AuthError("that reset link is no longer valid") from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= _now():
            raise AuthError("that reset link has expired — ask for a new one")

        user_id = rows[0]["user_id"]
        with self._lock:
            self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(replacement), user_id),
            )
            self._conn.execute(
                "UPDATE password_resets SET used_at = ? WHERE token_hash = ?",
                (_now().isoformat(timespec="seconds"), digest),
            )
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._conn.commit()
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("that reset link is no longer valid")
        return user

    def purge_expired_resets(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM password_resets WHERE expires_at <= ?",
                (_now().isoformat(timespec="seconds"),),
            )
            self._conn.commit()
        return cursor.rowcount or 0

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
