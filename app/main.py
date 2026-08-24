"""Bot entry point — webhook server on aiohttp, aiogram dispatcher.

/health returns 200 with ZERO DB access (Render probes it every ~5s).
/webhook is the Telegram update endpoint.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, Update

from .config import settings
from .db import init_schema
from .handlers import (backfill_cmds, callbacks, channel_posts, content_cmds,
                       diag_cmds, massdlt_cmds, queue_cmds, setup_cmds)
from .services import scheduler, tg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("main")


# ------------------------- dispatcher -------------------------
dp = Dispatcher()
dp.include_router(channel_posts.router)
dp.include_router(callbacks.router)
dp.include_router(setup_cmds.router)
dp.include_router(backfill_cmds.router)
dp.include_router(queue_cmds.router)
dp.include_router(content_cmds.router)
dp.include_router(diag_cmds.router)
dp.include_router(massdlt_cmds.router)


USER_MENU = [
    BotCommand(command="start", description="Welcome"),
    BotCommand(command="help", description="Help"),
    BotCommand(command="whoami", description="Your id + role"),
    BotCommand(command="favs", description="Saved files"),
]

ADMIN_MENU = USER_MENU + [
    BotCommand(command="queue", description="Next 10 in queue"),
    BotCommand(command="queueinfo", description="Queue overview"),
    BotCommand(command="peek", description="Next N titles only"),
    BotCommand(command="whereami", description="Current cursor + state"),
    BotCommand(command="find", description="Search captions"),
    BotCommand(command="dripnow", description="Publish next N covers now"),
    BotCommand(command="dripstop", description="Cancel running drip"),
    BotCommand(command="setschedule", description="IST slots × batch"),
    BotCommand(command="scheduleoff", description="Clear schedule"),
    BotCommand(command="pauseposting", description="Pause drip"),
    BotCommand(command="resumeposting", description="Resume drip"),
    BotCommand(command="skip", description="Skip next N / up to link"),
    BotCommand(command="skip_range", description="Skip a #A-#B range"),
    BotCommand(command="unskip", description="Requeue one #N"),
    BotCommand(command="jumpto", description="Force queue back to #N"),
    BotCommand(command="queue_reset", description="Nuclear: reset queue"),
    BotCommand(command="repost", description="Re-publish #N or code"),
    BotCommand(command="preview", description="DM-preview #N or code"),
    BotCommand(command="deletepost", description="Drop #N or code from queue"),
    BotCommand(command="spoiler", description="Spoiler 1/0"),
    BotCommand(command="protect", description="Protect-content 1/0"),
    BotCommand(command="postcaption", description="Caption extra below covers"),
    BotCommand(command="filecaption", description="Caption extra below files"),
    BotCommand(command="addchannel", description="Register a channel"),
    BotCommand(command="removechannel", description="Unregister"),
    BotCommand(command="listchannels", description="List channels"),
    BotCommand(command="setlog", description="Set log channel"),
    BotCommand(command="setcursor", description="Set DB-channel cursor"),
    BotCommand(command="tgsetapi", description="Set MTProto creds"),
    BotCommand(command="tglogin", description="MTProto login"),
    BotCommand(command="tgcode", description="Complete MTProto login"),
    BotCommand(command="tgstatus", description="MTProto status"),
    BotCommand(command="backfill_start", description="Start MTProto backfill"),
    BotCommand(command="backfill_resume", description="Resume from cursor"),
    BotCommand(command="backfill_stop", description="Stop backfill"),
    BotCommand(command="backfill_status", description="Backfill state (in-mem)"),
    BotCommand(command="backfill_reset", description="Clear backfill state"),
    BotCommand(command="massdlt", description="Bulk delete between links"),
    BotCommand(command="massdlt_status", description="/massdlt progress"),
    BotCommand(command="massdlt_stop", description="Stop /massdlt"),
    BotCommand(command="debug", description="Full state dump"),
    BotCommand(command="stats", description="Count summary"),
]


async def push_menus(bot: Bot) -> None:
    try:
        await tg.set_my_commands(bot, USER_MENU, scope=BotCommandScopeAllPrivateChats())
    except Exception:
        log.exception("set user menu failed")


# ------------------------- aiohttp app -------------------------
async def handle_health(request: web.Request) -> web.Response:
    # ZERO DB access — Render probes this every ~5s.
    return web.Response(text="ok")


async def handle_webhook(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if settings.webhook_secret and secret != settings.webhook_secret:
        return web.Response(status=403, text="forbidden")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    bot: Bot = request.app["bot"]
    try:
        update = Update.model_validate(data)
    except Exception:
        return web.Response(status=400, text="bad update")
    # Dispatch async — webhook returns 200 immediately.
    asyncio.create_task(dp.feed_update(bot=bot, update=update))
    return web.Response(text="ok")


async def on_startup(app: web.Application) -> None:
    bot: Bot = app["bot"]
    init_schema()
    await push_menus(bot)
    if settings.base_webhook_url:
        url = f"{settings.base_webhook_url}/webhook"
        try:
            await bot.set_webhook(
                url=url,
                secret_token=settings.webhook_secret or None,
                allowed_updates=["message", "callback_query", "channel_post"],
                drop_pending_updates=True,
            )
            log.info("webhook set → %s", url)
        except Exception:
            log.exception("set_webhook failed")
    scheduler.start(bot)


async def on_shutdown(app: web.Application) -> None:
    bot: Bot = app["bot"]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    scheduler.stop()
    try:
        await bot.session.close()
    except Exception:
        pass


def build_app() -> web.Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN env var missing")
    bot = Bot(token=settings.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", handle_health)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/webhook", handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
