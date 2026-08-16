"""Telegram client wrapper around aiogram's Bot with 429/5xx retry."""
from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

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


async def call(method: str, **kwargs: Any) -> Any:
    """Call a Bot API method with retry on rate-limit / transient errors."""
    bot = get_bot()
    attempts = 4
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await bot.session.call_method(getattr(bot, method), kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            retry_after = _retry_after(exc)
            if attempt < attempts and retry_after is not None:
                await asyncio.sleep(min(retry_after, 30) + 0.25)
                continue
            # aiogram raises TelegramRetryAfter / TelegramNetworkError etc.
            if attempt < attempts and _is_transient(exc):
                await asyncio.sleep(0.5 * attempt)
                continue
            raise
    raise last  # pragma: no cover


def _retry_after(exc: Exception) -> float | None:
    try:
        from aiogram.exceptions import TelegramRetryAfter

        if isinstance(exc, TelegramRetryAfter):
            return float(exc.retry_after)
    except Exception:  # pragma: no cover
        pass
    return None


def _is_transient(exc: Exception) -> bool:
    try:
        from aiogram.exceptions import TelegramNetworkError, TelegramServerError

        return isinstance(exc, (TelegramNetworkError, TelegramServerError))
    except Exception:  # pragma: no cover
        return False


# ---- convenience wrappers -------------------------------------------------- #

async def send_message(chat_id, text, **kw):
    return await call("sendMessage", chat_id=chat_id, text=text, **kw)


async def edit_message_text(chat_id, message_id, text, **kw):
    return await call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, **kw)


async def copy_message(chat_id, from_chat_id, message_id, **kw):
    return await call("copyMessage", chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kw)


async def send_photo(chat_id, photo, **kw):
    return await call("sendPhoto", chat_id=chat_id, photo=photo, **kw)


async def send_video(chat_id, video, **kw):
    return await call("sendVideo", chat_id=chat_id, video=video, **kw)


async def send_document(chat_id, document, **kw):
    return await call("sendDocument", chat_id=chat_id, document=document, **kw)


async def send_audio(chat_id, audio, **kw):
    return await call("sendAudio", chat_id=chat_id, audio=audio, **kw)


async def forward_message(chat_id, from_chat_id, message_id, **kw):
    return await call("forwardMessage", chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kw)


async def delete_message(chat_id, message_id):
    return await call("deleteMessage", chat_id=chat_id, message_id=message_id)


async def get_chat_member(chat_id, user_id):
    return await call("getChatMember", chat_id=chat_id, user_id=user_id)


async def get_chat(chat_id):
    return await call("getChat", chat_id=chat_id)


async def answer_callback(callback_query_id, **kw):
    return await call("answerCallbackQuery", callback_query_id=callback_query_id, **kw)


async def get_me() -> Any:
    return await call("getMe")
