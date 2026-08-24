"""Live channel_post handler — routes DB-channel updates into sync."""
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
        await sync.handle_channel_post(msg)
    except Exception:
        log.exception("channel_post handler error")
