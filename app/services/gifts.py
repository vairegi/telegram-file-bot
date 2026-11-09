"""v5.5 additive, idempotent gifts and restart-resumable notification jobs.

Credits are atomic with their receipt on MongoDB, or in one SQLite transaction.
Notification delivery is best effort and may repeat if a process stops after a
successful Telegram send but before its job checkpoint; tokens never re-credit
for the same command/recipient. No production actions happen on import.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time
from html import escape
from . import repo, tokens

log = logging.getLogger('gifts')
MAX_AMOUNT = 1_000_000
SEND_INTERVAL = 0.06   # under the default Telegram broadcast limit
_tasks = {}
_run_lock = asyncio.Lock()


def command_id(chat_id, message_id):
    return hashlib.sha256(f'gift:{int(chat_id)}:{int(message_id)}'.encode()).hexdigest()[:32]


async def credit(uid, amount, receipt, now=None):
    """Raise on DB error, NEVER report a failed write as a successful gift."""
    uid, amount = int(uid), int(amount)
    if uid <= 0 or not 1 <= amount <= MAX_AMOUNT:
        raise ValueError('Invalid user ID or gift amount')
    now = float(now if now is not None else time.time())
    exp = tokens.next_reset(now)
    tag = hashlib.sha256(str(receipt).encode()).hexdigest()
    if repo._mongo():
        from .. import mongo_db
        from pymongo import ReturnDocument
        duplicate = {'$in': [tag, {'$ifNull': ['$gift_ids', []]}]}
        live = {'$and': [{'$gt': [{'$ifNull': ['$token_expiry', 0]}, now]},
                         {'$gt': [{'$ifNull': ['$tokens', 0]}, 0]}]}
        pipeline = [{'$set': {
            'tokens': {'$cond': [duplicate, {'$ifNull': ['$tokens', 0]},
                       {'$add': [{'$cond': [live, {'$max': [0, {'$ifNull': ['$tokens', 0]}]}, 0]}, amount]}]},
            'token_expiry': {'$cond': [duplicate, {'$ifNull': ['$token_expiry', 0]},
                              {'$cond': [live, '$token_expiry', exp]}]},
            'gift_ids': {'$setUnion': [{'$ifNull': ['$gift_ids', []]}, [tag]]}
        }}, {'$unset': 'created_at'}]  # prevent old stamp TTL deleting gift receipts
        async def op(db):
            return await db.verified_users.find_one_and_update(
                {'_id': uid}, pipeline, upsert=True, return_document=ReturnDocument.BEFORE)
        before = (await mongo_db.with_retry(op)) or {}
        duplicate = tag in (before.get('gift_ids') or [])
        live = float(before.get('token_expiry') or 0) > now and int(before.get('tokens') or 0) > 0
        balance = max(0, int(before.get('tokens') or 0)) if live else 0
        return {'applied': not duplicate,
                'tokens': balance + (0 if duplicate else amount),
                'expiry': float(before.get('token_expiry') or 0) if duplicate or live else exp}
    # libsql: keep wallet and dedup receipt in the SAME transaction.
    from .. import db
    with db._lock:
        c = db._get_conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT value FROM settings WHERE key=?', (tokens._SETTINGS_KEY,)).fetchone()
            wallets = json.loads(row[0]) if row and row[0] else {}
            wallet = wallets.get(str(uid)) or {}
            receipts = list(wallet.get('gift_ids') or [])
            duplicate = tag in receipts
            if not duplicate:
                live = float(wallet.get('expiry') or 0) > now and int(wallet.get('tokens') or 0) > 0
                wallet['tokens'] = (max(0, int(wallet.get('tokens') or 0)) if live else 0) + amount
                wallet['expiry'] = float(wallet['expiry']) if live else exp
                wallet['gift_ids'] = receipts + [tag]
                wallets[str(uid)] = wallet
                c.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                          'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (tokens._SETTINGS_KEY, json.dumps(wallets)))
            c.commit()
        except Exception:
            c.rollback()
            raise
    repo._cache_invalidate('setting:' + tokens._SETTINGS_KEY)
    return {'applied': not duplicate, 'tokens': int(wallet.get('tokens') or 0),
            'expiry': float(wallet.get('expiry') or 0)}


async def get_job(job_id):
    if repo._mongo():
        from .. import mongo_db
        async def op(db):
            return await db.gift_jobs.find_one({'_id': str(job_id)})
        return await mongo_db.with_retry(op)
    return await repo.get_setting_json('giftjob:' + str(job_id), None)


async def save_job(job):
    if repo._mongo():
        from .. import mongo_db
        async def op(db):
            await db.gift_jobs.replace_one({'_id': job['_id']}, job, upsert=True)
        await mongo_db.with_retry(op)
    else:
        await repo.set_setting_json('giftjob:' + job['_id'], job)


async def create_job(job_id, sender, reply_chat, recipients, amount, message):
    existing = await get_job(job_id)
    if existing:
        return existing
    job = {'_id': str(job_id), 'sender': int(sender), 'reply_chat': int(reply_chat),
           'recipients': sorted({int(u) for u in recipients if int(u) > 0}),
           'amount': int(amount), 'message': str(message), 'cursor': 0,
           'credited': 0, 'notified': 0, 'credit_failed': 0, 'notify_failed': 0,
           'state': 'pending', 'created_at': time.time()}
    if repo._mongo():
        from .. import mongo_db
        async def op(db):
            await db.gift_jobs.update_one({'_id': job['_id']}, {'$setOnInsert': job}, upsert=True)
        await mongo_db.with_retry(op)
        return await get_job(job_id)
    await save_job(job)
    return job


def alert(job, result):
    return (f"🎁 <b>A little bonus just landed!</b>\n\n"
            f"You've received <b>+{job['amount']} token(s)</b> from the admins. ✨\n\n"
            f"💬 <blockquote>{escape(job['message'])}</blockquote>\n"
            f"🎟 Balance after gift: <b>{result['tokens']}</b>\n"
            f"⏳ Expires: {tokens.expiry_label(result['expiry'])}\n\n"
            "Your next download is waiting. Check /mystats anytime. 💚")


async def _notify(bot, uid, text):
    from aiogram.exceptions import TelegramRetryAfter
    for attempt in range(3):
        try:
            await bot.send_message(uid, text, parse_mode='HTML', disable_notification=False)
            return True
        except TelegramRetryAfter as exc:
            if attempt == 2:
                return False
            await asyncio.sleep(max(1, exc.retry_after) + 1)
        except Exception:
            return False
    return False


def summary(job):
    return (f"🎁 Gift job {job['_id']} — {job['state']}\n"
            f"Processed: {job['cursor']}/{len(job['recipients'])}\n"
            f"Credited: {job['credited']} · Alerts sent: {job['notified']}\n"
            f"Credit failures: {job['credit_failed']} · Alert failures: {job['notify_failed']}")


async def run_job(bot, job_id):
    # One worker loop at a time keeps broadcasts rate limited in this process.
    async with _run_lock:
        job = await get_job(job_id)
        if not job or job['state'] == 'done':
            return
        job['state'] = 'running'
        await save_job(job)
        while job['cursor'] < len(job['recipients']):
            uid = job['recipients'][job['cursor']]
            try:
                result = await credit(uid, job['amount'], f"{job_id}:{uid}")
            except Exception:
                log.exception('gift credit failed (uid=%s)', uid)
                job['credit_failed'] += 1
            else:
                # Duplicate receipt means a crash/retry AFTER the original credit;
                # count this recipient once in the job checkpoint, never add twice.
                job['credited'] += 1
                if await _notify(bot, uid, alert(job, result)):
                    job['notified'] += 1
                else:
                    job['notify_failed'] += 1
            job['cursor'] += 1
            await save_job(job)
            await asyncio.sleep(SEND_INTERVAL)
        job['state'] = 'done'
        await save_job(job)
        try:
            await bot.send_message(job['reply_chat'], summary(job), parse_mode=None)
        except Exception:
            log.warning('gift summary DM failed; /giftstatus still available')


def spawn(bot, job_id):
    current = _tasks.get(job_id)
    if current and not current.done():
        return
    async def runner():
        try:
            await run_job(bot, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('gift job paused; persisted cursor will resume on startup')
        finally:
            _tasks.pop(job_id, None)
    _tasks[job_id] = asyncio.create_task(runner())


async def resume_pending(bot):
    if repo._mongo():
        from .. import mongo_db
        async def op(db):
            return await db.gift_jobs.find({'state': {'$in': ['pending', 'running']}}).to_list(length=None)
        jobs = await mongo_db.with_retry(op)
    else:
        jobs = [json.loads(r['value']) for r in repo.query_all(
            "SELECT value FROM settings WHERE key LIKE 'giftjob:%'")]
    for job in jobs:
        if job['state'] in ('pending', 'running'):
            spawn(bot, job['_id'])


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
