"""v3.2 tests: broadcast tag preservation + userbot /forward engine.

All Telegram I/O is faked — these tests never touch the network.
"""
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, "/home/user/telegram-file-bot")

import pytest

from app.services import userbot as ub
from app.handlers import admin_stats


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self):
        self.dms: list[str] = []
        self.copied: list[int] = []
        self.forwarded: list[int] = []

    async def send_message(self, chat_id, text, **kw):
        self.dms.append(str(text))
        return SimpleNamespace(message_id=len(self.dms))

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.copied.append(int(chat_id))
        return SimpleNamespace(message_id=1)

    async def forward_message(self, chat_id, from_chat_id, message_id, **kw):
        self.forwarded.append(int(chat_id))
        return SimpleNamespace(message_id=1)


class FakeFloodWait(Exception):
    def __init__(self, seconds):
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class FakeRetryAfter(Exception):
    def __init__(self, retry_after):
        super().__init__("retry later")
        self.retry_after = retry_after


class FakeClient:
    """Records forward_messages calls. Can inject a one-shot FloodWait and
    can reject multi-id batches (exercises the singles fallback)."""

    def __init__(self, flood_on_calls=(), reject_batches=False):
        self.calls: list[tuple] = []
        self.flood_on_calls = set(flood_on_calls)
        self.reject_batches = reject_batches
        self._n = 0

    async def get_entity(self, ref):
        return ref

    async def forward_messages(self, dest, ids, from_peer=None):
        ids = list(ids)
        self._n += 1
        if self._n in self.flood_on_calls:
            raise FakeFloodWait(1)
        if self.reject_batches and len(ids) > 1:
            raise ValueError("MESSAGE_IDS_EMPTY")
        self.calls.append((dest, tuple(ids)))
        return [SimpleNamespace(id=i) for i in ids]


@pytest.fixture
def patched(monkeypatch):
    """Common patches: telethon present, fast pacing, fake FloodWait class."""
    monkeypatch.setattr(ub, "_TELETHON_OK", True)
    monkeypatch.setattr(ub, "FloodWaitError", FakeFloodWait)
    monkeypatch.setattr(ub, "_FWD_DELAY_S", 0.0)
    monkeypatch.setattr(ub, "_FWD_SINGLE_DELAY_S", 0.0)
    monkeypatch.setattr(ub, "_FWD_LONG_PAUSE_S", 0.0)
    return monkeypatch


def _patch_client(monkeypatch, client):
    async def _gc():
        return client
    monkeypatch.setattr(ub, "get_client", _gc)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# /forward engine
# ---------------------------------------------------------------------------
def test_forward_full_range_two_dests(patched):
    client = FakeClient()
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, txt = await ub.forward_start(bot, 999, -1002298797194,
                                         [-100111, -100222], 1, 250)
        assert ok, txt
        await ub._fwd_task

    _run(main())
    s = ub.forward_state()
    assert s.end_reason == "completed"
    assert s.forwarded == 250 * 2
    assert s.current_id == 251
    assert all(len(ids) <= 100 for _, ids in client.calls)   # batch cap
    assert {d for d, _ in client.calls} == {-100111, -100222}
    assert any("complete" in d for d in bot.dms)             # summary DM sent


def test_forward_floodwait_is_honored_and_retried(patched):
    client = FakeClient(flood_on_calls={2})   # flood on the 2nd API call
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, _ = await ub.forward_start(bot, 999, -1001, [-100111], 1, 150)
        assert ok
        await ub._fwd_task

    _run(main())
    s = ub.forward_state()
    assert s.end_reason == "completed"
    assert s.forwarded == 150
    assert "FloodWait" in s.last_error
    assert any("FloodWait" in d for d in bot.dms)            # admin warned


def test_forward_bad_batch_degrades_to_singles(patched):
    client = FakeClient(reject_batches=True)  # every 100-id batch fails
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, _ = await ub.forward_start(bot, 999, -1001, [-100111], 1, 120)
        assert ok
        await ub._fwd_task

    _run(main())
    s = ub.forward_state()
    assert s.end_reason == "completed"
    # singles mode: every call carries exactly 1 id, all 120 delivered
    assert all(len(ids) == 1 for _, ids in client.calls)
    assert s.forwarded == 120


def test_forward_long_pause_triggered(patched, monkeypatch):
    monkeypatch.setattr(ub, "_FWD_LONG_PAUSE_EVERY", 100)
    client = FakeClient()
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, _ = await ub.forward_start(bot, 999, -1001, [-100111], 1, 250)
        assert ok
        await ub._fwd_task

    _run(main())
    assert ub.forward_state().forwarded == 250
    assert any("Rate-limit rest" in d for d in bot.dms)


def test_forward_stop_and_resume(patched, monkeypatch):
    monkeypatch.setattr(ub, "_FWD_DELAY_S", 0.02)   # slow enough to catch mid-run
    client = FakeClient()
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, _ = await ub.forward_start(bot, 999, -1001, [-100111], 1, 2000)
        assert ok
        task = ub._fwd_task
        # wait until at least one batch actually went out, THEN stop — deterministic
        for _ in range(500):
            if ub.forward_state().current_id > 1:
                break
            await asyncio.sleep(0.01)
        ub.forward_stop()
        await task
        s = ub.forward_state()
        assert s.end_reason == "stopped"
        assert 1 < s.current_id <= 2001
        partial = s.forwarded
        assert 0 < partial < 2000

        ok, txt = await ub.forward_resume(bot, 999)
        assert ok, txt
        await ub._fwd_task
        s = ub.forward_state()
        assert s.end_reason == "completed"
        assert s.forwarded == 2000   # nothing lost, nothing doubled (single dest)

    _run(main())


def test_forward_span_guard(patched):
    client = FakeClient()
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, txt = await ub.forward_start(bot, 999, -1001, [-100111], 1, 500_000)
        assert not ok
        assert "too large" in txt

    _run(main())


def test_forward_refuses_when_running(patched, monkeypatch):
    monkeypatch.setattr(ub, "_FWD_DELAY_S", 0.05)
    client = FakeClient()
    _patch_client(patched, client)
    bot = FakeBot()

    async def main():
        ok, _ = await ub.forward_start(bot, 999, -1001, [-100111], 1, 300)
        assert ok
        ok2, txt2 = await ub.forward_start(bot, 999, -1001, [-100111], 1, 10)
        assert not ok2 and "already running" in txt2
        ub.forward_stop()
        await ub._fwd_task

    _run(main())


# ---------------------------------------------------------------------------
# /broadcast tag preservation
# ---------------------------------------------------------------------------
def _fake_admin_msg(src):
    replies = []

    async def _reply(text, **kw):
        replies.append(str(text))
        return SimpleNamespace(message_id=1)

    return SimpleNamespace(text="/broadcast", reply_to_message=src,
                           chat=SimpleNamespace(id=777), reply=_reply), replies


def _patch_broadcast_env(monkeypatch, users=(1, 2, 3)):
    async def _false(_msg):
        return False

    async def _uids():
        return list(users)

    monkeypatch.setattr(admin_stats, "_reject_non_admin", _false)
    monkeypatch.setattr(admin_stats.repo, "all_user_ids", _uids)
    monkeypatch.setattr(admin_stats, "TelegramRetryAfter", FakeRetryAfter)


def test_broadcast_with_tag_forwards(monkeypatch):
    _patch_broadcast_env(monkeypatch)
    bot = FakeBot()
    src = SimpleNamespace(chat=SimpleNamespace(id=55), message_id=5,
                          forward_origin=SimpleNamespace(type="channel"),
                          forward_from=None, forward_from_chat=None,
                          forward_sender_name=None)
    msg, replies = _fake_admin_msg(src)

    async def main():
        await admin_stats.cmd_broadcast(msg, bot)
        await asyncio.sleep(0.4)

    _run(main())
    assert bot.forwarded == [1, 2, 3]     # tag preserved → real forward
    assert bot.copied == []
    assert any("tag kept" in r for r in replies)
    assert any("Broadcast complete" in d for d in bot.dms)


def test_broadcast_without_tag_copies(monkeypatch):
    _patch_broadcast_env(monkeypatch)
    bot = FakeBot()
    src = SimpleNamespace(chat=SimpleNamespace(id=55), message_id=5)  # no fwd attrs
    msg, replies = _fake_admin_msg(src)

    async def main():
        await admin_stats.cmd_broadcast(msg, bot)
        await asyncio.sleep(0.4)

    _run(main())
    assert bot.copied == [1, 2, 3]        # clean copy, no tag
    assert bot.forwarded == []
    assert any("no tag" in r for r in replies)


def test_broadcast_retry_after_then_success(monkeypatch):
    _patch_broadcast_env(monkeypatch, users=(1,))
    bot = FakeBot()
    src = SimpleNamespace(chat=SimpleNamespace(id=55), message_id=5)
    msg, _ = _fake_admin_msg(src)
    state = {"raised": False}

    async def _flaky_copy(chat_id, from_chat_id, message_id, **kw):
        if not state["raised"]:
            state["raised"] = True
            raise FakeRetryAfter(0)
        bot.copied.append(int(chat_id))
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(bot, "copy_message", _flaky_copy)
    monkeypatch.setattr(admin_stats.asyncio, "sleep",
                        lambda s: asyncio.sleep(0) if False else _fast_sleep())

    async def _fast_sleep():
        return None

    async def main():
        await admin_stats.cmd_broadcast(msg, bot)
        await asyncio.sleep(0.3)

    # NOTE: keep real sleeps tiny — the RetryAfter wait is faked to ~0
    _run(main())
    assert bot.copied == [1]              # retried after the rate-limit hit
    assert any("sent: <b>1</b>" in d and "failed: <b>0</b>" in d
               for d in bot.dms)
