"""Environment / runtime settings — the ONLY place we read env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    bot_token: str
    base_webhook_url: str
    webhook_secret: str
    turso_database_url: str
    turso_auth_token: str
    super_admin_id: int
    port: int
    tg_api_id: int
    tg_api_hash: str
    telethon_session_string: str

    @classmethod
    def load(cls) -> "Settings":
        def _int(k: str, default: int = 0) -> int:
            try:
                return int(os.environ.get(k) or default)
            except ValueError:
                return default

        return cls(
            bot_token=os.environ.get("BOT_TOKEN", ""),
            base_webhook_url=os.environ.get("BASE_WEBHOOK_URL", "").rstrip("/"),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            turso_database_url=os.environ.get("TURSO_DATABASE_URL", ""),
            turso_auth_token=os.environ.get("TURSO_AUTH_TOKEN", ""),
            super_admin_id=_int("SUPER_ADMIN_ID"),
            port=_int("PORT", 10000),
            tg_api_id=_int("TG_API_ID"),
            tg_api_hash=os.environ.get("TG_API_HASH", ""),
            telethon_session_string=os.environ.get("TELETHON_SESSION_STRING", ""),
        )


settings = Settings.load()
