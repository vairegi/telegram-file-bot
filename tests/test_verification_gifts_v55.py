"""v5.5 integration: real LOCAL MongoDB + temporary libsql; no live accounts.
Run with localhost mongod on port 27017. Paid APIs/Telegram are stubbed.
"""
import asyncio
import datetime as dt
import hashlib
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
import pytest_asyncio

from app import db, mongo_db
from app.config import settings
from app.services import repo, tokens, shortener as sh, richlists as rl, gifts
from app.services import verification_history as vh, browse, posting, linkguard
from app.handlers import gift_cmds as gc, richlist_cmds as rc, setup_cmds, callbacks


@pytest_asyncio.fixture(params=['mongo', 'turso'])
async def storage(request, tmp_path, monkeypatch):
    mode = request.param
    monkeypatch.setattr(settings, 'db_backend', mode)
    monkeypatch.setattr(settings, 'super_admin_id', 999)
    monkeypatch.setattr(gifts, '_run_lock', asyncio.Lock())
    monkeypatch.setattr(sh, '_provider_lock', asyncio.Lock())
    monkeypatch.setattr(gifts, 'SEND_INTERVAL', 0)
    repo.cache_flush()
    if mode == 'mongo':
        monkeypatch.setattr(settings, 'mongodb_uri', 'mongodb://127.0.0.1:27017/?retryWrites=false')
        monkeypatch.setattr(settings, 'mongodb_db_name', 'v55_test_' + uuid4().hex)
        await mongo_db.reset_conn()
        await mongo_db.init_schema()
    else:
        monkeypatch.setattr(settings, 'turso_database_url', str(tmp_path / 'local.db'))
        monkeypatch.setattr(settings, 'turso_auth_token', '')
        db.reset_conn()
        db.init_schema()
    for uid, username, name in [(101, 'test_person', 'Test <Person>'), (102, None, 'No Username'),
                                (999, 'admin_test', 'Admin')]:
        await repo.upsert_directory_user(uid, username, name)
    yield mode
    await gifts.shutdown()
    if mode == 'mongo':
        await mongo_db._client.drop_database(settings.mongodb_db_name)
        await mongo_db.reset_conn()
    else:
        db.reset_conn()
    repo.cache_flush()


class Bot:
    def __init__(self, blocked=()):
        self.sent, self.blocked = [], set(blocked)
    async def send_message(self, chat_id=None, text='', **kw):
        if chat_id in self.blocked:
            raise RuntimeError('blocked')
        self.sent.append((chat_id, text, kw))
        return NS(message_id=None, date=dt.datetime.now(dt.timezone.utc))


class Message:
    def __init__(self, text, uid=999, mid=1):
        self.text, self.from_user, self.chat, self.message_id = text, NS(id=uid), NS(id=uid), mid
        self.replies = []
    async def reply(self, text, **kw):
        self.replies.append(text)


def table(rm):
    return next(b for b in rm['blocks'] if b['type'] == 'table')


@pytest.mark.asyncio
async def test_profile_links_are_native_or_public(storage):
    await rl.record_verification(101, elapsed=151, link_type='arolinks')
    await rl.record_verification(102, elapsed=161, link_type='vplink')
    rm, plain, users, count = await rl.build_verified_list()
    assert (users, count) == (2, 2)
    names = [r[1]['text'] for r in table(rm)['cells'][1:]]
    assert names[0]['url'] == 'https://t.me/test_person?profile'
    assert names[1]['type'] == 'text_mention'
    assert names[1]['user'] == {'id': 102, 'is_bot': False, 'first_name': 'No Username'}
    assert 'tg://user' not in str(names)
    assert 'Test &lt;Person&gt;' in plain


@pytest.mark.asyncio
async def test_history_has_ist_duration_actual_type_and_no_secret(storage):
    when = dt.datetime(2026, 11, 9, 6, 0, tzinfo=dt.timezone.utc).timestamp()
    await vh.record(101, when, 167.25, 'arolinks', 'VERY_SECRET_TOKEN')
    await vh.record(101, when + 1000, 180, 'gplinks', 'ANOTHER_SECRET')
    rm, plain = await rl.build_verified_user(101)
    cells = table(rm)['cells']
    assert [c['text'] for c in cells[0]] == ['TIME (IST)', 'SOLVE', 'TYPE']
    assert cells[2][0]['text'] == '09 Nov 2026, 11:30:00 AM'
    assert cells[2][1]['text'] == '167.2s'
    assert cells[2][2]['text'] == 'arolinks'
    rows, total = await vh.history(101)
    assert total == 2 and 'VERY_SECRET_TOKEN' not in str(rows)
    assert 'including captcha' in plain


@pytest.mark.asyncio
async def test_history_pagination_unknown_and_idempotence(storage):
    for i in range(23):
        await vh.record(101, 1000 + i, i + 130, 'provider', f'e{i}')
    await vh.record(101, 9999, 999, 'wrong_duplicate', 'e0')
    rows, total = await vh.history(101, page=2)
    assert total == 23 and len(rows) == 3
    assert all(r['provider'] == 'provider' for r in rows)
    rm, plain = await rl.build_verified_user(101)
    assert '/verified_users 101 2' in plain
    rm, plain = await rl.build_verified_user(321)
    assert table(rm)['cells'][1][0]['text'] == 'No recorded solves'
    await vh.record(102, None, None, 'unknown', 'legacy')
    rm, plain = await rl.build_verified_user(102)
    assert table(rm)['cells'][1][0]['text'] == 'Not recorded'
    assert table(rm)['cells'][1][1]['text'] == 'Not recorded'


@pytest.mark.asyncio
async def test_gate_metadata_actual_provider_and_redemption_snapshot(storage, monkeypatch):
    await repo.set_setting('shortener_enabled', 'on')
    await repo.set_setting('shortener_api', 'https://vplink.in/api?api=TEST&url=')
    await repo.set_setting_json('shortener_providers', [
        {'name': 'arolinks', 'api': 'https://arolinks.com/api?api=TEST&url=', 'hosts': []}])
    await sh.set_provider_enabled('vplink', False)
    async def username(bot): return 'TestBot'
    async def shorten(api, destination): return 'https://short.example/test'
    async def off(): return False
    monkeypatch.setattr(posting, 'get_bot_username', username)
    monkeypatch.setattr(sh, 'make_short_url_for', shorten)
    monkeypatch.setattr(linkguard, 'enabled', off)
    bot = Bot()
    assert await sh.send_gate(bot, 101, 'post') is True
    if storage == 'mongo':
        rec = await mongo_db._get_db().verify_tokens.find_one({'user_id': 101})
        tok = rec['_id']
    else:
        pending = await repo.get_setting_json('verify_tokens')
        tok, rec = next(iter(pending.items()))
    assert rec['provider'] == 'arolinks' and rec['sent_at'] > 0
    # Advance only the clock in shortener to simulate an honest 180s solve.
    class Clock:
        @staticmethod
        def time(): return rec['sent_at'] + 180
    monkeypatch.setattr(sh, 'time', Clock)
    assert await sh.consume_token_timed(tok, 101) == ('ok', 'post')
    assert await repo.token_get(tok) is None
    rows, total = await vh.history(101)
    assert total == 1 and rows[0]['provider'] == 'arolinks'
    assert rows[0]['elapsed'] == 180
    assert rows[0]['sent_at'] == rec['sent_at']
    assert await sh.consume_token_timed(tok, 101) == ('invalid', None)
    assert (await vh.history(101))[1] == 1


@pytest.mark.asyncio
async def test_invalid_or_bypass_is_not_logged(storage):
    await repo.token_create('early', 101, 'post')
    assert (await sh.consume_token_timed('early', 102))[0] == 'invalid'
    assert (await sh.consume_token_timed('early', 101))[0] == 'bypass'
    assert (await vh.history(101))[1] == 0


@pytest.mark.asyncio
async def test_gift_adds_and_preserves_existing_expiry(storage):
    await tokens.grant(101, 7)
    before = await tokens.get_wallet(101)
    result = await gifts.credit(101, 5, 'receipt')
    after = await tokens.get_wallet(101)
    assert result['applied'] and after['tokens'] == 12
    assert after['expiry'] == before['expiry']
    assert await tokens.consume(101) == 11


@pytest.mark.asyncio
async def test_gift_expired_wallet_discards_old_balance(storage):
    await tokens._write(101, 99, 1)
    result = await gifts.credit(101, 5, 'receipt')
    wallet = await tokens.get_wallet(101)
    assert wallet['tokens'] == 5 and wallet['expiry'] > __import__('time').time()
    assert result['tokens'] == 5


@pytest.mark.asyncio
async def test_gift_empty_wallet_and_reset_window(storage):
    now = dt.datetime(2026, 11, 9, 3, 30, tzinfo=tokens.IST).timestamp()
    result = await gifts.credit(101, 5, 'receipt', now=now)
    expected = dt.datetime(2026, 11, 10, 4, 0, tzinfo=tokens.IST).timestamp()
    assert result['expiry'] == expected


@pytest.mark.asyncio
async def test_duplicate_credit_never_regrants_after_spend_solve_or_expiry(storage):
    assert (await gifts.credit(101, 5, 'repeat'))['applied']
    await tokens.consume(101)
    assert not (await gifts.credit(101, 5, 'repeat'))['applied']
    assert (await tokens.get_wallet(101))['tokens'] == 4
    await tokens.grant(101, 11)
    assert not (await gifts.credit(101, 5, 'repeat'))['applied']
    assert (await tokens.get_wallet(101))['tokens'] == 11
    await tokens._write(101, 0, 1)
    assert not (await gifts.credit(101, 5, 'repeat'))['applied']
    assert (await tokens.get_wallet(101))['tokens'] == 0


@pytest.mark.asyncio
async def test_concurrent_unique_and_duplicate_credits(storage):
    await asyncio.gather(*(gifts.credit(101, 1, f'unique{i}') for i in range(20)))
    assert (await tokens.get_wallet(101))['tokens'] == 20
    await asyncio.gather(*(gifts.credit(101, 2, 'duplicate') for _ in range(10)))
    assert (await tokens.get_wallet(101))['tokens'] == 22


@pytest.mark.asyncio
async def test_gift_notification_escape_and_normal_alert(storage):
    jobid = gifts.command_id(999, 1)
    await gifts.create_job(jobid, 999, 999, [101], 5, '<b>Enjoy & win</b>')
    bot = Bot()
    await gifts.run_job(bot, jobid)
    user = next(m for m in bot.sent if m[0] == 101)
    assert '+5 token(s)' in user[1] and '&lt;b&gt;Enjoy &amp; win&lt;/b&gt;' in user[1]
    assert user[2]['disable_notification'] is False
    job = await gifts.get_job(jobid)
    assert job['state'] == 'done' and job['credited'] == job['notified'] == 1
    # Re-running a completed job never gives again or sends another alert.
    await gifts.run_job(bot, jobid)
    assert len([m for m in bot.sent if m[0] == 101]) == 1


@pytest.mark.asyncio
async def test_all_users_include_unverified_and_blocked_alerts_are_reported(storage):
    jobid = gifts.command_id(999, 2)
    await gifts.create_job(jobid, 999, 999, await repo.all_user_ids(), 5, 'Enjoy')
    await gifts.run_job(Bot(blocked=[102]), jobid)
    for uid in (101, 102, 999):
        assert (await tokens.get_wallet(uid))['tokens'] == 5
    job = await gifts.get_job(jobid)
    assert job['credited'] == 3 and job['notified'] == 2 and job['notify_failed'] == 1


@pytest.mark.asyncio
async def test_database_error_no_false_success(storage, monkeypatch):
    jobid = gifts.command_id(999, 3)
    await gifts.create_job(jobid, 999, 999, [101, 102], 5, 'Enjoy')
    real = gifts.credit
    async def sometimes(uid, *a, **kw):
        if uid == 101:
            raise RuntimeError('write failed')
        return await real(uid, *a, **kw)
    monkeypatch.setattr(gifts, 'credit', sometimes)
    bot = Bot()
    await gifts.run_job(bot, jobid)
    job = await gifts.get_job(jobid)
    assert job['credit_failed'] == 1 and job['credited'] == 1
    assert not any(m[0] == 101 for m in bot.sent)
    assert (await tokens.get_wallet(101))['tokens'] == 0


@pytest.mark.asyncio
async def test_restart_recovery_idempotent_after_crash_after_credit(storage, monkeypatch):
    jobid = gifts.command_id(999, 4)
    job = await gifts.create_job(jobid, 999, 999, [101, 102], 5, 'Enjoy')
    await gifts.credit(101, 5, f'{jobid}:101')  # credit committed, cursor not checkpointed
    job['state'] = 'running'
    await gifts.save_job(job)
    if storage == 'mongo':
        await mongo_db.reset_conn()
    else:
        db.reset_conn()
    repo.cache_flush()
    queued = []
    monkeypatch.setattr(gifts, 'spawn', lambda bot, jid: queued.append(jid))
    await gifts.resume_pending(Bot())
    assert queued == [jobid]
    await gifts.run_job(Bot(), jobid)
    assert (await tokens.get_wallet(101))['tokens'] == 5
    assert (await tokens.get_wallet(102))['tokens'] == 5
    assert (await gifts.get_job(jobid))['credited'] == 2


@pytest.mark.asyncio
async def test_job_creation_deduplicates_same_command(storage):
    jid = gifts.command_id(999, 100)
    job = await gifts.create_job(jid, 999, 999, [101, 101, 102], 5, 'original')
    assert len(job['recipients']) == 2
    repeated = await gifts.create_job(jid, 999, 999, [999], 100, 'changed')
    assert repeated['amount'] == 5 and repeated['message'] == 'original'


@pytest.mark.asyncio
async def test_gift_command_and_status(storage, monkeypatch):
    queued = []
    monkeypatch.setattr(gifts, 'spawn', lambda bot, jid: queued.append(jid))
    bot = Bot()
    msg = Message('/gift 101 5 Enjoy the archive')
    await gc.cmd_gift(msg, bot)
    assert queued and 'Gift queued' in msg.replies[0]
    job = await gifts.get_job(queued[0])
    assert job['recipients'] == [101] and job['message'] == 'Enjoy the archive'
    await gifts.run_job(bot, queued[0])
    status = Message('/giftstatus ' + queued[0])
    await gc.cmd_giftstatus(status, bot)
    assert 'done' in status.replies[0]
    allmsg = Message('/gift all 5 Enjoy', mid=2)
    await gc.cmd_gift(allmsg, bot)
    assert len((await gifts.get_job(queued[-1]))['recipients']) == 3


@pytest.mark.asyncio
async def test_admin_permissions_and_input_validation(storage, monkeypatch):
    queued = []
    monkeypatch.setattr(gifts, 'spawn', lambda bot, jid: queued.append(jid))
    for text in ['/gift all 0 Enjoy', '/gift all -1 Enjoy', '/gift all x Enjoy',
                 '/gift bad 5 Enjoy', '/gift all 5', '/gift all 1000001 Enjoy']:
        msg = Message(text)
        await gc.cmd_gift(msg, Bot())
        assert 'Usage' in msg.replies[0]
    msg = Message('/gift 987654321 5 Enjoy')
    await gc.cmd_gift(msg, Bot())
    assert 'not registered' in msg.replies[0]
    for handler, text in [(gc.cmd_gift, '/gift all 5 Enjoy'),
                          (gc.cmd_giftstatus, '/giftstatus ' + 'a'*32),
                          (rc.cmd_verified_users, '/verified_users 101')]:
        msg = Message(text, uid=101)
        await handler(msg, Bot())
        assert msg.replies == ['🚫 Admin only.']
    assert not queued


@pytest.mark.asyncio
async def test_individual_history_command_and_validation(storage, monkeypatch):
    await vh.record(101, 1794204000, 160, 'arolinks', 'cmdtest')
    sent = []
    async def capture(bot, chat_id, rm, plain, reply_to=0):
        sent.append((rm, plain))
    monkeypatch.setattr(rl, 'send_rich_or_plain', capture)
    await rc.cmd_verified_users(Message('/verified_users 101'), Bot())
    assert table(sent[0][0])['cells'][1][2]['text'] == 'arolinks'
    for arg in ('abc', '-1', '101 0', '101 1 extra'):
        msg = Message('/verified_users ' + arg)
        await rc.cmd_verified_users(msg, Bot())
        assert 'Usage' in msg.replies[0]


@pytest.mark.asyncio
async def test_rich_api_rejection_plain_fallback_still_contains_mentions(storage, monkeypatch):
    await rl.record_verification(102, elapsed=160, link_type='vplink')
    rm, plain, *_ = await rl.build_verified_list()
    async def reject(*a, **kw):
        raise browse.BrowseError('unsupported rich message')
    monkeypatch.setattr(browse, 'api_post', reject)
    bot = Bot()
    await rl.send_rich_or_plain(bot, 999, rm, plain)
    assert bot.sent[0][2]['parse_mode'] == 'HTML'
    assert '<a href="tg://user?id=102">' in bot.sent[0][1]


@pytest.mark.asyncio
async def test_rescue_callback_is_not_a_second_solve(storage, monkeypatch):
    await tokens.grant(101, 5)
    async def no_post(_): return None
    monkeypatch.setattr(repo, 'get_post_by_code', no_post)
    class CB:
        from_user = NS(id=101)
        data = 'vrfchk:code'
        message = NS()
        async def answer(self, *a, **kw): pass
    await callbacks.on_verify_check(CB(), Bot())
    assert (await vh.history(101))[1] == 0
    assert (await rl._today_log()).get('101', {}).get('count', 0) == 0


@pytest.mark.asyncio
async def test_floodwait_notification_retries(storage, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage
    calls = []
    class FloodBot:
        async def send_message(self, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise TelegramRetryAfter(method=SendMessage(chat_id=101, text='x'), message='flood', retry_after=1)
    async def fast(_): pass
    # Replace this module's reference, never asyncio.sleep globally: Mongo's
    # own background monitor needs its real yielding sleep during teardown.
    monkeypatch.setattr(gifts, 'asyncio', NS(sleep=fast, gather=asyncio.gather,
                                           CancelledError=asyncio.CancelledError))
    assert await gifts._notify(FloodBot(), 101, 'hello')
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_daily_aggregate_deduplicates_redemption(storage):
    for _ in range(2):
        await rl.record_verification(101, elapsed=160, sent_at=12345,
                                     link_type='arolinks', event_id='same-redemption')
    assert (await rl._today_log())['101']['count'] == 1
    assert (await vh.history(101))[1] == 1


@pytest.mark.asyncio
async def test_large_plain_fallback_is_chunked_without_broken_mentions(storage, monkeypatch):
    async def reject(*args, **kwargs):
        raise browse.BrowseError('unsupported')
    monkeypatch.setattr(browse, 'api_post', reject)
    lines = ['<pre>'] + ['row ' + 'x' * 60 for _ in range(100)] + ['</pre>']
    lines += [rl.profile_html(i, 'Person', 'person_' + str(i)) for i in range(100, 200)]
    bot = Bot()
    await rl.send_rich_or_plain(bot, 999, {}, '\n'.join(lines))
    assert len(bot.sent) > 1
    assert sum(text.count('<a href=') for _, text, _ in bot.sent) == 100
    for _, text, _ in bot.sent:
        assert len(text) < 4096
        assert text.count('<pre>') == text.count('</pre>')
        assert text.count('<a href=') == text.count('</a>')


@pytest.mark.asyncio
async def test_background_spawn_deduplicates_and_shutdown_is_clean(storage):
    jid = gifts.command_id(999, 123)
    await gifts.create_job(jid, 999, 999, [101], 5, 'Enjoy')
    bot = Bot()
    gifts.spawn(bot, jid)
    task = gifts._tasks[jid]
    gifts.spawn(bot, jid)
    assert gifts._tasks[jid] is task
    await task
    assert not gifts._tasks
    assert (await tokens.get_wallet(101))['tokens'] == 5
    await gifts.shutdown()


@pytest.mark.asyncio
async def test_shutdown_leaves_pending_job_recoverable(storage, monkeypatch):
    jid = gifts.command_id(999, 124)
    await gifts.create_job(jid, 999, 999, [101], 5, 'Enjoy')
    entered = asyncio.Event()
    blocker = asyncio.Event()
    real_credit = gifts.credit
    async def blocked(*args, **kwargs):
        entered.set()
        await blocker.wait()
        return await real_credit(*args, **kwargs)
    monkeypatch.setattr(gifts, 'credit', blocked)
    gifts.spawn(Bot(), jid)
    await asyncio.wait_for(entered.wait(), timeout=5)
    await gifts.shutdown()
    assert not gifts._tasks
    job = await gifts.get_job(jid)
    assert job['state'] == 'running' and job['cursor'] == 0
    monkeypatch.setattr(gifts, 'credit', real_credit)
    await gifts.run_job(Bot(), jid)
    assert (await tokens.get_wallet(101))['tokens'] == 5
