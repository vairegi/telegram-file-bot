"""Inline-button callbacks for ❤️ Save / 🗑 Remove on delivered files."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

from ..services import repo

log = logging.getLogger("callbacks")
router = Router(name="callbacks")


@router.callback_query(lambda c: (c.data or "").startswith("save:"))
async def on_save(cb: CallbackQuery) -> None:
    try:
        pid = int((cb.data or "").split(":", 1)[1])
    except Exception:
        await cb.answer("Bad data.")
        return
    repo.add_favorite(cb.from_user.id, pid)
    await cb.answer("❤️ Saved!")


@router.callback_query(lambda c: (c.data or "").startswith("unsave:"))
async def on_unsave(cb: CallbackQuery) -> None:
    try:
        pid = int((cb.data or "").split(":", 1)[1])
    except Exception:
        await cb.answer("Bad data.")
        return
    repo.remove_favorite(cb.from_user.id, pid)
    await cb.answer("🗑 Removed.")
