"""Telegram client wrapper.

FIX #2 (aiogram 3 API): the old code used an internal session method that
which does not exist on AiohttpSession. Correct aiogram 3 usage: build a
TelegramMethod object and pass it to bot(method). Public helpers return
plain dicts so callers stay framework-agnostic.
"""
from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    DeleteMessage,
    EditMessageCaption,
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
from aiogram.types import BotCommand

from ..config import settings

_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


async def _call(method) -> Any:
    bot = get_bot()
    attempts = 4
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _as_dict(await bot(method))
        except TelegramRetryAfter as exc:
            last = exc
            if attempt < attempts:
                await asyncio.sleep(min(exc.retry_after, 30) + 0.25)
                continue
            raise
        except (TelegramNetworkError, TelegramServerError) as exc:
            last = exc
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            raise
    if last:
        raise last
    return None


def _as_dict(result) -> Any:
    if hasattr(result, "model_dump"):
        return result.model_dump(exclude_none=True)
    if isinstance(result, list):
        return [_as_dict(r) for r in result]
    return result


async def send_message(chat_id, text, **kw):
    return await _call(SendMessage(chat_id=chat_id, text=text, **kw))


async def edit_message_text(chat_id, message_id, text, **kw):
    return await _call(EditMessageText(chat_id=chat_id, message_id=message_id, text=text, **kw))


async def edit_message_caption(chat_id, message_id, caption, **kw):
    return await _call(EditMessageCaption(chat_id=chat_id, message_id=message_id, caption=caption, **kw))


async def copy_message(chat_id, from_chat_id, message_id, **kw):
    return await _call(CopyMessage(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kw))


async def send_photo(chat_id, photo, **kw):
    return await _call(SendPhoto(chat_id=chat_id, photo=photo, **kw))


async def send_video(chat_id, video, **kw):
    return await _call(SendVideo(chat_id=chat_id, video=video, **kw))


async def send_document(chat_id, document, **kw):
    return await _call(SendDocument(chat_id=chat_id, document=document, **kw))


async def send_audio(chat_id, audio, **kw):
    return await _call(SendAudio(chat_id=chat_id, audio=audio, **kw))


async def forward_message(chat_id, from_chat_id, message_id, **kw):
    return await _call(ForwardMessage(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kw))


async def delete_message(chat_id, message_id):
    return await _call(DeleteMessage(chat_id=chat_id, message_id=message_id))


async def get_chat_member(chat_id, user_id):
    return await _call(GetChatMember(chat_id=chat_id, user_id=user_id))


async def get_chat(chat_id):
    return await _call(GetChat(chat_id=chat_id))


async def answer_callback(callback_query_id, text=None, show_alert=False):
    return await _call(AnswerCallbackQuery(
        callback_query_id=callback_query_id, text=text, show_alert=show_alert
    ))


async def get_me() -> Any:
    return await _call(GetMe())


async def set_my_commands(commands: list[tuple[str, str]]) -> None:
    cmds = [BotCommand(command=c, description=d) for c, d in commands]
    await _call(SetMyCommands(commands=cmds))
