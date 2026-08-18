"""Telegram client wrapper (aiogram 3, native).

FIX #2: use the native await-bot(Method) path only (no legacy session API calls).
TelegramMethod subclasses submitted with `await bot(<Method>(...))`,
which is the canonical aiogram-3 path.

Every send/copy helper accepts protect_content so /protect 1 can propagate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    DeleteMessage,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    ForwardMessage,
    GetChat,
    GetChatMember,
    GetMe,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SetMyCommands,
)

log = logging.getLogger("tg")

_MAX_ATTEMPTS = 4


async def _run(bot: Bot, method) -> Any:
    """Send a TelegramMethod with basic retry / rate-limit handling."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await bot(method)
        except TelegramRetryAfter as e:
            wait = min(int(getattr(e, "retry_after", 3) or 3), 30) + 1
            log.warning("rate limited: sleeping %ss (attempt %s)", wait, attempt)
            await asyncio.sleep(wait)
            last_exc = e
        except TelegramBadRequest as e:
            # These are usually deterministic errors -> do not retry
            raise
        except Exception as e:  # network / 5xx
            last_exc = e
            await asyncio.sleep(0.5 * attempt)
    if last_exc:
        raise last_exc


async def get_me(bot: Bot):
    return await _run(bot, GetMe())


async def get_chat(bot: Bot, chat_id):
    return await _run(bot, GetChat(chat_id=chat_id))


async def get_chat_member(bot: Bot, chat_id, user_id: int):
    return await _run(bot, GetChatMember(chat_id=chat_id, user_id=user_id))


async def send_message(bot: Bot, chat_id, text: str, *,
                       reply_markup=None, parse_mode: str = "HTML",
                       disable_web_page_preview: bool = True,
                       protect_content: bool = False):
    return await _run(bot, SendMessage(
        chat_id=chat_id, text=text, reply_markup=reply_markup,
        parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview,
        protect_content=protect_content))


async def send_photo(bot: Bot, chat_id, photo: str, *, caption: Optional[str] = None,
                     reply_markup=None, parse_mode: str = "HTML",
                     protect_content: bool = False, has_spoiler: bool = False):
    return await _run(bot, SendPhoto(
        chat_id=chat_id, photo=photo, caption=caption, parse_mode=parse_mode,
        reply_markup=reply_markup, protect_content=protect_content,
        has_spoiler=has_spoiler))


async def send_video(bot: Bot, chat_id, video: str, *, caption: Optional[str] = None,
                     reply_markup=None, parse_mode: str = "HTML",
                     protect_content: bool = False, has_spoiler: bool = False):
    return await _run(bot, SendVideo(
        chat_id=chat_id, video=video, caption=caption, parse_mode=parse_mode,
        reply_markup=reply_markup, protect_content=protect_content,
        has_spoiler=has_spoiler))


async def send_document(bot: Bot, chat_id, document: str, *, caption: Optional[str] = None,
                        reply_markup=None, parse_mode: str = "HTML",
                        protect_content: bool = False):
    return await _run(bot, SendDocument(
        chat_id=chat_id, document=document, caption=caption, parse_mode=parse_mode,
        reply_markup=reply_markup, protect_content=protect_content))


async def send_audio(bot: Bot, chat_id, audio: str, *, caption: Optional[str] = None,
                     reply_markup=None, parse_mode: str = "HTML",
                     protect_content: bool = False):
    return await _run(bot, SendAudio(
        chat_id=chat_id, audio=audio, caption=caption, parse_mode=parse_mode,
        reply_markup=reply_markup, protect_content=protect_content))


async def copy_message(bot: Bot, chat_id, from_chat_id, message_id: int, *,
                       caption: Optional[str] = None, parse_mode: str = "HTML",
                       reply_markup=None, protect_content: bool = False):
    """Preferred delivery path: token-agnostic (works after BotFather rotation)."""
    kwargs = dict(chat_id=chat_id, from_chat_id=from_chat_id,
                  message_id=message_id, reply_markup=reply_markup,
                  protect_content=protect_content)
    if caption is not None:
        kwargs["caption"] = caption
        kwargs["parse_mode"] = parse_mode
    return await _run(bot, CopyMessage(**kwargs))


async def forward_message(bot: Bot, chat_id, from_chat_id, message_id: int, *,
                          protect_content: bool = False):
    return await _run(bot, ForwardMessage(
        chat_id=chat_id, from_chat_id=from_chat_id,
        message_id=message_id, protect_content=protect_content))


async def edit_message_caption(bot: Bot, chat_id, message_id, caption: str, *,
                               reply_markup=None, parse_mode: str = "HTML"):
    return await _run(bot, EditMessageCaption(
        chat_id=chat_id, message_id=message_id, caption=caption,
        parse_mode=parse_mode, reply_markup=reply_markup))


async def edit_message_text(bot: Bot, chat_id, message_id, text: str, *,
                            reply_markup=None, parse_mode: str = "HTML"):
    return await _run(bot, EditMessageText(
        chat_id=chat_id, message_id=message_id, text=text,
        parse_mode=parse_mode, reply_markup=reply_markup))


async def edit_message_markup(bot: Bot, chat_id, message_id, reply_markup=None):
    return await _run(bot, EditMessageReplyMarkup(
        chat_id=chat_id, message_id=message_id, reply_markup=reply_markup))


async def delete_message(bot: Bot, chat_id, message_id: int):
    return await _run(bot, DeleteMessage(chat_id=chat_id, message_id=message_id))


async def answer_callback(bot: Bot, callback_query_id: str, text: str = "", *,
                          show_alert: bool = False):
    return await _run(bot, AnswerCallbackQuery(
        callback_query_id=callback_query_id, text=text, show_alert=show_alert))


async def set_my_commands(bot: Bot, commands, scope=None, language_code=None):
    kwargs = dict(commands=commands)
    if scope is not None:
        kwargs["scope"] = scope
    if language_code is not None:
        kwargs["language_code"] = language_code
    return await _run(bot, SetMyCommands(**kwargs))
