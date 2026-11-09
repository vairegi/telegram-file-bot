"""Admin-only /gift user_id|all N message and /giftstatus job_id."""
import logging
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from ..config import settings
from ..services import gifts, repo

log = logging.getLogger('gift_cmds')
router = Router(name='gift_cmds')


async def _admin(msg):
    if not msg.from_user:
        return False
    if msg.from_user.id == settings.super_admin_id or await repo.is_admin(msg.from_user.id):
        return True
    await msg.reply('🚫 Admin only.')
    return False


@router.message(Command('gift'))
async def cmd_gift(msg: Message, bot: Bot):
    if not await _admin(msg):
        return
    parts = (msg.text or '').split(maxsplit=3)
    try:
        if len(parts) != 4:
            raise ValueError
        target, amount, message = parts[1], int(parts[2]), parts[3].strip()
        if not 1 <= amount <= gifts.MAX_AMOUNT or not message or len(message) > 1000:
            raise ValueError
        if target.casefold() != 'all' and (not target.isdigit() or int(target) <= 0):
            raise ValueError
    except ValueError:
        await msg.reply('Usage: /gift <user_id|all> <N> <message>\n'
                        'N: 1–1,000,000; message: 1–1,000 characters.\n'
                        'Example: /gift all 5 Enjoy')
        return
    try:
        if target.casefold() == 'all':
            recipients = await repo.all_user_ids()
        else:
            uid = int(target)
            if not await repo.get_directory_user(uid):
                await msg.reply('❌ User not registered. They need to start this bot first.')
                return
            recipients = [uid]
        recipients = sorted({int(u) for u in recipients if int(u) > 0})
        if not recipients:
            await msg.reply('No registered users to gift.')
            return
        job_id = gifts.command_id(msg.chat.id, msg.message_id)
        job = await gifts.create_job(job_id, msg.from_user.id, msg.chat.id,
                                     recipients, amount, message)
        if job['state'] == 'done':
            await msg.reply(gifts.summary(job), parse_mode=None)
            return
        await msg.reply(f"🎁 Gift queued for {len(job['recipients'])} user(s): +{job['amount']} each.\n"
                        f"Job: {job_id}\nStatus: /giftstatus {job_id}\n"
                        'Existing live expiry is kept; empty/expired wallets use the normal 4 AM IST reset.\n'
                        'Users who blocked the bot can still receive tokens, but their alert may fail.',
                        parse_mode=None)
        gifts.spawn(bot, job_id)
    except Exception:
        log.exception('gift setup failed')
        await msg.reply('❌ Gift setup could not complete. No success has been assumed; check logs/status.')


@router.message(Command('giftstatus'))
async def cmd_giftstatus(msg: Message, bot: Bot):
    if not await _admin(msg):
        return
    args = (msg.text or '').split()
    if len(args) != 2 or len(args[1]) != 32 or any(c not in '0123456789abcdef' for c in args[1]):
        await msg.reply('Usage: /giftstatus <job_id from the gift acknowledgement>')
        return
    try:
        job = await gifts.get_job(args[1])
        if not job:
            await msg.reply('Gift job not found.')
            return
        await msg.reply(gifts.summary(job), parse_mode=None)
        if job['state'] in ('pending', 'running'):
            gifts.spawn(bot, job['_id'])
    except Exception:
        log.exception('gift status failed')
        await msg.reply('❌ Gift status temporarily unavailable.')
