"""Capture channel_post updates from Database Channels and queue them."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from ..services import sync

router = Router(name="channel_posts")


@router.channel_post()
async def on_channel_post(message: Message):
    try:
        status = await sync.handle_channel_post(
            message.chat.id, message.message_id, _message_to_dict(message))
        if status != "not-database-channel":
            print(f"[sync] channel_post {message.chat.id}:{message.message_id} -> {status}")
    except Exception as exc:
        print(f"[sync] channel_post error: {exc}")


def _message_to_dict(message: Message) -> dict:
    return {
        "message_id": message.message_id,
        "caption": message.caption,
        "text": message.text,
        "photo": [p.model_dump() for p in message.photo] if message.photo else None,
        "video": message.video.model_dump() if message.video else None,
        "document": message.document.model_dump() if message.document else None,
        "audio": message.audio.model_dump() if message.audio else None,
        "media_group_id": message.media_group_id,
    }
