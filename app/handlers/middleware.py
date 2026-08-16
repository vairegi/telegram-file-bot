"""Middleware: register users, block banned users, dedupe update_id."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from .. import db
from ..services import users


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        update = data.get("event_update")
        if update is not None:
            uid = getattr(update, "update_id", None)
            if uid is not None:
                if db.query_scalar("SELECT 1 FROM telegram_updates WHERE update_id=?", (uid,)):
                    return None
                db.execute("INSERT OR IGNORE INTO telegram_updates (update_id) VALUES (?)", (uid,))

        u = data.get("event_from_user")
        if u is not None:
            users.upsert_user(u.id, username=u.username,
                              first_name=u.first_name, last_name=u.last_name)
            if users.is_banned(u.id):
                return None
        return await handler(event, data)
