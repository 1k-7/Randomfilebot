from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IndexedFile:
    id: int
    file_id: str
    file_type: str
    label: str | None
    added_by: int
    created_at: datetime


@dataclass(frozen=True)
class ForceSubChat:
    chat_id: str
    title: str | None
    invite_link: str | None
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class UserStats:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_blocked: bool
    request_count: int
    refresh_count: int
    denied_count: int
    files_sent: int
    first_seen: datetime
    last_seen: datetime
    last_request_at: datetime | None


@dataclass(frozen=True)
class MembershipEvent:
    user_id: int
    chat_id: str
    status: str
    created_at: datetime
