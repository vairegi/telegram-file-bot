"""Real-storage v5.4 integration tests; never use a production Mongo URI.

Run with a local MongoDB at 127.0.0.1:27017. Turso cases use temporary
libsql files. Telegram and paid-shortener calls are faked, storage is not.
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from app import db, mongo_db
from app.config import settings
from app.services import repo, shortener as sh, posting, linkguard as lg, tokens
from app.handlers import shortener_cmds as cmds

VP = 'https://vplink.in/api?api=TEST_VP&url='
ARO = 'https://arolinks.com/api?api=TEST_ARO&url='
GP = 'https://gplinks.co/api?api=TEST_GP&url='


@pytest_asyncio.fixture(params=['turso', 'mongo'])
async def storage(request, monkeypatch, tmp_path):
    """Exercise the application's REAL repo accessors on both DB engines."""
    repo.cache_flush()
    monkeypatch.setattr(sh, '_provider_lock', asyncio.Lock())
    mode = request.param
    monkeypatch.setattr(settings, 'db_backend', mode)
    if mode == 'mongo':
        # Hard-coded localhost; no env var can redirect this test to Atlas.
        monkeypatch.setattr(settings, 'mongodb_uri', 'mongodb://127.0.0.1:27017/?retryWrites=false')
        monkeypatch.setattr(settings, 'mongodb_db_name', 'provider_test_' + uuid4().hex)
        await mongo_db.reset_conn()
        await mongo_db._get_db().command('ping')
    else:
        monkeypatch.setattr(settings, 'turso_database_url', str(tmp_path / 'settings.db'))
        monkeypatch.setattr(settings, 'turso_auth_token', '')
        db.reset_conn()
        db.init_schema()
    async def admin(_): return False
    async def unconfigured(): return False
    monkeypatch.setattr(cmds, '_reject_non_admin', admin)
    monkeypatch.setattr(lg, 'is_configured', unconfigured)
    await repo.set_setting('shortener_api', VP)
    await repo.set_setting_json('shortener_providers', [
        {'name': 'arolinks', 'api': ARO, 'hosts': ['links.arolinks.com']}])
    yield mode
    if mode == 'mongo':
        await mongo_db._client.drop_database(settings.mongodb_db_name)
        await mongo_db.reset_conn()
    else:
        db.reset_conn()
    repo.cache_flush()


class Message:
    def __init__(self, text):
        self.text = text
        self.from_user = SimpleNamespace(id=101)
        self.replies = []
    async def reply(self, text, **kwargs):
        self.replies.append(text)


async def invoke(handler, text):
    msg = Message(text)
    await handler(msg)
    return '\n'.join(msg.replies)


async def selected(uid=101):
    rows = await sh.get_providers()
    i = await sh.current_provider_index(uid)
    return rows[i]['name'] if i >= 0 else None


@pytest.mark.asyncio
async def test_legacy_read_without_migration_write(storage):
    assert [p['name'] for p in await sh.get_providers()] == ['vplink', 'arolinks']
    assert await repo.get_setting_json(sh.PROVIDERS_KEY, None) is None
    assert await selected() == 'vplink'


@pytest.mark.asyncio
async def test_delete_original_by_index_and_no_resurrection(storage):
    reply = await invoke(cmds.cmd_delshortener, '/delshortener 0')
    assert 'Removed' in reply and 'vplink' in reply
    assert [p['name'] for p in await sh.get_providers()] == ['arolinks']
    assert await sh.get_api_base() == ''
    # Even stale legacy config must not revive a deleted original provider.
    await repo.set_setting('shortener_api', VP)
    repo.cache_flush()
    assert [p['name'] for p in await sh.get_providers()] == ['arolinks']
    assert await selected() == 'arolinks'


@pytest.mark.asyncio
async def test_delete_original_by_name_case_insensitive(storage):
    await invoke(cmds.cmd_delshortener, '/delshortener VpLiNk')
    assert await selected() == 'arolinks'


@pytest.mark.asyncio
async def test_pause_resume_original_and_repeat_commands(storage):
    for _ in range(2):
        assert 'Paused' in await invoke(cmds.cmd_pause, '/pause vplink')
        assert await selected() == 'arolinks'
    for _ in range(3):
        await sh.advance_provider(101)
        assert await selected() == 'arolinks'
    assert 'Resumed' in await invoke(cmds.cmd_resume, '/resume 0')
    await sh.advance_provider(101)
    assert await selected() == 'vplink'
    await sh.advance_provider(101)
    assert await selected() == 'arolinks'


@pytest.mark.asyncio
async def test_pause_arolinks_only_vplink_used(storage):
    await invoke(cmds.cmd_pause, '/pause arolinks')
    for _ in range(4):
        assert await selected() == 'vplink'
        await sh.advance_provider(101)
    assert [p['enabled'] for p in await sh.get_providers()] == [True, False]


@pytest.mark.asyncio
async def test_three_provider_cycle_skips_middle_and_wraps(storage):
    await sh.add_provider('gplinks', GP)
    assert [await selected()] == ['vplink']
    await sh.advance_provider(101)
    assert await selected() == 'arolinks'
    await sh.advance_provider(101)
    assert await selected() == 'gplinks'
    await sh.advance_provider(101)
    assert await selected() == 'vplink'
    await sh.set_provider_enabled('arolinks', False)
    await sh.advance_provider(101)
    assert await selected() == 'gplinks'
    await sh.advance_provider(101)
    assert await selected() == 'vplink'


@pytest.mark.asyncio
async def test_delete_preserves_legacy_numeric_cursors(storage):
    await sh.add_provider('gplinks', GP)
    await repo.set_setting_json(sh.ROTATION_KEY, {'101': 1, '102': 2, '103': 0})
    await sh.remove_provider('vplink')
    assert await selected(101) == 'arolinks'
    assert await selected(102) == 'gplinks'
    assert await selected(103) == 'arolinks'
    await sh.remove_provider('arolinks')
    assert await selected(101) == 'gplinks'
    assert await selected(102) == 'gplinks'


@pytest.mark.asyncio
async def test_pause_and_remove_everyone_explicit_empty_persists(storage):
    await sh.set_provider_enabled('vplink', False)
    text = await invoke(cmds.cmd_pause, '/pause arolinks')
    assert 'No active shorteners' in text
    assert await selected() is None
    await sh.remove_provider('vplink')
    await sh.remove_provider('arolinks')
    repo.cache_flush()
    assert await repo.get_setting_json(sh.PROVIDERS_KEY) == []
    assert await sh.get_providers() == []
    await sh.add_provider('vplink', VP)
    assert await selected() == 'vplink'


@pytest.mark.asyncio
async def test_pause_state_survives_connection_restart(storage):
    await sh.set_provider_enabled('vplink', False)
    if storage == 'mongo':
        await mongo_db.reset_conn()
    else:
        db.reset_conn()
    repo.cache_flush()
    assert await selected() == 'arolinks'
    assert (await sh.get_providers())[0]['enabled'] is False


@pytest.mark.asyncio
async def test_add_delete_readd_and_duplicate_validation(storage):
    assert 'Added' in await invoke(cmds.cmd_addshortener, f'/addshortener gplinks | {GP} | links.gplinks.co')
    assert 'already exists' in await invoke(cmds.cmd_addshortener, f'/addshortener GPLINKS | {GP}')
    await sh.set_provider_enabled('gplinks', False)
    assert 'Removed' in await invoke(cmds.cmd_delshortener, '/delshortener 2')
    await sh.add_provider('gplinks', GP)
    assert (await sh.get_providers())[-1]['enabled'] is True
    for name, api, hosts in [('bad name', GP, []), ('bad', 'file:///tmp/x?url=', []),
                             ('bad', 'https://x.com/api?url=DEST', []),
                             ('bad', GP, ['https://bad.com/path'])]:
        with pytest.raises(ValueError):
            await sh.add_provider(name, api, hosts)


@pytest.mark.asyncio
async def test_all_commands_admin_only_and_unknown_target(storage, monkeypatch):
    before = await sh.get_providers()
    for handler, text in [(cmds.cmd_pause, '/pause missing'), (cmds.cmd_resume, '/resume 99'),
                          (cmds.cmd_delshortener, '/delshortener -1')]:
        assert 'No such shortener' in await invoke(handler, text)
    async def denied(_): return True
    monkeypatch.setattr(cmds, '_reject_non_admin', denied)
    for handler, text in [(cmds.cmd_pause, '/pause vplink'), (cmds.cmd_resume, '/resume vplink'),
                          (cmds.cmd_delshortener, '/delshortener vplink'),
                          (cmds.cmd_addshortener, f'/addshortener gp | {GP}')]:
        assert await invoke(handler, text) == ''
    assert await sh.get_providers() == before


@pytest.mark.asyncio
async def test_list_statuses_and_hide_api_tokens(storage):
    await sh.set_provider_enabled('vplink', False)
    text = await invoke(cmds.cmd_shorteners, '/shorteners')
    assert 'PAUSED' in text and 'ACTIVE' in text
    assert 'TEST_VP' not in text and 'TEST_ARO' not in text
    assert '/pause' in text and '/resume' in text


@pytest.mark.asyncio
async def test_legacy_api_updates_registry_preserving_paused_state(storage):
    await sh.set_provider_enabled('vplink', False)
    new_api = VP.replace('TEST_VP', 'NEW_TEST')
    assert 'saved' in await invoke(cmds.cmd_shortenerapi, '/shortenerapi ' + new_api)
    assert await sh.get_api_base() == new_api
    assert (await sh.get_providers())[0]['enabled'] is False
    await sh.remove_provider('vplink')
    await invoke(cmds.cmd_shortenerapi, '/shortenerapi ' + VP)
    assert [p['name'] for p in await sh.get_providers()] == ['arolinks', 'vplink']


@pytest.mark.asyncio
async def test_wallet_untouched_by_provider_edits(storage):
    wallet = await tokens.grant(101, 3)
    before = await tokens.get_wallet(101)
    await sh.set_provider_enabled('vplink', False)
    await sh.remove_provider('arolinks')
    assert await tokens.get_wallet(101) == before
    assert before['tokens'] == 3


@pytest.mark.asyncio
async def test_all_paused_gate_does_not_call_any_shortener(storage, monkeypatch):
    await repo.set_setting('shortener_enabled', '1')
    await sh.set_provider_enabled('vplink', False)
    await sh.set_provider_enabled('arolinks', False)
    async def forbidden(*args, **kwargs):
        raise AssertionError('No provider calls allowed')
    monkeypatch.setattr(sh, 'make_short_url_for', forbidden)
    assert await sh.send_gate(SimpleNamespace(), 101, 'post') is False
    with pytest.raises(RuntimeError, match='No active'):
        await sh.make_short_url('https://example.com')


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['off', 'on', 'unreachable', 'shortener_error'])
async def test_actual_gate_never_falls_back_to_paused_provider(storage, monkeypatch, guard):
    await repo.set_setting('shortener_enabled', '1')
    await sh.set_provider_enabled('vplink', False)
    calls, sent = [], []
    async def username(bot): return 'TestBot'
    async def token(uid, code): return 'TestToken'
    async def make_url(api, dest):
        calls.append((api, dest))
        if guard == 'shortener_error':
            raise RuntimeError('Simulated shortener outage')
        return 'https://arolinks.com/test'
    async def yes(): return True
    async def enabled(): return guard != 'off'
    async def grant(dest, ref_hosts):
        assert 'links.arolinks.com' in ref_hosts
        if guard == 'unreachable': return None
        return {'finish2_url': 'https://worker.example/finish2', 'slug': 'grant'}
    async def mint(url, grant_slug):
        return {'url': 'https://worker.example/public'}
    async def send_message(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=None)
    monkeypatch.setattr(posting, 'get_bot_username', username)
    monkeypatch.setattr(sh, 'new_token', token)
    monkeypatch.setattr(sh, 'make_short_url_for', make_url)
    monkeypatch.setattr(lg, 'enabled', enabled)
    monkeypatch.setattr(lg, 'is_configured', yes)
    monkeypatch.setattr(lg, 'mint_return_grant', grant)
    monkeypatch.setattr(lg, 'mint', mint)
    result = await sh.send_gate(SimpleNamespace(send_message=send_message), 101, 'post')
    assert result is (guard != 'shortener_error')
    assert calls and all(api == ARO for api, _ in calls)
    if guard != 'shortener_error':
        url = sent[0]['reply_markup'].inline_keyboard[0][0].url
        assert url == ('https://worker.example/public' if guard == 'on' else 'https://arolinks.com/test')


@pytest.mark.asyncio
async def test_mark_verified_grants_tokens_and_skips_paused(storage):
    await sh.set_provider_enabled('vplink', False)
    await sh._mark_verified(101)
    assert (await tokens.get_wallet(101))['tokens'] == await tokens.per_solve()
    assert await selected() == 'arolinks'


@pytest.mark.asyncio
async def test_concurrent_admin_adds_do_not_lose_providers(storage):
    await asyncio.gather(sh.add_provider('gplinks', GP), sh.add_provider('other', GP))
    assert {p['name'] for p in await sh.get_providers()} == {'vplink', 'arolinks', 'gplinks', 'other'}


@pytest.mark.asyncio
async def test_real_http_shortening_after_legacy_removal(storage):
    """Actual aiohttp request/response and destination encoding; localhost only."""
    from aiohttp import web
    received = []
    async def api(request):
        received.append(request.query['url'])
        return web.json_response({'status': 'success', 'shortenedUrl': 'https://short.example/test'})
    app = web.Application()
    app.router.add_get('/api', api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        await sh.remove_provider('vplink')
        await sh.remove_provider('arolinks')
        await sh.add_provider('local', f'http://127.0.0.1:{port}/api?api=TEST&url=')
        destination = 'https://worker.example/finish2?s=grant&t=a%2Bb&x=1'
        assert await sh.make_short_url(destination) == 'https://short.example/test'
        assert received == [destination]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_compatibility_wrapper_never_uses_paused_legacy(storage, monkeypatch):
    await sh.set_provider_enabled('vplink', False)
    calls = []
    async def shorten(api, dest):
        calls.append(api)
        return 'https://arolinks.com/test'
    monkeypatch.setattr(sh, 'make_short_url_for', shorten)
    await sh.make_short_url('https://worker.example/finish2')
    assert calls == [ARO]


@pytest.mark.asyncio
async def test_menu_has_commands_and_no_duplicate_names():
    from app.main import ADMIN_MENU
    names = [c.command for c in ADMIN_MENU]
    assert 'pause' in names and 'resume' in names
    assert len(names) == len(set(names))

