"""v3.9: backup flood-wait backoff + deleted-source auto-skip."""
import sys, asyncio, random
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import backup


class FakeBot:
    def __init__(self): self.dms = []
    async def send_message(self, cid, text, **kw):
        self.dms.append(str(text)); return SimpleNamespace(message_id=1)


def test_backoff_grows_exponentially():
    """Sleep = wait * 1,2,4,8... + jitter, capped — not flat wait+1s."""
    src = open("/home/user/telegram-file-bot/app/services/backup.py").read()
    assert "2 ** (nonlocal_fw - 1)" in src
    assert "random.uniform(2, 15)" in src
    assert "900" in src  # 15-min cap


def test_streak_resets_on_success():
    src = open("/home/user/telegram-file-bot/app/services/backup.py").read()
    # success path zeroes the streak
    assert src.count("s._fw_streak = 0") >= 2  # success + non-flood paths


def _reset():
    backup._active.clear()


def test_missing_source_marked_done(monkeypatch):
    """'message to copy not found' records the row (skip) instead of erroring
    every auto_loop cycle forever."""
    backup._active.clear()
    recorded = []
    async def _all(): return [{"source_chat_id": -1001, "source_message_id": 45258}]
    async def _set(b): return set()
    async def _paused(): return False
    async def _record(b, d, m, t): recorded.append((d, m, t))
    async def _mirror(bot, b, d, m):
        return (False, None, "TelegramBadRequest: message to copy not found")
    monkeypatch.setattr(backup.repo, "all_db_source_messages", _all)
    monkeypatch.setattr(backup.repo, "backup_mirrored_set", _set)
    monkeypatch.setattr(backup.repo, "backup_is_paused", _paused)
    monkeypatch.setattr(backup.repo, "backup_record", _record)
    monkeypatch.setattr(backup, "_mirror_one", _mirror)
    s = backup.RunState(backup_chat_id=-1004233369369, running=False, started_at=1.0)
    backup._active[-1004233369369] = s
    out = asyncio.run(backup.run_backup(FakeBot(), -1004233369369, admin_chat_id=None, limit=10))
    assert recorded == [(-1001, 45258, 0)]      # marked done, tmid=0
    assert "deleted" in (out.get("last_error") or "")


def test_flood_waits_back_off_and_continue(monkeypatch):
    backup._active.clear()
    """3 consecutive floods then success: sleeps grow, message completes."""
    sleeps = []
    async def _fake_sleep(t): sleeps.append(t)
    monkeypatch.setattr(backup.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(backup.random, "uniform", lambda a, b: 0)  # deterministic
    calls = {"n": 0}
    async def _all(): return [{"source_chat_id": -1001, "source_message_id": 9}]
    async def _set(b): return set()
    async def _paused(): return False
    async def _record(b, d, m, t): pass
    async def _mirror(bot, b, d, m):
        calls["n"] += 1
        if calls["n"] <= 3:
            return (False, None, "FloodWait: A wait of 36 seconds is required")
        return (True, 999, None)
    monkeypatch.setattr(backup.repo, "all_db_source_messages", _all)
    monkeypatch.setattr(backup.repo, "backup_mirrored_set", _set)
    monkeypatch.setattr(backup.repo, "backup_is_paused", _paused)
    monkeypatch.setattr(backup.repo, "backup_record", _record)
    monkeypatch.setattr(backup, "_mirror_one", _mirror)
    s = backup.RunState(backup_chat_id=-1004233369369, running=False, started_at=1.0)
    backup._active[-1004233369369] = s
    out = asyncio.run(backup.run_backup(FakeBot(), -1004233369369, admin_chat_id=None, limit=5))
    assert out["mirrored"] == 1
    # flood rests are the first three sleeps: 36*1, 36*2, 36*4 (the 0.35s
    # per-message pace sleep after success is excluded)
    assert sleeps[:3] == [36.0, 72.0, 144.0]
