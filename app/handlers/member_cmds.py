"""v2.6: /add <chat_id> @user1 @user2 … — bulk-add members via MTProto userbot."""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import repo, userbot as ub
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
        from telethon.errors import (FloodWaitError, UserPrivacyRestrictedError,
                                     UserAlreadyParticipantError, ChatAdminRequiredError)
        from telethon.tl.types import ChatAdminRights
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
        ok, ok_admin, already, failed, privacy = 0, 0, 0, [], 0
        for t in targets:
            while True:
                try:
                    user_ent = await client.get_entity(t)
                    try:
                        await client(InviteToChannelRequest(entity, [user_ent]))
                        ok += 1
                    except Exception as ie:
                        # Bots cannot be regular members of channels — promote
                        # them straight to full admin instead.
                        if "Bots can only be admins" in str(ie):
                            from telethon.tl.functions.channels import EditAdminRequest
                            # Channel-safe rights: manage_call is GROUP-only
                            # and triggers "wrong rights combination" on
                            # channels. ban_users is valid on both. If the
                            # account lacks add_admins permission, retry with
                            # a minimal set that any admin can grant.
                            is_megagroup = bool(getattr(entity, "megagroup", False))
                            base = dict(
                                change_info=True, edit_messages=True,
                                delete_messages=True, ban_users=True,
                                invite_users=True, pin_messages=True,
                                anonymous=False)
                            if is_megagroup:
                                base["manage_call"] = True
                            else:
                                base["post_messages"] = True  # channel-only
                            try:
                                rights = ChatAdminRights(add_admins=True, **base)
                                await client(EditAdminRequest(entity, user_ent,
                                                              rights, rank="bot"))
                            except Exception:
                                # Fallback: minimal rights (no add_admins) —
                                # works when the invoker lacks that permission.
                                rights = ChatAdminRights(add_admins=False, **base)
                                await client(EditAdminRequest(entity, user_ent,
                                                              rights, rank="bot"))
                            ok_admin += 1
                        else:
                            raise
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
                 f"Added as member: <b>{ok}</b>  |  added as ADMIN: <b>{ok_admin}</b>  |  "
                 f"already in: <b>{already}</b>  |  "
                 f"privacy-blocked: <b>{privacy}</b>  |  failed: <b>{len(failed)}</b>"]
        if failed:
            lines.append("Failed: " + ", ".join(esc(x) for x in failed[:30]))
        await bot.send_message(msg.from_user.id, "\n".join(lines), parse_mode="HTML")

    asyncio.create_task(_run())


# ------------------------- /leaderboard (all users) -------------------------
@router.message(Command("leaderboard"))
async def cmd_leaderboard(msg: Message) -> None:
    """Weekly top file-fetchers — open to every user. Resets Monday 1 AM IST."""
    try:
        rows = await repo.top_fetchers_week(limit=10)
    except Exception as e:
        log.warning("leaderboard failed: %s", e)
        rows = []
    if not rows:
        await msg.reply(
            "🏆 <b>Weekly Leaderboard</b>\n\n"
            "💤 No file fetches yet this week — be the first!\n"
            "<i>Resets Monday 1:00 AM IST</i>",
            parse_mode="HTML")
        return
    dir_map = await repo.get_directory_users([int(r["user_id"]) for r in rows])
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Weekly Leaderboard</b> (files fetched)", ""]
    for i, r in enumerate(rows, 1):
        uid = int(r["user_id"])
        info = dir_map.get(uid) or {}
        name = (f"@{info['username']}" if info.get("username")
                else (info.get("first_name") or f"User {uid}"))
        rank = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{rank} {esc(name)} — <b>{int(r['fetches'])}</b> files")
    lines += ["", "<i>Resets Monday 1:00 AM IST</i>"]
    await msg.reply("\n".join(lines), parse_mode="HTML")
