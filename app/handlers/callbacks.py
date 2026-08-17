"""Inline-callback handlers: save / unsave (PDF DMs only)."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from ..services import posting, repo, tg, users

log = logging.getLogger("callbacks")
router = Router(name="callbacks")


@router.callback_query(F.data.startswith("save:"))
async def cb_save(cb: CallbackQuery, bot: Bot) -> None:
    try:
        pid = int(cb.data.split(":", 1)[1])
    except Exception:
        await tg.answer_callback(bot, cb.id, "❌ Bad data")
        return
    users.upsert_user(cb.from_user.id, cb.from_user.username,
                      cb.from_user.first_name, cb.from_user.last_name)
    users.add_favorite(cb.from_user.id, pid)
    try:
        await bot.edit_message_reply_markup(
            chat_id=cb.message.chat.id,
            message_id=cb.message.message_id,
            reply_markup=posting.kb_pdf_save(pid, saved=True))
    except Exception:
        pass
    await tg.answer_callback(bot, cb.id, "❤️ Saved")


@router.callback_query(F.data.startswith("unsave:"))
async def cb_unsave(cb: CallbackQuery, bot: Bot) -> None:
    try:
        pid = int(cb.data.split(":", 1)[1])
    except Exception:
        await tg.answer_callback(bot, cb.id, "❌ Bad data")
        return
    users.remove_favorite(cb.from_user.id, pid)
    try:
        await bot.edit_message_reply_markup(
            chat_id=cb.message.chat.id,
            message_id=cb.message.message_id,
            reply_markup=posting.kb_pdf_save(pid, saved=False))
    except Exception:
        pass
    await tg.answer_callback(bot, cb.id, "🗑 Removed")
