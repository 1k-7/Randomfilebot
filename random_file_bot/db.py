from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copy2
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
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
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
                    mode TEXT NOT NULL DEFAULT 'member',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sudo_users (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS privileged_users (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_bonuses (
                    user_id INTEGER PRIMARY KEY,
                    bonus_requests INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    referred_id INTEGER PRIMARY KEY,
                    referrer_id INTEGER NOT NULL,
                    bonus_awarded INTEGER NOT NULL,
                    fulfilled INTEGER NOT NULL DEFAULT 0,
                    fulfilled_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );

                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    bonus_requests INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promo_redemptions (
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    redeemed_at TEXT NOT NULL,
                    PRIMARY KEY (code, user_id),
                    FOREIGN KEY(code) REFERENCES promo_codes(code) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS active_file_messages (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE INDEX IF NOT EXISTS idx_users_first_seen
                    ON users (first_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_users_last_seen
                    ON users (last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_indexed_files_type_id
                    ON indexed_files (file_type, id);
                CREATE INDEX IF NOT EXISTS idx_referrals_referrer
                    ON referrals (referrer_id);
                CREATE INDEX IF NOT EXISTS idx_referrals_pending
                    ON referrals (referred_id, fulfilled);
                CREATE INDEX IF NOT EXISTS idx_promo_expires
                    ON promo_codes (expires_at);
                """
            )
            self._ensure_column(
                conn,
                "force_sub_chats",
                "mode",
                "TEXT NOT NULL DEFAULT 'member'",
            )
            self._ensure_column(
                conn,
                "referrals",
                "fulfilled",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                "referrals",
                "fulfilled_at",
                "TEXT",
            )
            conn.execute(
                "UPDATE referrals SET fulfilled = 1 WHERE bonus_awarded > 0 AND fulfilled = 0"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sudo_users (user_id, added_by, created_at)
                VALUES (?, ?, ?)
                """,
                (owner_id, owner_id, to_db_time()),
            )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def backup_to(self, destination_path: str | Path) -> None:
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            destination = sqlite3.connect(destination_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()

    def restore_from(self, source_path: str | Path) -> None:
        source_path = Path(source_path)
        backup_path = self.path.with_suffix(self.path.suffix + ".pre-import")
        with sqlite3.connect(source_path) as candidate:
            integrity = candidate.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise ValueError("SQLite integrity check failed")
            required = {"users", "indexed_files", "file_events", "bot_settings"}
            rows = candidate.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            present = {str(row[0]) for row in rows}
            missing = required - present
            if missing:
                raise ValueError(f"Missing required tables: {', '.join(sorted(missing))}")
        if self.path.exists():
            copy2(self.path, backup_path)
        with sqlite3.connect(source_path) as source:
            destination = sqlite3.connect(self.path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, to_db_time()),
            )

    def single_file_mode_enabled(self) -> bool:
        return self.get_setting("single_file_mode", "0") == "1"

    def set_single_file_mode(self, enabled: bool) -> None:
        self.set_setting("single_file_mode", "1" if enabled else "0")

    def maintenance_mode_enabled(self) -> bool:
        return self.get_setting("maintenance_mode", "0") == "1"

    def set_maintenance_mode(self, enabled: bool) -> None:
        self.set_setting("maintenance_mode", "1" if enabled else "0")

    def spoiler_mode_enabled(self) -> bool:
        return self.get_setting("spoiler_mode", "0") == "1"

    def set_spoiler_mode(self, enabled: bool) -> None:
        self.set_setting("spoiler_mode", "1" if enabled else "0")

    def protect_content_enabled(self) -> bool:
        return self.get_setting("protect_content_mode", "0") == "1"

    def set_protect_content(self, enabled: bool) -> None:
        self.set_setting("protect_content_mode", "1" if enabled else "0")

    def get_user_setting(self, user_id: int, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
            return str(row["value"]) if row else default

    def set_user_setting(self, user_id: int, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (user_id, key, value, to_db_time()),
            )

    def user_spoiler_enabled(self, user_id: int) -> bool:
        return self.get_user_setting(user_id, "spoiler_mode", "0") == "1"

    def set_user_spoiler(self, user_id: int, enabled: bool) -> None:
        self.set_user_setting(user_id, "spoiler_mode", "1" if enabled else "0")

    def referral_mode_enabled(self) -> bool:
        return self.get_setting("referral_mode", "0") == "1"

    def set_referral_mode(self, enabled: bool) -> None:
        self.set_setting("referral_mode", "1" if enabled else "0")

    def referral_bonus(self) -> int:
        value = self.get_setting("referral_bonus_requests", "5") or "5"
        try:
            return max(0, int(value))
        except ValueError:
            return 5

    def set_referral_bonus(self, amount: int) -> None:
        self.set_setting("referral_bonus_requests", str(max(0, amount)))

    def delete_timer_seconds(self) -> int:
        value = self.get_setting("delete_timer_seconds", "0") or "0"
        try:
            return max(0, int(value))
        except ValueError:
            return 0

    def set_delete_timer_seconds(self, seconds: int) -> None:
        self.set_setting("delete_timer_seconds", str(max(0, seconds)))

    def get_active_file_message(self, user_id: int) -> tuple[int, int] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT chat_id, message_id FROM active_file_messages WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            return int(row["chat_id"]), int(row["message_id"])

    def set_active_file_message(self, user_id: int, chat_id: int, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO active_file_messages (user_id, chat_id, message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    message_id = excluded.message_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, chat_id, message_id, to_db_time()),
            )

    def clear_active_file_message(
        self,
        user_id: int,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> None:
        clauses = ["user_id = ?"]
        params: list[object] = [user_id]
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if message_id is not None:
            clauses.append("message_id = ?")
            params.append(message_id)
        with self.connect() as conn:
            conn.execute(
                f"DELETE FROM active_file_messages WHERE {' AND '.join(clauses)}",
                params,
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
        now = to_db_time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (
                    user_id, username, first_name, last_name, is_bot, is_blocked,
                    first_seen, last_seen
                )
                VALUES (?, NULL, NULL, NULL, 0, ?, ?, ?)
                """,
                (user_id, int(blocked), now, now),
            )
            conn.execute(
                "UPDATE users SET is_blocked = ?, last_seen = ? WHERE user_id = ?",
                (int(blocked), now, user_id),
            )

    def ensure_user_record(self, user_id: int) -> None:
        now = to_db_time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (
                    user_id, username, first_name, last_name, is_bot, is_blocked,
                    first_seen, last_seen
                )
                VALUES (?, NULL, NULL, NULL, 0, 0, ?, ?)
                """,
                (user_id, now, now),
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

    def is_privileged(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM privileged_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def add_privileged(self, user_id: int, added_by: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO privileged_users (user_id, added_by, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, added_by, to_db_time()),
            )

    def remove_privileged(self, user_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM privileged_users WHERE user_id = ?",
                (user_id,),
            )
            return cursor.rowcount > 0

    def list_privileged(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM privileged_users ORDER BY user_id"
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def bonus_requests(self, user_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT bonus_requests FROM user_bonuses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["bonus_requests"]) if row else 0

    def add_bonus_requests(self, user_id: int, amount: int) -> int:
        amount = max(0, amount)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_bonuses (user_id, bonus_requests, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    bonus_requests = bonus_requests + excluded.bonus_requests,
                    updated_at = excluded.updated_at
                """,
                (user_id, amount, to_db_time()),
            )
            row = conn.execute(
                "SELECT bonus_requests FROM user_bonuses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["bonus_requests"]) if row else 0

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

    def random_file(
        self,
        *,
        exclude_id: int | None = None,
        file_type: str | None = None,
    ) -> IndexedFile | None:
        clauses: list[str] = []
        params: list[object] = []
        if exclude_id is not None:
            clauses.append("id != ?")
            params.append(exclude_id)
        if file_type is not None:
            clauses.append("file_type = ?")
            params.append(file_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            bounds = conn.execute(
                f"SELECT MIN(id) AS min_id, MAX(id) AS max_id FROM indexed_files {where}",
                params,
            ).fetchone()
            if not bounds or bounds["min_id"] is None or bounds["max_id"] is None:
                return None
            min_id = int(bounds["min_id"])
            max_id = int(bounds["max_id"])
            row = None
            span = max(1, max_id - min_id + 1)
            for _ in range(3):
                pivot = min_id + (abs(conn.execute("SELECT random()").fetchone()[0]) % span)
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM indexed_files
                    {where + ' AND' if where else 'WHERE'} id >= ?
                    ORDER BY id
                    LIMIT 1
                    """,
                    [*params, pivot],
                ).fetchone()
                if row:
                    break
            if not row:
                row = conn.execute(
                    f"SELECT * FROM indexed_files {where} ORDER BY id LIMIT 1",
                    params,
                ).fetchone()
            return self._file_from_row(row) if row else None

    def random_files(self, limit: int = 100) -> list[IndexedFile]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM indexed_files ORDER BY RANDOM() LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._file_from_row(row) for row in rows]

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
        mode: str = "member",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO force_sub_chats (chat_id, title, invite_link, mode, enabled, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    invite_link = excluded.invite_link,
                    mode = excluded.mode,
                    enabled = 1
                """,
                (chat_id, title, invite_link, mode, to_db_time()),
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
        request_increment = 1 if event_type in {"request", "refresh", "inline"} else 0
        refresh_increment = 1 if event_type == "refresh" else 0
        sent_increment = 1 if event_type in {"request", "refresh", "inline"} and file_db_id else 0
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

    def create_referral(self, referrer_id: int, referred_id: int) -> bool:
        if referrer_id == referred_id or not self.referral_mode_enabled():
            return False
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM referrals WHERE referred_id = ?",
                (referred_id,),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """
                INSERT INTO referrals (referred_id, referrer_id, bonus_awarded, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (referred_id, referrer_id, 0, to_db_time()),
            )
            return True

    def fulfill_referral(self, referred_id: int) -> int:
        if not self.referral_mode_enabled():
            return 0
        bonus = self.referral_bonus()
        if bonus <= 0:
            return 0
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT referrer_id
                FROM referrals
                WHERE referred_id = ? AND fulfilled = 0
                """,
                (referred_id,),
            ).fetchone()
            if not row:
                return 0
            referrer_id = int(row["referrer_id"])
            now = to_db_time()
            conn.execute(
                """
                UPDATE referrals
                SET fulfilled = 1,
                    bonus_awarded = ?,
                    fulfilled_at = ?
                WHERE referred_id = ? AND fulfilled = 0
                """,
                (bonus, now, referred_id),
            )
            conn.execute(
                """
                INSERT INTO user_bonuses (user_id, bonus_requests, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    bonus_requests = bonus_requests + excluded.bonus_requests,
                    updated_at = excluded.updated_at
                """,
                (referrer_id, bonus, now),
            )
            return bonus

    def referral_count(self, referrer_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM referrals WHERE referrer_id = ? AND fulfilled = 1",
                (referrer_id,),
            ).fetchone()
            return int(row["count"])

    def create_promo_code(
        self,
        code: str,
        *,
        bonus_requests: int,
        max_uses: int,
        expires_at: datetime | None,
        created_by: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO promo_codes (
                    code, bonus_requests, max_uses, uses, expires_at, created_by, created_at
                )
                VALUES (?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    bonus_requests = excluded.bonus_requests,
                    max_uses = excluded.max_uses,
                    uses = 0,
                    expires_at = excluded.expires_at,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
                """
                ,
                (
                    code,
                    max(0, bonus_requests),
                    max(1, max_uses),
                    to_db_time(expires_at) if expires_at else None,
                    created_by,
                    to_db_time(),
                ),
            )

    def redeem_promo_code(self, code: str, user_id: int) -> tuple[bool, str, int]:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM promo_codes WHERE lower(code) = lower(?)",
                (code,),
            ).fetchone()
            if not row:
                return False, "That promo code does not exist.", 0
            expires_at = from_db_time(row["expires_at"])
            if expires_at and expires_at <= now:
                return False, "That promo code has expired.", 0
            if int(row["uses"]) >= int(row["max_uses"]):
                return False, "That promo code has already been fully redeemed.", 0
            used = conn.execute(
                "SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?",
                (row["code"], user_id),
            ).fetchone()
            if used:
                return False, "You have already redeemed that promo code.", 0
            bonus = int(row["bonus_requests"])
            conn.execute(
                """
                INSERT INTO promo_redemptions (code, user_id, redeemed_at)
                VALUES (?, ?, ?)
                """,
                (row["code"], user_id, to_db_time(now)),
            )
            conn.execute(
                "UPDATE promo_codes SET uses = uses + 1 WHERE code = ?",
                (row["code"],),
            )
            conn.execute(
                """
                INSERT INTO user_bonuses (user_id, bonus_requests, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    bonus_requests = bonus_requests + excluded.bonus_requests,
                    updated_at = excluded.updated_at
                """,
                (user_id, bonus, to_db_time(now)),
            )
            total = conn.execute(
                "SELECT bonus_requests FROM user_bonuses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return True, "Promo code redeemed.", int(total["bonus_requests"]) if total else bonus

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
                  AND event_type IN ('request', 'refresh', 'inline')
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

    def has_join_request(self, user_id: int, chat_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM membership_events
                WHERE user_id = ?
                  AND chat_id = ?
                  AND status = 'join_request'
                LIMIT 1
                """,
                (user_id, chat_id),
            ).fetchone()
            return row is not None

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
                    (SELECT COUNT(*) FROM file_events WHERE event_type IN ('request', 'refresh', 'inline')) AS requests,
                    (SELECT COUNT(*) FROM file_events WHERE event_type IN ('request', 'refresh', 'inline') AND created_at >= ?) AS recent_requests,
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

    def all_user_stats(self) -> list[UserStats]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY first_seen DESC"
            ).fetchall()
            return [self._user_from_row(row) for row in rows]

    def user_export_records(self) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    u.first_name,
                    u.last_name,
                    u.is_blocked,
                    u.request_count,
                    u.refresh_count,
                    u.denied_count,
                    u.files_sent,
                    u.first_seen,
                    u.last_seen,
                    u.last_request_at,
                    COALESCE(b.bonus_requests, 0) AS bonus_requests,
                    CASE WHEN s.user_id IS NULL THEN 0 ELSE 1 END AS is_sudo,
                    CASE WHEN p.user_id IS NULL THEN 0 ELSE 1 END AS is_privileged,
                    COALESCE(r.referrals, 0) AS referrals
                FROM users u
                LEFT JOIN user_bonuses b ON b.user_id = u.user_id
                LEFT JOIN sudo_users s ON s.user_id = u.user_id
                LEFT JOIN privileged_users p ON p.user_id = u.user_id
                LEFT JOIN (
                    SELECT referrer_id, COUNT(*) AS referrals
                    FROM referrals
                    WHERE fulfilled = 1
                    GROUP BY referrer_id
                ) r ON r.referrer_id = u.user_id
                ORDER BY u.first_seen DESC
                """
            ).fetchall()
            return [
                {
                    "user_id": int(row["user_id"]),
                    "username": row["username"],
                    "first_name": row["first_name"],
                    "last_name": row["last_name"],
                    "is_blocked": bool(row["is_blocked"]),
                    "request_count": int(row["request_count"]),
                    "refresh_count": int(row["refresh_count"]),
                    "denied_count": int(row["denied_count"]),
                    "files_sent": int(row["files_sent"]),
                    "bonus_requests": int(row["bonus_requests"]),
                    "is_sudo": bool(row["is_sudo"]),
                    "is_privileged": bool(row["is_privileged"]),
                    "referrals": int(row["referrals"]),
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "last_request_at": row["last_request_at"],
                }
                for row in rows
            ]

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
            mode=str(row["mode"]),
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
