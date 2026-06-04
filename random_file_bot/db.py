from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import ForceSubChat, IndexedFile, MembershipEvent, UserStats


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db_time(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


def from_db_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self, owner_id: int) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    is_blocked INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_request_at TEXT,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    refresh_count INTEGER NOT NULL DEFAULT 0,
                    denied_count INTEGER NOT NULL DEFAULT 0,
                    files_sent INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS indexed_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL UNIQUE,
                    file_type TEXT NOT NULL DEFAULT 'document',
                    label TEXT,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_db_id INTEGER,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(file_db_id) REFERENCES indexed_files(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS force_sub_chats (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT,
                    invite_link TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sudo_users (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS membership_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_file_events_user_time
                    ON file_events (user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_file_events_type_time
                    ON file_events (event_type, created_at);
                CREATE INDEX IF NOT EXISTS idx_membership_events_time
                    ON membership_events (created_at);
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sudo_users (user_id, added_by, created_at)
                VALUES (?, ?, ?)
                """,
                (owner_id, owner_id, to_db_time()),
            )

    def upsert_user(self, user, *, blocked: bool = False) -> None:
        now = to_db_time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, first_name, last_name, is_bot, is_blocked,
                    first_seen, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    is_bot = excluded.is_bot,
                    is_blocked = excluded.is_blocked,
                    last_seen = excluded.last_seen
                """,
                (
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                    int(user.is_bot),
                    int(blocked),
                    now,
                    now,
                ),
            )

    def mark_user_blocked(self, user_id: int, blocked: bool = True) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET is_blocked = ?, last_seen = ? WHERE user_id = ?",
                (int(blocked), to_db_time(), user_id),
            )

    def is_sudo(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sudo_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def add_sudo(self, user_id: int, added_by: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sudo_users (user_id, added_by, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, added_by, to_db_time()),
            )

    def remove_sudo(self, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM sudo_users WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    def list_sudos(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT user_id FROM sudo_users ORDER BY user_id").fetchall()
            return [int(row["user_id"]) for row in rows]

    def add_file(self, file_id: str, file_type: str, label: str | None, added_by: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO indexed_files (file_id, file_type, label, added_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    file_type = excluded.file_type,
                    label = COALESCE(excluded.label, indexed_files.label)
                """,
                (file_id, file_type, label, added_by, to_db_time()),
            )

    def import_files(
        self,
        files: list[tuple[str, str, str | None]],
        *,
        added_by: int,
        replace: bool = False,
    ) -> int:
        now = to_db_time()
        with self.connect() as conn:
            if replace:
                conn.execute("DELETE FROM indexed_files")
            conn.executemany(
                """
                INSERT INTO indexed_files (file_id, file_type, label, added_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    file_type = excluded.file_type,
                    label = COALESCE(excluded.label, indexed_files.label)
                """,
                [
                    (file_id, file_type, label, added_by, now)
                    for file_id, file_type, label in files
                ],
            )
            return len(files)

    def remove_file(self, file_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM indexed_files WHERE file_id = ?", (file_id,))
            return cursor.rowcount > 0

    def update_file_type(self, file_db_id: int, file_type: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE indexed_files SET file_type = ? WHERE id = ?",
                (file_type, file_db_id),
            )

    def random_file(self, *, exclude_id: int | None = None) -> IndexedFile | None:
        with self.connect() as conn:
            if exclude_id is not None:
                row = conn.execute(
                    """
                    SELECT *
                    FROM indexed_files
                    WHERE id != ?
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    (exclude_id,),
                ).fetchone()
                if row:
                    return self._file_from_row(row)
            row = conn.execute("SELECT * FROM indexed_files ORDER BY RANDOM() LIMIT 1").fetchone()
            return self._file_from_row(row) if row else None

    def count_files(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM indexed_files").fetchone()
            return int(row["count"])

    def recent_files(self, limit: int = 10) -> list[IndexedFile]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM indexed_files ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._file_from_row(row) for row in rows]

    def add_force_sub_chat(
        self,
        chat_id: str,
        title: str | None,
        invite_link: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO force_sub_chats (chat_id, title, invite_link, enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    invite_link = excluded.invite_link,
                    enabled = 1
                """,
                (chat_id, title, invite_link, to_db_time()),
            )

    def remove_force_sub_chat(self, chat_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM force_sub_chats WHERE chat_id = ?",
                (chat_id,),
            )
            return cursor.rowcount > 0

    def list_force_sub_chats(self, *, enabled_only: bool = True) -> list[ForceSubChat]:
        query = "SELECT * FROM force_sub_chats"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at ASC"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
            return [self._force_sub_from_row(row) for row in rows]

    def record_file_event(self, user_id: int, file_db_id: int | None, event_type: str) -> None:
        request_increment = 1 if event_type in {"request", "refresh"} else 0
        refresh_increment = 1 if event_type == "refresh" else 0
        sent_increment = 1 if event_type in {"request", "refresh"} and file_db_id else 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO file_events (user_id, file_db_id, event_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, file_db_id, event_type, to_db_time()),
            )
            conn.execute(
                """
                UPDATE users
                SET request_count = request_count + ?,
                    refresh_count = refresh_count + ?,
                    files_sent = files_sent + ?,
                    last_request_at = ?,
                    last_seen = ?
                WHERE user_id = ?
                """,
                (
                    request_increment,
                    refresh_increment,
                    sent_increment,
                    to_db_time(),
                    to_db_time(),
                    user_id,
                ),
            )

    def record_denied(self, user_id: int, event_type: str = "denied") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO file_events (user_id, file_db_id, event_type, created_at)
                VALUES (?, NULL, ?, ?)
                """,
                (user_id, event_type, to_db_time()),
            )
            conn.execute(
                """
                UPDATE users
                SET denied_count = denied_count + 1,
                    last_seen = ?
                WHERE user_id = ?
                """,
                (to_db_time(), user_id),
            )

    def requests_since(self, user_id: int, since: datetime) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM file_events
                WHERE user_id = ?
                  AND event_type IN ('request', 'refresh')
                  AND created_at >= ?
                """,
                (user_id, to_db_time(since)),
            ).fetchone()
            return int(row["count"])

    def record_membership_event(self, user_id: int, chat_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO membership_events (user_id, chat_id, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, chat_id, status, to_db_time()),
            )

    def total_stats(self, window_minutes: int = 60) -> dict[str, int]:
        since = utcnow() - timedelta(minutes=window_minutes)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM users) AS users,
                    (SELECT COUNT(*) FROM users WHERE is_blocked = 1) AS blocked,
                    (SELECT COUNT(*) FROM indexed_files) AS files,
                    (SELECT COUNT(*) FROM force_sub_chats WHERE enabled = 1) AS fsubs,
                    (SELECT COUNT(*) FROM file_events WHERE event_type IN ('request', 'refresh')) AS requests,
                    (SELECT COUNT(*) FROM file_events WHERE event_type IN ('request', 'refresh') AND created_at >= ?) AS recent_requests,
                    (SELECT COUNT(*) FROM file_events WHERE event_type LIKE 'denied%') AS denied
                """,
                (to_db_time(since),),
            ).fetchone()
            return {key: int(row[key]) for key in row.keys()}

    def top_users(self, limit: int = 10) -> list[UserStats]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM users
                ORDER BY request_count DESC, last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._user_from_row(row) for row in rows]

    def recent_users(self, limit: int = 10) -> list[UserStats]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM users
                ORDER BY first_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._user_from_row(row) for row in rows]

    def blocked_users(self, limit: int = 10) -> list[UserStats]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM users
                WHERE is_blocked = 1
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._user_from_row(row) for row in rows]

    def get_user_stats(self, user_id: int) -> UserStats | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return self._user_from_row(row) if row else None

    def recent_membership_events(self, limit: int = 15) -> list[MembershipEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, chat_id, status, created_at
                FROM membership_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                MembershipEvent(
                    user_id=int(row["user_id"]),
                    chat_id=str(row["chat_id"]),
                    status=str(row["status"]),
                    created_at=from_db_time(row["created_at"]) or utcnow(),
                )
                for row in rows
            ]

    def active_user_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM users WHERE is_blocked = 0 ORDER BY user_id"
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def _file_from_row(self, row: sqlite3.Row) -> IndexedFile:
        return IndexedFile(
            id=int(row["id"]),
            file_id=str(row["file_id"]),
            file_type=str(row["file_type"]),
            label=row["label"],
            added_by=int(row["added_by"]),
            created_at=from_db_time(row["created_at"]) or utcnow(),
        )

    def _force_sub_from_row(self, row: sqlite3.Row) -> ForceSubChat:
        return ForceSubChat(
            chat_id=str(row["chat_id"]),
            title=row["title"],
            invite_link=row["invite_link"],
            enabled=bool(row["enabled"]),
            created_at=from_db_time(row["created_at"]) or utcnow(),
        )

    def _user_from_row(self, row: sqlite3.Row) -> UserStats:
        return UserStats(
            user_id=int(row["user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            is_blocked=bool(row["is_blocked"]),
            request_count=int(row["request_count"]),
            refresh_count=int(row["refresh_count"]),
            denied_count=int(row["denied_count"]),
            files_sent=int(row["files_sent"]),
            first_seen=from_db_time(row["first_seen"]) or utcnow(),
            last_seen=from_db_time(row["last_seen"]) or utcnow(),
            last_request_at=from_db_time(row["last_request_at"]),
        )
