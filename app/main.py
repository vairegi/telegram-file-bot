"""Application entry point.

Webhook mode (Render): aiohttp server bound to $PORT + in-process scheduler.
Polling mode: local dev fallback when BASE_WEBHOOK_URL is unset.

FIX #3: the aiohttp app ALWAYS binds to $PORT so Render's port scanner is
satisfied. /health is exposed for keep-alive pings.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .config import settings
from .services import repo, sync, tg
from .services.scheduler import scheduler_loop
from .handlers import callbacks, channel_posts, commands, extras
from .handlers.middleware import UserMiddleware

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("telegram-file-bot")


def build_bot() -> Bot:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not set")
    return Bot(token=settings.bot_token,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(channel_posts.router)
    dp.include_router(callbacks.router)
    dp.include_router(commands.router)
    try:
        dp.include_router(extras.router)
    except Exception:
        logger.exception("failed to include extras router")
    return dp


async def on_startup(bot: Bot) -> None:
    await sync.ensure_cursor_seeded()
    try:
        await commands.register_menu_commands(bot)
    except Exception:
        logger.exception("register_menu_commands failed")
    try:
        me = await tg.get_me(bot)
        logger.info("Bot @%s is up.", getattr(me, "username", "?"))
    except Exception:
        logger.warning("get_me failed (bad BOT_TOKEN?)")


def _build_web_app(dp: Dispatcher, bot: Bot) -> web.Application:
    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot,
                                   secret_token=settings.webhook_secret or None)
    handler.register(app, path=settings.webhook_path)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "mode": "webhook" if settings.use_webhook else "polling",
            "channels": {
                "database": len(repo.get_database_channels()),
                "main": len(repo.get_main_channels()),
            },
            "queue": repo.queued_covers_count(),
        })

    async def root(_: web.Request) -> web.Response:
        return web.Response(text="telegram-file-bot: OK")

    app.router.add_get("/health", health)
    app.router.add_get("/", root)
    setup_application(app, dp, bot=bot)
    return app


async def _run_webhook(dp: Dispatcher, bot: Bot) -> None:
    app = _build_web_app(dp, bot)
    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret or None,
        allowed_updates=["message", "edited_message", "channel_post",
                         "edited_channel_post", "chat_join_request",
                         "callback_query"],
        drop_pending_updates=False)
    logger.info("Webhook registered at %s", settings.webhook_url)

    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    try:
        runner = web.AppRunner(app)
        await runner.setup()
        # FIX #3: bind to $PORT so Render's port scanner is satisfied.
        site = web.TCPSite(runner, settings.web_server_host, settings.port)
        await site.start()
        logger.info("Web server listening on %s:%s",
                    settings.web_server_host, settings.port)
        await asyncio.Event().wait()
    finally:
        scheduler_task.cancel()
        try:
            await bot.delete_webhook()
        except Exception:
            pass


async def _run_polling(dp: Dispatcher, bot: Bot) -> None:
    """Local-dev polling — STILL binds a small aiohttp server to $PORT
    so Render's health-check works if you accidentally deploy in polling mode."""
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mode": "polling"})

    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_server_host, settings.port)
    await site.start()
    logger.info("Health server on %s:%s (polling mode)",
                settings.web_server_host, settings.port)

    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await dp.start_polling(bot,
                               allowed_updates=["message", "edited_message",
                                                "channel_post", "edited_channel_post",
                                                "callback_query", "chat_join_request"])
    finally:
        scheduler_task.cancel()


async def main() -> None:
    bot = build_bot()
    dp = build_dispatcher()
    dp.startup.register(on_startup)
    if settings.use_webhook:
        await _run_webhook(dp, bot)
    else:
        await _run_polling(dp, bot)


if __name__ == "__main__":
    asyncio.run(main())
