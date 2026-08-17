"""Application entry point.

Webhook mode (Render): aiohttp server bound to $PORT + in-process scheduler.
Polling mode: local dev fallback when BASE_WEBHOOK_URL is unset.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .config import settings
from .services import sync
from .services.scheduler import scheduler_loop
from .services.tg import get_bot, get_me
from .handlers import callbacks, channel_posts, commands, extras
from .handlers.middleware import UserMiddleware

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("telegram-file-bot")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(channel_posts.router)
    dp.include_router(callbacks.router)
    dp.include_router(commands.router)
    dp.include_router(extras.router)
    return dp


async def on_startup(bot: Bot) -> None:
    await sync.ensure_cursor_seeded()
    await commands.register_menu_commands()
    me = await get_me()
    logger.info("Bot @%s is up. Cursor=%s",
                me.get("username") if isinstance(me, dict) else me.username,
                sync.repo.get_cursor())


async def _run_webhook(dp: Dispatcher, bot: Bot) -> None:
    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot,
                                   secret_token=settings.webhook_secret)
    handler.register(app, path=settings.webhook_path)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    setup_application(app, dp, bot=bot)

    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret,
        allowed_updates=["message", "edited_message", "channel_post",
                         "chat_join_request", "callback_query"],
        drop_pending_updates=False)
    logger.info("Webhook registered at %s (path %s)",
                settings.webhook_url, settings.webhook_path)

    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        runner = web.AppRunner(app)
        await runner.setup()
        # FIX #3: bind to $PORT so Render's port scanner finds the service.
        site = web.TCPSite(runner, settings.web_server_host, settings.port)
        await site.start()
        logger.info("Web server listening on %s:%s",
                    settings.web_server_host, settings.port)
        await asyncio.Event().wait()
    finally:
        scheduler_task.cancel()
        await bot.delete_webhook()


async def _run_polling(dp: Dispatcher, bot: Bot) -> None:
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()


async def main() -> None:
    bot = get_bot()
    dp = build_dispatcher()
    dp.startup.register(on_startup)
    if settings.use_webhook:
        await _run_webhook(dp, bot)
    else:
        await _run_polling(dp, bot)


if __name__ == "__main__":
    asyncio.run(main())
