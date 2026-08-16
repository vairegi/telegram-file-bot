"""Middleware: register users, block banned users, dedupe update_id."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from .. import db
from ..services import users


class UserMiddleware(BaseMiddleware):
    """Track every user and skip/deny banned users."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Dedupe webhook deliveries by update_id.
        update: Update | None = data.get("event_update", None) or data.get("update")
        if update is not None:
            uid = getattr(update, "update_id", None)
            if uid is not None:
                if db.query_scalar("SELECT 1 FROM telegram_updates WHERE update_id = ?", (uid,)):
                    return None
                db.execute("INSERT OR IGNORE INTO telegram_updates (update_id) VALUES (?)", (uid,))

        user_info = data.get("event_from_user")
        if user_info is not None:
            users.upsert_user(
                user_info.id,
                username=user_info.username,
                first_name=user_info.first_name,
                last_name=user_info.last_name,
            )
            if users.is_banned(user_info.id):
                return None  # silently drop banned users
        return await handler(event, data)
