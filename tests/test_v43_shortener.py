"""v4.3.3: button removal + Mongo single-use tokens with TTL."""
import sys, asyncio, time
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo, shortener as sh, posting
from app.handlers import shortener_cmds, setup_cmds


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def fake_settings(monkeypatch):
    store = {}
    async def _gs(key, default=None): return store.get(key, default)
    async def _ss(key, val):
        if val is None: store.pop(key, None)
        else: store[key] = val
    async def _gsj(key, default=None): return store.get(key, default)
    async def _ssj(key, val): store[key] = val
    async def _gsb(key, default=False):
        v = store.get(key)
        return default if v is None else str(v) in ("1", "true", "on", "yes")
    monkeypatch.setattr(repo, "get_setting", _gs)
    monkeypatch.setattr(repo, "set_setting", _ss)
    monkeypatch.setattr(repo, "get_setting_json", _gsj)
    monkeypatch.setattr(repo, "set_setting_json", _ssj)
    monkeypatch.setattr(repo, "get_setting_bool", _gsb)
    monkeypatch.setattr(repo, "_mongo", lambda: False)
    monkeypatch.setattr(repo.settings, "db_backend", "json")
    return store


# ---- 1. single-use: consumed token is deleted, cannot be reused ----
def test_single_use_token_deleted_after_redeem(fake_settings):
    tok = run(sh.new_token(7, "abc"))
    run(sh.mark_completed(tok))
    assert run(sh.consume_token(tok, 7)) == "abc"
    assert run(sh.consume_token(tok, 7)) is None        # reuse impossible
    assert run(sh.peek_token(tok)) is None              # deleted from store


# ---- 1b. different user rejected immediately ----
def test_different_user_rejected(fake_settings):
    tok = run(sh.new_token(7, "abc"))
    run(sh.mark_completed(tok))
    assert run(sh.consume_token(tok, 999)) is None      # wrong user: reject
    assert run(sh.is_verified(999)) is False
    # original owner can still redeem (doc not burned by the stranger)
    assert run(sh.consume_token(tok, 7)) == "abc"


# ---- 2. incomplete token never redeems (shortener not finished) ----
def test_uncompleted_token_never_redeems(fake_settings):
    tok = run(sh.new_token(7, "abc"))
    assert run(sh.consume_token(tok, 7)) is None        # no landing page hit
    assert run(sh.is_verified(7)) is False
    run(sh.mark_completed(tok))                          # now it works
    assert run(sh.consume_token(tok, 7)) == "abc"


# ---- 3. TTL: tokens older than 15 min are gone (fallback path enforces too) ----
def test_token_ttl_15min(fake_settings):
    tok = run(sh.new_token(7, "abc"))
    data = run(repo.get_setting_json("verify_tokens", {}))
    data[tok]["created_at"] = time.time() - 901          # > 900s old
    run(repo.set_setting_json("verify_tokens", data))
    assert run(sh.peek_token(tok)) is None
    assert run(sh.consume_token(tok, 7)) is None
    assert run(sh.is_verified(7)) is False


def test_verify_ttl_hours_default_and_bounds(fake_settings):
    assert run(sh.get_ttl_hours()) == 6.0
    run(repo.set_setting("verify_ttl_hours", "24"))
    assert run(sh.get_ttl_hours()) == 24.0
    run(repo.set_setting("verify_ttl_hours", "999"))
    assert run(sh.get_ttl_hours()) == 6.0


def test_verification_persists_restart_proof(fake_settings):
    tok = run(sh.new_token(7, "abc"))
    run(sh.mark_completed(tok))
    run(sh.consume_token(tok, 7))
    assert run(sh.is_verified(7)) is True                # reads DB, not RAM


# ---- 4. gate message: NO "I've Verified" button, token never exposed ----
def test_gate_has_no_ive_verified_button(monkeypatch, fake_settings):
    run(repo.set_setting("shortener_enabled", "1"))
    run(repo.set_setting("shortener_api", "https://vplink.in/api?api=T&url="))
    async def _no_admin(uid): return False
    monkeypatch.setattr(repo, "is_admin", _no_admin)
    monkeypatch.setattr(posting.repo, "is_admin", _no_admin)
    async def _short(dest): return "https://vplink.in/XYZ"
    async def _uname(bot): return "mybot"
    monkeypatch.setattr(sh, "make_short_url", _short)
    monkeypatch.setattr(posting, "get_bot_username", _uname)
    sent = {}
    class _Bot:
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            sent.update(text=text, kb=reply_markup)
    import app.main as main_mod
    monkeypatch.setattr(main_mod.settings, "base_webhook_url", "https://b.onrender.com")
    assert run(sh.send_gate(_Bot(), 7, "abc")) is True
    rows = sent["kb"].inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert "🔓 Verify & Unlock" in labels
    assert not any("I've Verified" in l for l in labels)   # REMOVED
    urls = [getattr(b, "url", "") or "" for row in rows for b in row]
    assert not any("start=verify_" in u for u in urls)     # token never in DM


# ---- 5. landing page html ----
def test_landing_html():
    html = sh.landing_html("https://t.me/mybot?start=verify_X", "Heading")
    assert 'http-equiv="refresh"' in html
    assert "Continue in Telegram" in html


# ---- 6. verified TTL uses wall clock and repo persistence ----
def test_verified_set_get_count(fake_settings):
    run(repo.verified_set(7, time.time() + 3600))
    assert run(repo.verified_get(7)) > time.time()
    assert run(repo.verified_count()) == 1
    run(repo.verified_set(7, time.time() - 1))
    assert run(repo.verified_get(7)) < time.time()
    assert run(repo.verified_count()) == 0


# ---- 7. admin commands still work ----
def test_admin_commands(monkeypatch, fake_settings):
    monkeypatch.setattr(shortener_cmds, "_reject_non_admin",
                        lambda m: asyncio.sleep(0, result=False))
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    def call(text):
        m = SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), reply=_reply)
        verb = text.split()[0][1:]
        fn = {"shortener": shortener_cmds.cmd_shortener,
              "shortenerapi": shortener_cmds.cmd_shortenerapi,
              "setverifytime": shortener_cmds.cmd_setverifytime,
              "shortenerbtn": shortener_cmds.cmd_shortenerbtn,
              "clearshortenerbtns": shortener_cmds.cmd_clearshortenerbtns}[verb]
        run(fn(m))
    call("/shortener on"); assert run(sh.is_enabled()) is True
    call("/shortenerapi https://vplink.in/api?api=T&url=")
    assert run(sh.get_api_base()) == "https://vplink.in/api?api=T&url="
    call("/setverifytime 12"); assert run(sh.get_ttl_hours()) == 12.0
    call("/shortenerbtn 💎 Premium | https://t.me/p")
    assert run(sh.get_secondary_buttons()) == [["💎 Premium", "https://t.me/p"]]
    call("/clearshortenerbtns"); assert run(sh.get_secondary_buttons()) == []
    call("/shortener off"); assert run(sh.is_enabled()) is False
