"""Route incoming channel posts through the sync engine."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from ..services import sync

log = logging.getLogger("channel_posts")
router = Router(name="channel_posts")


@router.channel_post()
async def on_channel_post(msg: Message) -> None:
    try:
        result = await sync.handle_channel_post(msg)
        if result:
            log.info("captured %s from chat %s msg %s",
                     result, msg.chat.id, msg.message_id)
    except Exception:
        log.exception("channel_post handler failed")


@router.edited_channel_post()
async def on_channel_post_edited(msg: Message) -> None:
    # Optional: refresh caption for edited covers. Keep simple for now.
    pass
