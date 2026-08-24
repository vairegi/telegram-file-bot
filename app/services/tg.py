"""Thin wrappers around aiogram Bot calls.

Only reason this file exists: keep aiogram-3 method spellings in ONE place
so upgrading aiogram is a single-file change.
"""
from __future__ import annotations

from typing import Any, Optional


async def get_me(bot):
    return await bot.get_me()


async def copy_message(bot, *, chat_id, from_chat_id, message_id,
                       caption: Optional[str] = None,
                       reply_markup=None,
                       protect_content: bool = False) -> Any:
    return await bot.copy_message(
        chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id,
        caption=caption, reply_markup=reply_markup,
        parse_mode="HTML", protect_content=protect_content,
    )


async def send_photo(bot, *, chat_id, photo, caption: Optional[str] = None,
                     reply_markup=None, protect_content: bool = False,
                     has_spoiler: bool = False) -> Any:
    return await bot.send_photo(
        chat_id=chat_id, photo=photo, caption=caption,
        reply_markup=reply_markup, parse_mode="HTML",
        protect_content=protect_content, has_spoiler=has_spoiler,
    )


async def send_video(bot, *, chat_id, video, caption: Optional[str] = None,
                     reply_markup=None, protect_content: bool = False,
                     has_spoiler: bool = False) -> Any:
    return await bot.send_video(
        chat_id=chat_id, video=video, caption=caption,
        reply_markup=reply_markup, parse_mode="HTML",
        protect_content=protect_content, has_spoiler=has_spoiler,
    )


async def send_document(bot, *, chat_id, document, caption: Optional[str] = None,
                        reply_markup=None, protect_content: bool = False) -> Any:
    return await bot.send_document(
        chat_id=chat_id, document=document, caption=caption,
        reply_markup=reply_markup, parse_mode="HTML",
        protect_content=protect_content,
    )


async def send_sticker(bot, *, chat_id, sticker, protect_content: bool = False) -> Any:
    return await bot.send_sticker(
        chat_id=chat_id, sticker=sticker, protect_content=protect_content,
    )


async def send_message(bot, *, chat_id, text: str, reply_markup=None,
                       protect_content: bool = False) -> Any:
    return await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup,
        parse_mode="HTML", protect_content=protect_content,
    )


async def delete_message(bot, *, chat_id, message_id) -> Any:
    try:
        return await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        return None


async def set_my_commands(bot, commands, scope=None):
    return await bot.set_my_commands(commands, scope=scope)
