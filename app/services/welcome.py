"""v5.3 — new-user welcome bonus (3 free tokens, no shortener on first visit).

Brand-new users (never seen before in the user directory) get a one-time
grant of 3 tokens expiring at the next 04:00 IST reset — same wallet as the
paid-shortener grants, so posting.deliver_to_user's existing consume path
works unchanged. While tokens remain, send_gate never fires, so newcomers
never see the shortener until the bonus is spent.

Everything is fail-open: any storage hiccup just skips the bonus and the
normal gate applies.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot

from . import repo, tokens

log = logging.getLogger("welcome")

BONUS_TOKENS = 3
DELETE_AFTER_SEC = 600          # welcome message self-destructs after 10 min

WELCOME_TEXT = (
    "⚡ <b>CONNECTION ESTABLISHED… welcome aboard, explorer!</b> 🤖\n\n"
    "You've been granted <b>3 FREE TOKENS</b> 🎟️ — our little welcome gift. "
    "Each tap of <b>⬇️ DOWNLOAD</b> spends just 1 token, so your first "
    "<b>3 downloads need NO shortener</b>. 🎉\n\n"
    "🧠 <b>Your command cheatsheet:</b>\n"
    "├ /browse — browse the full collection\n"
    "├ /favs — your ❤️ saved collection\n"
    "├ /whoami — your ID & account info\n"
    "└ /help — every command explained\n\n"
    "⏳ When the tokens run out, a quick short-link verification refuels you "
    "with 11 more — that's what keeps this archive alive. 💚\n"
    "<i>(This message self-destructs in 10 minutes. Enjoy!)</i>"
)


async def _is_brand_new(user_id: int) -> bool:
    """True when no prior directory record exists (checked BEFORE tracking)."""
    try:
        d = await repo.get_directory_user(int(user_id))
        return not d
    except Exception:
        return False


async def _mark_bonus_given(user_id: int) -> None:
    if repo._mongo():
        from .. import mongo_db

        async def _op(db):
            await db.verified_users.update_one(
                {"_id": int(user_id)},
                {"$set": {"welcome_bonus": True}},
                upsert=True)
            return True
        await mongo_db.with_retry(_op)
        return
    data = await repo.get_setting_json("welcome_bonus_uids", {}) or {}
    data[str(int(user_id))] = int(time.time())
    await repo.set_setting_json("welcome_bonus_uids", data)


async def _bonus_given(user_id: int) -> bool:
    """Double-check flag (mongo path: stored on the wallet document)."""
    try:
        if repo._mongo():
            from .. import mongo_db

            async def _op(db):
                d = await db.verified_users.find_one(
                    {"_id": int(user_id)}, {"welcome_bonus": 1})
                return bool((d or {}).get("welcome_bonus"))
            return bool(await mongo_db.with_retry(_op))
        data = await repo.get_setting_json("welcome_bonus_uids", {}) or {}
        return str(int(user_id)) in data
    except Exception:
        return True  # fail-safe: never double-grant on a read error


async def _delete_later(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await asyncio.sleep(DELETE_AFTER_SEC)
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def grant_welcome_bonus(bot: Bot, user_id: int) -> bool:
    """Grant 3 free tokens + send the geeky welcome DM (10-min self-destruct).
    Returns True when the bonus path fired. Fail-open: any error -> False."""
    try:
        await tokens.grant(int(user_id), BONUS_TOKENS)
        await _mark_bonus_given(user_id)
        sent = await bot.send_message(int(user_id), WELCOME_TEXT,
                                      parse_mode="HTML")
        asyncio.create_task(_delete_later(bot, sent.chat.id, sent.message_id))
        return True
    except Exception as e:
        log.warning("welcome bonus failed for %s: %s", user_id, e)
        return False


async def maybe_welcome(bot: Bot, user_id: int) -> bool:
    """Entry point: brand-new + never bonused -> grant. Else no-op."""
    try:
        if not await _is_brand_new(user_id):
            return False
        if await _bonus_given(user_id):
            return False
        return await grant_welcome_bonus(bot, int(user_id))
    except Exception as e:
        log.warning("welcome check failed for %s: %s", user_id, e)
        return False
