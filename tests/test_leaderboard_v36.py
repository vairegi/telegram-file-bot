"""v3.6: leaderboard/favsall profile links + name backfill + beautified help."""
import sys, asyncio, inspect
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo
from app.handlers import member_cmds, admin_stats, setup_cmds


class FakeBot:
    def __init__(self, chats=None):
        self.chats = chats or {}
    async def get_chat(self, uid):
        if uid in self.chats:
            return self.chats[uid]
        raise Exception("user not found")


def test_leaderboard_profile_links_and_ids(monkeypatch):
    rows = [{"user_id": 6039189465, "fetches": 15},
            {"user_id": 8831632127, "fetches": 11},
            {"user_id": 5628992186, "fetches": 2}]
    async def _tfw(limit=10): return rows[:limit]
    # directory has INCOMPLETE rows for two users (migration-era: no names)
    async def _dir(uids):
        return {6039189465: {"user_id": 6039189465, "username": None, "first_name": None},
                8831632127: {"user_id": 8831632127, "username": "criminalsking", "first_name": None},
                5628992186: {"user_id": 5628992186, "username": None, "first_name": None}}
    async def _upsert(uid, uname, fname): pass
    monkeypatch.setattr(member_cmds.repo, "top_fetchers_week", _tfw)
    monkeypatch.setattr(member_cmds.repo, "get_directory_users", _dir)
    monkeypatch.setattr(member_cmds.repo, "upsert_directory_user", _upsert)
    bot = FakeBot({6039189465: SimpleNamespace(username=None, first_name="Soby lr1"),
                   5628992186: SimpleNamespace(username=None, first_name="Mangal")})
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/leaderboard", from_user=SimpleNamespace(id=1), reply=_reply)
    asyncio.run(member_cmds.cmd_leaderboard(m, bot))
    out = replies[0]
    # medal + linked name + numeric id + count — exactly the requested format
    assert '🥇 <a href="tg://user?id=6039189465">Soby lr1</a> <code>6039189465</code> — <b>15</b> files' in out
    assert '🥈 <a href="tg://user?id=8831632127">@criminalsking</a> <code>8831632127</code> — <b>11</b> files' in out
    assert '🥉 <a href="tg://user?id=5628992186">Mangal</a> <code>5628992186</code> — <b>2</b> files' in out
    assert "Resets Monday" in out


def test_leaderboard_name_backfill_when_dir_empty(monkeypatch):
    """Users absent from the directory get a live get_chat lookup."""
    rows = [{"user_id": 42, "fetches": 5}]
    async def _tfw(limit=10): return rows
    async def _dir(uids): return {}
    upserts = []
    async def _upsert(uid, uname, fname): upserts.append((uid, uname, fname))
    monkeypatch.setattr(member_cmds.repo, "top_fetchers_week", _tfw)
    monkeypatch.setattr(member_cmds.repo, "get_directory_users", _dir)
    monkeypatch.setattr(member_cmds.repo, "upsert_directory_user", _upsert)
    bot = FakeBot({42: SimpleNamespace(username="neo", first_name="Neo")})
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/leaderboard", from_user=SimpleNamespace(id=1), reply=_reply)
    asyncio.run(member_cmds.cmd_leaderboard(m, bot))
    assert "@neo" in replies[0] and "<code>42</code>" in replies[0]
    assert upserts == [(42, "neo", "Neo")]  # cached back for next time


def test_favsall_uses_profile_links(monkeypatch):
    async def _st(): return 1
    async def _sv(): return 3
    monkeypatch.setattr(admin_stats.repo, "savers_total", _st)
    monkeypatch.setattr(admin_stats.repo, "saves_total", _sv)
    async def _ts(limit=30, offset=0):
        return [{"user_id": 777, "saves": 3, "last_save": "x"}]
    monkeypatch.setattr(admin_stats.repo, "top_savers", _ts)
    async def _dir(uids): return {777: {"user_id": 777, "username": "seven", "first_name": None}}
    monkeypatch.setattr(admin_stats.repo, "get_directory_users", _dir)
    async def _favcov(uid, limit=3): return [{"caption": "Some Title"}]
    async def _favcnt(uid): return 3
    monkeypatch.setattr(admin_stats.repo, "favorite_covers_of_user", _favcov)
    monkeypatch.setattr(admin_stats.repo, "favorites_count_of_user", _favcnt)
    text, _, _ = asyncio.run(admin_stats._favsall_text(FakeBot(), 0))
    assert '<a href="tg://user?id=777">@seven</a> <code>777</code>' in text
    assert "3</b> saves" in text


def test_help_is_beautified_and_quoted():
    uh = setup_cmds._USER_HELP
    ah = setup_cmds._ADMIN_HELP
    assert "HELP MENU" in uh and "┌" in uh and "└" in uh
    assert "<blockquote>" in uh and "/leaderboard" in uh
    assert "ADMIN MENU" in ah and ah.count("<blockquote>") >= 7
    # every blockquote closed
    assert uh.count("<blockquote>") == uh.count("</blockquote>")
    assert ah.count("<blockquote>") == ah.count("</blockquote>")
    # no raw '<' that would break HTML parse (except tags)
    import re
    body = re.sub(r"</?b>|</?blockquote>|</?i>", "", uh + ah)
    assert "<" not in body.replace("&lt;", ""), "unescaped < in help text"


def test_favsall_callback_answers_fast():
    src = inspect.getsource(admin_stats.on_favsall_page)
    first_answer = src.index("await cb.answer()")
    first_render = src.index("_favsall_text")
    assert first_answer < first_render, "callback must ack before rendering"
