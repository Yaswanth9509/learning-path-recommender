"""Persistence, over SQLite by default and Postgres when deployed.

With no `DATABASE_URL` this is what it always was: stdlib `sqlite3`, the whole
application state in one file that can be deleted to reset. Set `DATABASE_URL`
and the same SQL runs against Postgres instead, because a deployed instance
with no persistent disk would otherwise lose every account when it sleeps. The
differences between the two engines live in `sqlstore.py`, not here.

Rich objects are stored as JSON blobs because they are always read and written
whole.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .accounts import Accounts
from .models import (
    FeedbackEntry,
    LearnerProfile,
    LearningPath,
    ProgressEntry,
    utcnow,
)
from .sqlstore import Store, database_url

DEFAULT_DB_PATH = Path(__file__).parent / "learning.db"

_SCHEMA = """
-- `user_id` is empty for learners created before accounts existed; they are
-- adopted by the first account that signs in on this instance.
CREATE TABLE IF NOT EXISTS profiles (
    learner_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL DEFAULT '',
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id);
-- One row per goal a learner is pursuing: a learner can run several roadmaps
-- side by side, with one of them active at a time.
CREATE TABLE IF NOT EXISTS paths (
    learner_id TEXT NOT NULL,
    goal_id    TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (learner_id, goal_id)
);
CREATE TABLE IF NOT EXISTS progress (
    learner_id TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (learner_id, item_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    signal     TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_progress_learner ON progress(learner_id);
CREATE INDEX IF NOT EXISTS idx_feedback_learner ON feedback(learner_id);
CREATE INDEX IF NOT EXISTS idx_conversation_learner ON conversation(learner_id);
"""


class Database:
    """Thread-safe wrapper around one database connection."""

    def __init__(
        self, path: Optional[Path | str] = None, url: Optional[str] = None
    ) -> None:
        # An explicit path is a caller asking for a file, so it wins over the
        # environment — that is how the tests keep using SQLite regardless.
        self.url = "" if path is not None else (
            url if url is not None else database_url()
        )
        env_path = os.environ.get("LEARNING_DB_PATH")
        self.path = None if self.url else Path(path or env_path or DEFAULT_DB_PATH)
        self._lock = threading.Lock()
        if self.path is not None and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection behind an explicit lock: FastAPI serves requests from
        # a thread pool, and every write here is short.
        self._conn = Store(self.path, self.url)
        with self._lock:
            self._migrate()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        #: Users and sessions live in the same database as everything else.
        self.accounts = Accounts(self._conn, self._lock)

    @property
    def is_postgres(self) -> bool:
        return self._conn.is_postgres

    def _migrate(self) -> None:
        """Bring an older database up to the current schema.

        The only change so far: `paths` used to hold one row per learner. It now
        holds one per goal, so a learner can run several roadmaps at once. The
        single existing path is carried over under its own goal.

        Postgres support arrived after both changes, so no Postgres database
        can predate them and there is nothing to migrate.
        """
        if self._conn.is_postgres:
            return
        tables = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "profiles" in tables:
            profile_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(profiles)")
            }
            if "user_id" not in profile_columns:
                self._conn.execute(
                    "ALTER TABLE profiles ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()

        if "paths" not in tables:
            return
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(paths)")
        }
        if "goal_id" in columns:
            return

        legacy = self._conn.execute("SELECT learner_id, data, updated_at FROM paths")
        rows = [
            (r["learner_id"], json.loads(r["data"]).get("goal_id", ""), r["data"],
             r["updated_at"])
            for r in legacy
        ]
        self._conn.execute("DROP TABLE paths")
        self._conn.execute(
            "CREATE TABLE paths (learner_id TEXT NOT NULL, goal_id TEXT NOT NULL, "
            "data TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY (learner_id, goal_id))"
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO paths (learner_id, goal_id, data, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [row for row in rows if row[1]],
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- internals
    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -------------------------------------------------------------- profiles
    def save_profile(
        self, profile: LearnerProfile, user_id: str = ""
    ) -> LearnerProfile:
        profile.updated_at = utcnow()
        self._write(
            "INSERT INTO profiles (learner_id, user_id, data, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(learner_id) DO UPDATE SET "
            "data=excluded.data, updated_at=excluded.updated_at, "
            "user_id=CASE WHEN excluded.user_id != '' THEN excluded.user_id "
            "ELSE profiles.user_id END",
            (profile.learner_id, user_id, profile.model_dump_json(),
             profile.updated_at),
        )
        return profile

    def owner_of(self, learner_id: str) -> Optional[str]:
        rows = self._query(
            "SELECT user_id FROM profiles WHERE learner_id = ?", (learner_id,)
        )
        return rows[0]["user_id"] if rows else None

    def count_learners(self, user_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM profiles WHERE user_id = ?", (user_id,)
        )
        return int(rows[0]["n"]) if rows else 0

    def adopt_orphan_learners(self, user_id: str) -> int:
        """Give every unowned learner to `user_id`.

        A one-time migration for a database that predates accounts. It is not
        safe to run automatically on a shared instance — an unowned learner is
        just as likely to be an anonymous visitor's as it is to be a leftover —
        so the caller opts in explicitly.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE profiles SET user_id = ? WHERE user_id = ''", (user_id,)
            )
            self._conn.commit()
        return cursor.rowcount

    def get_profile(self, learner_id: str) -> Optional[LearnerProfile]:
        rows = self._query(
            "SELECT data FROM profiles WHERE learner_id = ?", (learner_id,)
        )
        if not rows:
            return None
        return LearnerProfile.model_validate_json(rows[0]["data"])

    def list_profiles(self, user_id: Optional[str] = None) -> list[LearnerProfile]:
        """Every learner, or only one user's — never another user's."""
        if user_id is None:
            rows = self._query("SELECT data FROM profiles ORDER BY updated_at DESC")
        else:
            rows = self._query(
                "SELECT data FROM profiles WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            )
        return [LearnerProfile.model_validate_json(r["data"]) for r in rows]

    def delete_learner(self, learner_id: str) -> None:
        for table in ("profiles", "paths", "progress", "feedback", "conversation"):
            self._write(f"DELETE FROM {table} WHERE learner_id = ?", (learner_id,))

    # ----------------------------------------------------------------- paths
    def save_path(self, path: LearningPath) -> LearningPath:
        self._write(
            "INSERT INTO paths (learner_id, goal_id, data, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(learner_id, goal_id) DO UPDATE SET "
            "data=excluded.data, updated_at=excluded.updated_at",
            (path.learner_id, path.goal_id, path.model_dump_json(), utcnow()),
        )
        return path

    def get_path(
        self, learner_id: str, goal_id: Optional[str] = None
    ) -> Optional[LearningPath]:
        """The roadmap for one goal, or the most recently touched one."""
        if goal_id is None:
            rows = self._query(
                "SELECT data FROM paths WHERE learner_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (learner_id,),
            )
        else:
            rows = self._query(
                "SELECT data FROM paths WHERE learner_id = ? AND goal_id = ?",
                (learner_id, goal_id),
            )
        if not rows:
            return None
        return LearningPath.model_validate_json(rows[0]["data"])

    def list_paths(self, learner_id: str) -> list[LearningPath]:
        rows = self._query(
            "SELECT data FROM paths WHERE learner_id = ? ORDER BY updated_at ASC",
            (learner_id,),
        )
        return [LearningPath.model_validate_json(r["data"]) for r in rows]

    def delete_path(self, learner_id: str, goal_id: str) -> None:
        self._write(
            "DELETE FROM paths WHERE learner_id = ? AND goal_id = ?",
            (learner_id, goal_id),
        )

    # -------------------------------------------------------------- progress
    def set_progress(self, learner_id: str, item_id: str, status: str) -> ProgressEntry:
        entry = ProgressEntry(item_id=item_id, status=status)
        self._write(
            "INSERT INTO progress (learner_id, item_id, status, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(learner_id, item_id) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at",
            (learner_id, item_id, status, entry.updated_at),
        )
        return entry

    def get_progress(self, learner_id: str) -> dict[str, ProgressEntry]:
        rows = self._query(
            "SELECT item_id, status, updated_at FROM progress WHERE learner_id = ?",
            (learner_id,),
        )
        return {
            r["item_id"]: ProgressEntry(
                item_id=r["item_id"], status=r["status"], updated_at=r["updated_at"]
            )
            for r in rows
        }

    def completed_items(self, learner_id: str) -> list[str]:
        rows = self._query(
            "SELECT item_id FROM progress WHERE learner_id = ? AND status = 'completed'",
            (learner_id,),
        )
        return [r["item_id"] for r in rows]

    # -------------------------------------------------------------- feedback
    def add_feedback(self, learner_id: str, entry: FeedbackEntry) -> FeedbackEntry:
        self._write(
            "INSERT INTO feedback (learner_id, item_id, signal, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (learner_id, entry.item_id, entry.signal, entry.note, entry.created_at),
        )
        return entry

    def get_feedback(self, learner_id: str) -> list[FeedbackEntry]:
        rows = self._query(
            "SELECT item_id, signal, note, created_at FROM feedback "
            "WHERE learner_id = ? ORDER BY id ASC",
            (learner_id,),
        )
        return [
            FeedbackEntry(
                item_id=r["item_id"],
                signal=r["signal"],
                note=r["note"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ---------------------------------------------------------- conversation
    def add_message(self, learner_id: str, role: str, content: str) -> None:
        self._write(
            "INSERT INTO conversation (learner_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (learner_id, role, content, utcnow()),
        )

    def get_conversation(self, learner_id: str, limit: int = 20) -> list[dict[str, str]]:
        rows = self._query(
            "SELECT role, content FROM conversation WHERE learner_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (learner_id, limit),
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_conversation(self, learner_id: str) -> None:
        self._write("DELETE FROM conversation WHERE learner_id = ?", (learner_id,))


_db: Optional[Database] = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database()
    return _db


def reset_db_singleton(db: Optional[Database] = None) -> None:
    """Test hook: swap the process-wide database."""
    global _db
    with _db_lock:
        _db = db
