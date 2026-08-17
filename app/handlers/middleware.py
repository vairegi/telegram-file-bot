"""Middleware: dedupe update_id, block banned users.

Simplified: no per-update user upsert (handlers already touch users on demand).
This eliminates a double-write per update that stalled Turso libsql.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from .. import db
from ..services import users

log = logging.getLogger("middleware")


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Dedupe by update_id (best-effort; skip if it fails)
        update = data.get("event_update")
        if update is not None:
            uid = getattr(update, "update_id", None)
            if uid is not None:
                try:
                    db.execute(
                        "INSERT INTO telegram_updates (update_id) VALUES (?)",
                        (uid,))
                except Exception:
                    # Duplicate update_id -> skip
                    return None

        # Block banned users (do NOT upsert here; let handlers do it)
        u = data.get("event_from_user")
        if u is not None:
            try:
                if users.is_banned(u.id):
                    return None
            except Exception:
                log.exception("is_banned check failed")

        return await handler(event, data)
