"""SQLite persistence: per-chat scan checkpoints + a record of everything saved.

Two jobs:
1. Remember how far each chat has been scanned (message id **and** timestamp),
   so a restart continues exactly where it stopped and never saves a post twice.
2. Keep an audit trail of what was matched and when, so you can review locally
   without digging through Telegram.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id           INTEGER PRIMARY KEY,
    title             TEXT,
    last_message_id   INTEGER NOT NULL DEFAULT 0,
    last_scanned_date TEXT,
    updated_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    chat_title   TEXT,
    message_id   INTEGER NOT NULL,
    message_date TEXT,
    matched      TEXT,
    mode         TEXT,
    link         TEXT,
    deadline     TEXT,
    saved_at     TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_chat ON saved_messages (chat_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_db(dt: Optional[datetime]) -> Optional[str]:
    """Canonical UTC ISO string; lexicographic order == chronological order."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_db(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, path: str | Path = "jobsaver.sqlite3"):
        self.path = Path(path).expanduser()
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (safe on old DBs)."""
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(chat_state)")
        }
        if "last_scanned_date" not in columns:
            self._conn.execute("ALTER TABLE chat_state ADD COLUMN last_scanned_date TEXT")

        saved_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(saved_messages)")
        }
        if "deadline" not in saved_columns:
            self._conn.execute("ALTER TABLE saved_messages ADD COLUMN deadline TEXT")

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        finally:
            cursor.close()

    # ------------------------------------------------------------------ #
    # Scan checkpoints
    # ------------------------------------------------------------------ #
    def get_last_message_id(self, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT last_message_id FROM chat_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return int(row["last_message_id"]) if row else 0

    def get_checkpoint(self, chat_id: int) -> Optional[datetime]:
        """Timestamp of the newest message already scanned in this chat."""
        row = self._conn.execute(
            "SELECT last_scanned_date FROM chat_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return from_db(row["last_scanned_date"]) if row else None

    def set_checkpoint(
        self,
        chat_id: int,
        *,
        title: Optional[str],
        scanned_until: Optional[datetime],
        last_message_id: int,
    ) -> None:
        """Advance the checkpoint. Never moves backwards on either field."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO chat_state
                    (chat_id, title, last_message_id, last_scanned_date, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (chat_id) DO UPDATE SET
                    last_message_id = MAX(chat_state.last_message_id, excluded.last_message_id),
                    last_scanned_date = CASE
                        WHEN excluded.last_scanned_date IS NULL
                            THEN chat_state.last_scanned_date
                        WHEN chat_state.last_scanned_date IS NULL
                            THEN excluded.last_scanned_date
                        WHEN excluded.last_scanned_date > chat_state.last_scanned_date
                            THEN excluded.last_scanned_date
                        ELSE chat_state.last_scanned_date
                    END,
                    title      = COALESCE(excluded.title, chat_state.title),
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, last_message_id, to_db(scanned_until), _now()),
            )

    def set_last_message_id(self, chat_id: int, last_message_id: int, title: str | None) -> None:
        """Backwards-compatible helper (advances the id cursor only)."""
        self.set_checkpoint(
            chat_id, title=title, scanned_until=None, last_message_id=last_message_id
        )

    def reset_checkpoints(self, chat_id: Optional[int] = None) -> int:
        """Forget scan progress (keeps the saved-message history)."""
        with self._cursor() as cursor:
            if chat_id is None:
                cursor.execute(
                    "DELETE FROM chat_state"
                )
            else:
                cursor.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
            return cursor.rowcount

    def list_checkpoints(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM chat_state ORDER BY COALESCE(last_scanned_date, '') DESC"
            ).fetchall()
        )

    # ------------------------------------------------------------------ #
    # Saved messages
    # ------------------------------------------------------------------ #
    def was_saved(self, chat_id: int, message_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM saved_messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
        return row is not None

    def record_saved(
        self,
        *,
        chat_id: int,
        chat_title: str | None,
        message_id: int,
        message_date: str | None,
        matched: list[str] | tuple[str, ...],
        mode: str,
        link: str | None,
        deadline: str | None = None,
        replace: bool = False,
    ) -> bool:
        """Insert one row. Returns False if it was already recorded (and not replaced).

        When ``replace`` is True, any existing row for this
        ``(chat_id, message_id)`` is deleted first so the new write gets a fresh
        ``saved_at`` timestamp.
        """
        with self._cursor() as cursor:
            if replace:
                cursor.execute(
                    "DELETE FROM saved_messages WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                )
            cursor.execute(
                """
                INSERT OR IGNORE INTO saved_messages
                    (chat_id, chat_title, message_id, message_date, matched, mode, link, deadline, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    chat_title,
                    message_id,
                    message_date,
                    ", ".join(matched),
                    mode,
                    link,
                    deadline,
                    _now(),
                ),
            )
            return cursor.rowcount > 0

    def count_saved(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM saved_messages").fetchone()
        return int(row["n"]) if row else 0

    def delete_saved(self, chat_id: int, message_id: int) -> bool:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM saved_messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
            return cursor.rowcount > 0

    def review_jobs(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM saved_messages ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline, message_date DESC, id"))

    def recent_saved(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM saved_messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )
