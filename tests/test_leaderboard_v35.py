"""v3.5: favsall long-message fix + public weekly /leaderboard."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo
from app.handlers import admin_stats, member_cmds


def test_favsall_never_exceeds_4096(monkeypatch):
    """30 savers × long titles must still fit Telegram's cap."""
    async def _st(): return 500
    async def _sv(): return 9999
    monkeypatch.setattr(admin_stats.repo, "savers_total", _st)
    monkeypatch.setattr(admin_stats.repo, "saves_total", _sv)
    async def _ts(limit=30, offset=0):
        return [{"user_id": i, "saves": 500 - i, "last_save": "x"} for i in range(1, limit + 1)]
    monkeypatch.setattr(admin_stats.repo, "top_savers", _ts)
    async def _dir(uids): return {u: {"user_id": u, "username": f"user_with_a_long_name_{u}", "first_name": "F"} for u in uids}
    monkeypatch.setattr(admin_stats.repo, "get_directory_users", _dir)
    async def _favcov(uid, limit=3):
        return [{"caption": "A very long doujinshi title with many words " * 4} for _ in range(3)]
    async def _favcnt(uid): return 400
    monkeypatch.setattr(admin_stats.repo, "favorite_covers_of_user", _favcov)
    monkeypatch.setattr(admin_stats.repo, "favorites_count_of_user", _favcnt)
    class B:
        async def get_chat(self, uid): return SimpleNamespace(username=None, first_name="F")
    text, pages, _ = asyncio.run(admin_stats._favsall_text(B(), 0))
    assert len(text) <= 4096, f"len={len(text)}"
    assert "Top savers" in text and "saves" in text
    assert pages >= 2


def _patch_lb(monkeypatch, rows, names):
    async def _tfw(limit=10): return rows[:limit]
    async def _dir(uids): return {u: names.get(u, {}) for u in uids}
    monkeypatch.setattr(member_cmds.repo, "top_fetchers_week", _tfw)
    monkeypatch.setattr(member_cmds.repo, "get_directory_users", _dir)


def test_leaderboard_renders_for_regular_user(monkeypatch):
    rows = [{"user_id": 1, "fetches": 87}, {"user_id": 2, "fetches": 40},
            {"user_id": 3, "fetches": 12}, {"user_id": 4, "fetches": 3}]
    _patch_lb(monkeypatch, rows, {1: {"username": "alice"}, 2: {"first_name": "Bob"}})
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/leaderboard", from_user=SimpleNamespace(id=999), reply=_reply)
    asyncio.run(member_cmds.cmd_leaderboard(m, SimpleNamespace()))
    out = replies[0]
    assert "Weekly Leaderboard" in out and "@alice" in out and "Bob" in out
    assert "87" in out and "🥇" in out and "Resets Monday" in out
    assert "User 4" in out


def test_leaderboard_empty(monkeypatch):
    _patch_lb(monkeypatch, [], {})
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/leaderboard", from_user=SimpleNamespace(id=1), reply=_reply)
    asyncio.run(member_cmds.cmd_leaderboard(m, SimpleNamespace()))
    assert "No file fetches yet" in replies[0]


def test_weekly_counter_rollover_and_top(monkeypatch):
    """Turso path: counters reset when the week key changes; top ordering right.
    get_setting_json/set_setting_json are SYNC in the Turso branch."""
    monkeypatch.setattr(repo, "_mongo", lambda: False)
    store = {}
    async def _gsj(key, default=None): return store.get(key, default)
    async def _ssj(key, val): store[key] = val
    monkeypatch.setattr(repo, "get_setting_json", _gsj)
    monkeypatch.setattr(repo, "set_setting_json", _ssj)
    weeks = iter(["2026-08-31", "2026-08-31", "2026-09-07"])
    monkeypatch.setattr("app.utils.week_start_ist", lambda: next(weeks))
    asyncio.run(repo.record_fetch_weekly(1, 5))
    asyncio.run(repo.record_fetch_weekly(1, 2))
    asyncio.run(repo.record_fetch_weekly(9, 50))
    monkeypatch.setattr("app.utils.week_start_ist", lambda: "2026-09-07")
    top = asyncio.run(repo.top_fetchers_week(10))
    assert top == [{"user_id": 9, "fetches": 50}], top
