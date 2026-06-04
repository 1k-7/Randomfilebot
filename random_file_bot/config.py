from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    owner_id: int
    database_path: str
    session_name: str
    session_workdir: str
    max_concurrent_transmissions: int
    request_limit: int
    request_window_seconds: int


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    load_dotenv()
    return Config(
        bot_token=_required("BOT_TOKEN"),
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        owner_id=int(_required("OWNER_ID")),
        database_path=os.getenv("DATABASE_PATH", "bot.sqlite3"),
        session_name=os.getenv("SESSION_NAME", "random_file_bot"),
        session_workdir=os.getenv("SESSION_WORKDIR", "."),
        max_concurrent_transmissions=int(os.getenv("MAX_CONCURRENT_TRANSMISSIONS", "4")),
        request_limit=int(os.getenv("REQUEST_LIMIT", "30")),
        request_window_seconds=int(os.getenv("REQUEST_WINDOW_MINUTES", "60")) * 60,
    )
