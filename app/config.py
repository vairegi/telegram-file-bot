"""Central configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int = 0) -> int:
    raw = _env(key, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))
    base_webhook_url: str = field(default_factory=lambda: _env("BASE_WEBHOOK_URL"))
    webhook_path: str = field(default_factory=lambda: _env("WEBHOOK_PATH", "/webhook"))
    webhook_secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))
    web_server_host: str = field(default_factory=lambda: _env("WEB_SERVER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 10000))
    turso_database_url: str = field(default_factory=lambda: _env("TURSO_DATABASE_URL"))
    turso_auth_token: str = field(default_factory=lambda: _env("TURSO_AUTH_TOKEN"))
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH"))
    start_message_id: int = field(default_factory=lambda: _env_int("START_MESSAGE_ID", 0))
    super_admin_id: int = field(default_factory=lambda: _env_int("SUPER_ADMIN_ID", 0))
    log_channel_id: int = field(default_factory=lambda: _env_int("LOG_CHANNEL_ID", 0))

    @property
    def webhook_url(self) -> str:
        base = self.base_webhook_url.rstrip("/")
        path = self.webhook_path
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    @property
    def use_webhook(self) -> bool:
        return bool(self.base_webhook_url)


settings = Settings()
