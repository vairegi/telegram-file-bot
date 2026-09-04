"""v3.4: /stats enrichment + user tracking + favsall resilience."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo
from app.handlers import diag_cmds, admin_stats


def test_stats_output_has_all_fields(monkeypatch):
    async def _false(_m): return False
    monkeypatch.setattr(diag_cmds, "_reject_non_admin", _false)
    vals = {"total_cover_count": 20190, "total_file_count": 20549,
            "published_cover_count": 2124, "queued_cover_count": 18066,
            "users_total": 500, "users_active_today": 12, "users_active_week": 48,
            "users_active_month": 210, "users_new_today": 3,
            "fetches_today": 27, "fetches_total": 9876}
    for k, v in vals.items():
        async def _f(v=v): return v
        monkeypatch.setattr(repo, k, _f)
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/stats", reply=_reply)
    asyncio.run(diag_cmds.cmd_stats(m))
    out = replies[0]
    for frag in ("Covers: 20190", "Files: 20549", "Published: 2124", "Pending: 18066",
                 "Total: 500", "Active today: 12", "this week: 48", "this month: 210",
                 "New today: 3", "Today: 27", "All time: 9876"):
        assert frag in out, f"missing: {frag}"


def test_start_handler_tracks_user():
    import inspect
    from app.handlers import setup_cmds
    for fn in ("cmd_start_deep", "cmd_start_plain"):
        assert "track_user_seen" in inspect.getsource(getattr(setup_cmds, fn)), fn


def test_delivery_records_fetch():
    import inspect
    from app.services import posting
    src = inspect.getsource(posting.deliver_to_user)
    assert "record_file_fetch" in src and "track_user_seen" in src


def test_favsall_offset_fallback(monkeypatch):
    async def _st(): return 3
    async def _sv(): return 10
    monkeypatch.setattr(admin_stats.repo, "savers_total", _st)
    monkeypatch.setattr(admin_stats.repo, "saves_total", _sv)
    async def _ts(limit=100, offset=0):
        if offset: raise Exception("offset not supported")
        return [{"user_id": i, "saves": 10 - i} for i in range(1, 40)]
    monkeypatch.setattr(admin_stats.repo, "top_savers", _ts)
    async def _dir(uids): return {u: {"user_id": u, "username": None, "first_name": f"u{u}"} for u in uids}
    monkeypatch.setattr(admin_stats.repo, "get_directory_users", _dir)
    async def _favcov(uid, limit=3): return []
    async def _favcnt(uid): return 0
    monkeypatch.setattr(admin_stats.repo, "favorite_covers_of_user", _favcov)
    monkeypatch.setattr(admin_stats.repo, "favorites_count_of_user", _favcnt)
    class B:
        async def get_chat(self, uid): return SimpleNamespace(username=None, first_name=f"u{uid}")
    text, pages, _ = asyncio.run(admin_stats._favsall_text(B(), 0))
    assert "Top savers" in text and "saves" in text


def test_favsall_empty_state(monkeypatch):
    async def _z(): return 0
    monkeypatch.setattr(admin_stats.repo, "savers_total", _z)
    monkeypatch.setattr(admin_stats.repo, "saves_total", _z)
    async def _ts(limit=100, offset=0): return []
    monkeypatch.setattr(admin_stats.repo, "top_savers", _ts)
    async def _dir(uids): return {}
    monkeypatch.setattr(admin_stats.repo, "get_directory_users", _dir)
    class B:
        async def get_chat(self, uid): return SimpleNamespace()
    text, pages, _ = asyncio.run(admin_stats._favsall_text(B(), 0))
    assert "No saves yet" in text
