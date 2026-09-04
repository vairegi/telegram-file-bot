"""v3.7: auto-skip covers whose DB-channel source message was deleted."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import posting, repo


class FakeBot:
    def __init__(self): self.dms = []
    async def send_message(self, chat_id, text, **kw):
        self.dms.append((int(chat_id), str(text)))
        return SimpleNamespace(message_id=1)


def _cover(pid, num=None, title="Some Title"):
    return {"id": pid, "kind": "cover", "post_number": num, "caption": title,
            "source_chat_id": -1002298797194, "source_message_id": 1000 + pid,
            "code": f"c{pid}"}


@pytest.fixture
def env(monkeypatch):
    state = {"queue": [], "skipped": [], "published": []}
    async def _paused(): return False
    async def _nextq():
        return state["queue"][0] if state["queue"] else None
    async def _skip(pid):
        state["skipped"].append(pid)
        state["queue"] = [c for c in state["queue"] if c["id"] != pid]
    async def _admins(): return [{"user_id": 111}]
    async def _high(): return 2000
    async def _getbyid(pid): return next((c for c in state["queue"] if c["id"] == pid), None)
    monkeypatch.setattr(posting, "_paused", _paused)
    monkeypatch.setattr(posting.repo, "next_queued_cover", _nextq)
    monkeypatch.setattr(posting.repo, "skip_post_by_id", _skip)
    monkeypatch.setattr(posting.repo, "list_admins", _admins)
    monkeypatch.setattr(posting.repo, "highest_post_number", _high)
    monkeypatch.setattr(posting.repo, "get_post_by_id", _getbyid)
    monkeypatch.setattr(posting, "settings", SimpleNamespace(super_admin_id=999))

    def make_pub(fail_ids, err="Telegram server says - Bad Request: message to copy not found"):
        async def _pub(bot, cover):
            if cover["id"] in fail_ids:
                return [{"chat_id": -1002392274488, "ok": False, "error": err}]
            state["published"].append(cover["id"])
            state["queue"] = [c for c in state["queue"] if c["id"] != cover["id"]]
            return [{"chat_id": -1002392274488, "ok": True, "message_id": 55}]
        return _pub
    return state, make_pub


def test_missing_source_auto_skipped_next_published(monkeypatch, env):
    state, make_pub = env
    state["queue"] = [_cover(1), _cover(2)]
    monkeypatch.setattr(posting, "publish_cover_to_mains", make_pub(fail_ids={1}))
    bot = FakeBot()
    out = asyncio.run(posting.publish_next(bot))
    assert out and out["id"] == 2          # next cover published
    assert state["skipped"] == [1]         # bad one removed from queue
    assert state["published"] == [2]
    dms_to = {cid for cid, _ in bot.dms}
    assert {999, 111} <= dms_to            # super admin + admin alerted
    txt = bot.dms[0][1]
    assert "skipped" in txt and "source message not found" in txt
    assert "Some Title" in txt and "#2001" in txt  # predicted number 2000+1


def test_other_errors_still_stop(monkeypatch, env):
    state, make_pub = env
    state["queue"] = [_cover(1), _cover(2)]
    monkeypatch.setattr(posting, "publish_cover_to_mains",
                        make_pub(fail_ids={1}, err="Flood control exceeded"))
    bot = FakeBot()
    out = asyncio.run(posting.publish_next(bot))
    assert out is None                     # stops as before
    assert state["skipped"] == []          # nothing auto-skipped
    assert bot.dms == []                   # no alert


def test_batch_continues_past_deleted(monkeypatch, env):
    state, make_pub = env
    state["queue"] = [_cover(1), _cover(2), _cover(3), _cover(4)]
    monkeypatch.setattr(posting, "publish_cover_to_mains", make_pub(fail_ids={1, 2}))
    bot = FakeBot()
    published = asyncio.run(posting.publish_batch(bot, 4))
    assert [c["id"] for c in published] == [3, 4]   # skipped 1,2 then posted 3,4
    assert state["skipped"] == [1, 2]
    assert len(bot.dms) == 4                          # 2 skips × 2 admins


def test_empty_queue_returns_none(monkeypatch, env):
    state, make_pub = env
    monkeypatch.setattr(posting, "publish_cover_to_mains", make_pub(set()))
    assert asyncio.run(posting.publish_next(FakeBot())) is None


def test_run_of_deletions_bounded(monkeypatch, env):
    """60 deleted covers in a row: loop must terminate (safety cap)."""
    state, make_pub = env
    state["queue"] = [_cover(i) for i in range(1, 61)]
    monkeypatch.setattr(posting, "publish_cover_to_mains",
                        make_pub(fail_ids=set(range(1, 61))))
    bot = FakeBot()
    out = asyncio.run(posting.publish_next(bot))
    assert out is None
    assert len(state["skipped"]) == 50      # stopped at the safety bound
