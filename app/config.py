"""Central configuration loaded once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))

    # Webhook
    base_webhook_url: str = field(default_factory=lambda: _env("BASE_WEBHOOK_URL"))
    webhook_path: str = field(default_factory=lambda: _env("WEBHOOK_PATH", "/webhook"))
    webhook_secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))

    # Web server
    web_server_host: str = field(default_factory=lambda: _env("WEB_SERVER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))

    # Database
    turso_database_url: str = field(default_factory=lambda: _env("TURSO_DATABASE_URL"))
    turso_auth_token: str = field(default_factory=lambda: _env("TURSO_AUTH_TOKEN"))
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH"))

    # Resume / sync
    start_message_id: int = field(
        default_factory=lambda: int(_env("START_MESSAGE_ID", "0") or "0")
    )

    # Super admin
    super_admin_id: int = field(default_factory=lambda: int(_env("SUPER_ADMIN_ID") or "0"))

    # Optional
    log_channel_id: int = field(default_factory=lambda: int(_env("LOG_CHANNEL_ID") or "0"))

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
