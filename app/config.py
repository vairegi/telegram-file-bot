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
    mongodb_uri: str
    mongodb_db_name: str
    db_backend: str

    @classmethod
    def load(cls) -> "Settings":
        def _int(k: str, default: int = 0) -> int:
            try:
                return int(os.environ.get(k) or default)
            except ValueError:
                return default

        # MTProto env aliases: accept both TG_* and the plain local names so a
        # locally-generated STRING_SESSION setup works without renaming.
        api_id = _int("TG_API_ID") or _int("API_ID")
        api_hash = (os.environ.get("TG_API_HASH") or os.environ.get("API_HASH") or "")
        session_str = (os.environ.get("TELETHON_SESSION_STRING")
                       or os.environ.get("STRING_SESSION") or "")

        return cls(
            bot_token=os.environ.get("BOT_TOKEN", ""),
            base_webhook_url=os.environ.get("BASE_WEBHOOK_URL", "").rstrip("/"),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            turso_database_url=os.environ.get("TURSO_DATABASE_URL", ""),
            turso_auth_token=os.environ.get("TURSO_AUTH_TOKEN", ""),
            super_admin_id=_int("SUPER_ADMIN_ID"),
            port=_int("PORT", 10000),
            tg_api_id=api_id,
            tg_api_hash=api_hash,
            telethon_session_string=session_str,
            # v3.0: MongoDB backend. DB_BACKEND stays 'turso' unless explicitly
            # flipped, so deploying this code with no env change is a no-op.
            mongodb_uri=os.environ.get("MONGODB_URI", ""),
            mongodb_db_name=os.environ.get("MONGODB_DB_NAME", "telegram_file_bot"),
            db_backend=(os.environ.get("DB_BACKEND", "turso") or "turso").strip().lower(),
        )


settings = Settings.load()
