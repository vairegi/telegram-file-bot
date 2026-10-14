"""v5.3 — new-user welcome bonus (3 free tokens, 10-min self-destruct DM)."""
import asyncio

import pytest

from app.services import welcome, tokens
from app.handlers import setup_cmds


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kw):
        m = type("M", (), {})()
        m.chat = type("C", (), {"id": chat_id})()
        m.message_id = 1000 + len(self.sent)
        self.sent.append((chat_id, text, kw))
        return m

    async def delete_message(self, chat_id, mid):
        self.deleted.append((chat_id, mid))
        return True


def _fresh_user(monkeypatch, store):
    """Wire in-memory fakes: nobody in directory, no bonus flag, empty wallet."""
    monkeypatch.setattr(welcome.repo, "_mongo", lambda: False)
    monkeypatch.setattr(tokens.repo, "_mongo", lambda: False)

    async def get_setting_json(key, default=None):
        return dict(store.get(key, default))
    async def set_setting_json(key, val):
        store[key] = val
        return True
    async def get_directory_user(uid):
        return store["directory"].get(int(uid))
    async def get_setting_int(key, default):
        return default
    monkeypatch.setattr(welcome.repo, "get_setting_json", get_setting_json)
    monkeypatch.setattr(welcome.repo, "set_setting_json", set_setting_json)
    monkeypatch.setattr(welcome.repo, "get_directory_user", get_directory_user)
    monkeypatch.setattr(tokens.repo, "get_setting_json", get_setting_json)
    monkeypatch.setattr(tokens.repo, "set_setting_json", set_setting_json)
    monkeypatch.setattr(tokens.repo, "get_setting_int", get_setting_int)


def test_brand_new_user_gets_3_tokens_and_dm(monkeypatch):
    store = {"directory": {}}
    _fresh_user(monkeypatch, store)
    bot = FakeBot()
    fired = run(welcome.maybe_welcome(bot, 42))
    assert fired is True
    # 3 tokens in the wallet (Turso path)
    wallet = store["token_wallets"]["42"]
    assert wallet["tokens"] == 3
    assert wallet["expiry"] > 0
    # welcome DM sent, HTML, contains key content
    chat, text, kw = bot.sent[0]
    assert chat == 42 and kw.get("parse_mode") == "HTML"
    assert "3 FREE TOKENS" in text
    for cmd in ("/browse", "/favs", "/whoami", "/help"):
        assert cmd in text
    assert "10 minutes" in text
    # bonus flag recorded so it can't repeat
    assert "42" in store["welcome_bonus_uids"]


def test_existing_user_gets_nothing(monkeypatch):
    store = {"directory": {42: {"username": "oldie", "first_name": "Old"}}}
    _fresh_user(monkeypatch, store)
    bot = FakeBot()
    fired = run(welcome.maybe_welcome(bot, 42))
    assert fired is False
    assert bot.sent == []
    assert "token_wallets" not in store


def test_bonus_never_repeats_for_same_user(monkeypatch):
    store = {"directory": {}}
    _fresh_user(monkeypatch, store)
    bot = FakeBot()
    assert run(welcome.maybe_welcome(bot, 7)) is True
    # simulate the user coming back again (directory now knows them)
    store["directory"][7] = {"username": None, "first_name": "N"}
    assert run(welcome.maybe_welcome(bot, 7)) is False
    assert len(bot.sent) == 1  # only one DM ever
    assert store["token_wallets"]["7"]["tokens"] == 3  # not re-granted


def test_double_grant_guard_via_flag(monkeypatch):
    """Even if the directory read glitches (returns empty), the bonus flag
    prevents a second grant."""
    store = {"directory": {}}  # always looks brand-new
    _fresh_user(monkeypatch, store)
    bot = FakeBot()
    assert run(welcome.maybe_welcome(bot, 9)) is True
    # flag is set from the first run; second call must no-op
    assert run(welcome.maybe_welcome(bot, 9)) is False
    assert len(bot.sent) == 1
    assert store["token_wallets"]["9"]["tokens"] == 3


def test_storage_error_fails_open(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(welcome.repo, "get_directory_user", boom)
    bot = FakeBot()
    assert run(welcome.maybe_welcome(bot, 5)) is False
    assert bot.sent == []


def test_wallet_consumes_bonus_tokens_normally(monkeypatch):
    """Bonus tokens live in the SAME wallet: consume path works unchanged —
    while tokens remain, the shortener gate never fires (has_tokens True)."""
    store = {"directory": {}}
    _fresh_user(monkeypatch, store)
    bot = FakeBot()
    run(welcome.maybe_welcome(bot, 11))
    assert run(tokens.has_tokens(11)) is True
    assert run(tokens.consume(11)) == 2
    assert run(tokens.consume(11)) == 1
    assert run(tokens.consume(11)) == 0
    assert run(tokens.has_tokens(11)) is False  # gate would now apply


@pytest.mark.asyncio
async def test_delete_later_actually_deletes(monkeypatch):
    async def no_sleep(d):
        return None
    monkeypatch.setattr(welcome.asyncio, "sleep", no_sleep)
    bot = FakeBot()
    await welcome._delete_later(bot, 42, 555)
    assert bot.deleted == [(42, 555)]


def test_deep_link_calls_maybe_welcome_before_tracking():
    """Source-order guard: setup_cmds.cmd_start_deep must call
    welcome.maybe_welcome BEFORE repo.track_user_seen (so 'brand new' is
    judged before the user is recorded)."""
    import inspect
    src = inspect.getsource(setup_cmds.cmd_start_deep)
    i_w = src.index("maybe_welcome")
    i_t = src.index("track_user_seen")
    assert i_w < i_t
    assert "welcome bonus" in src
