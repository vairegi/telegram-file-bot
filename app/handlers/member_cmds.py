"""v2.6: /add <chat_id> @user1 @user2 … — bulk-add members via MTProto userbot."""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import userbot as ub
from ..utils import esc, parse_channel_id
from .setup_cmds import _reject_non_admin

log = logging.getLogger("member_cmds")
router = Router(name="member_cmds")


@router.message(Command("add"))
async def cmd_add(msg: Message, bot: Bot) -> None:
    """Bulk-invite users to a channel/group using the logged-in userbot.

    Usage: /add <channel_id> @user1 @user2 123456 @user4 …
    Rate-safe: 2s between invites, FloodWait-aware, final summary DM.
    """
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply(
            "Usage: <code>/add &lt;channel_id&gt; @user1 @user2 123456 …</code>\n"
            "Adds each user to the channel via the MTProto userbot.",
            parse_mode="HTML")
        return
    chan = parse_channel_id(parts[1])
    if not chan:
        await msg.reply("❌ Bad channel id.")
        return
    targets = []
    for tok in parts[2:]:
        tok = tok.strip().lstrip("@")
        if not tok:
            continue
        targets.append(tok)
    if not targets:
        await msg.reply("❌ No usernames/ids given.")
        return
    if len(targets) > 200:
        await msg.reply("❌ Max 200 users per run — split into batches.")
        return

    if not ub.telethon_available():
        await msg.reply("❌ telethon not installed on the server.")
        return

    await msg.reply(f"🚀 Adding <b>{len(targets)}</b> user(s) to <code>{chan}</code>… "
                    f"(2s between invites, FloodWait-aware)", parse_mode="HTML")

    async def _run():
        from telethon.tl.functions.channels import InviteToChannelRequest
        from telethon.errors import FloodWaitError, UserPrivacyRestrictedError, UserAlreadyParticipantError
        try:
            client = await ub.get_client()
        except Exception as e:
            await bot.send_message(msg.from_user.id, f"❌ Userbot not ready: {esc(str(e))}",
                                   parse_mode="HTML")
            return
        try:
            entity = await client.get_entity(chan)
        except Exception as e:
            await bot.send_message(msg.from_user.id,
                                   f"❌ Cannot resolve channel <code>{chan}</code>: {esc(str(e))}",
                                   parse_mode="HTML")
            return
        ok, already, failed, privacy = 0, 0, [], 0
        for t in targets:
            while True:
                try:
                    user_ent = await client.get_entity(t)
                    await client(InviteToChannelRequest(entity, [user_ent]))
                    ok += 1
                    break
                except FloodWaitError as fw:
                    wait_s = int(getattr(fw, "seconds", 0) or 5)
                    try:
                        await bot.send_message(msg.from_user.id,
                                               f"⏳ FloodWait: pausing {wait_s}s…")
                    except Exception:
                        pass
                    await asyncio.sleep(wait_s + 1)
                    continue
                except UserAlreadyParticipantError:
                    already += 1
                    break
                except UserPrivacyRestrictedError:
                    privacy += 1
                    failed.append(t)
                    break
                except Exception as e:
                    log.warning("/add %s → %s failed: %s", t, chan, e)
                    failed.append(t)
                    break
            await asyncio.sleep(2)  # rate safety between invites
        lines = [f"✅ <b>Add complete</b> for <code>{chan}</code>",
                 f"Added: <b>{ok}</b>  |  already in: <b>{already}</b>  |  "
                 f"privacy-blocked: <b>{privacy}</b>  |  failed: <b>{len(failed)}</b>"]
        if failed:
            lines.append("Failed: " + ", ".join(esc(x) for x in failed[:30]))
        await bot.send_message(msg.from_user.id, "\n".join(lines), parse_mode="HTML")

    asyncio.create_task(_run())
