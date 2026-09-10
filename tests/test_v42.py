"""v4.2: parallel delivery, fsub 15-min cache, similar refresh + 60s
auto-delete, customizable /start, /mystats, /debug RAM lines."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo, posting, fsub, recommend
from app.handlers import setup_cmds, callbacks, diag_cmds


# ---- 1. fsub 15-minute membership cache ----
def test_fsub_membership_cached_15min(monkeypatch):
    fsub._member_cache.clear()
    calls = {"n": 0}

    class _M:
        status = "member"

    class _Bot:
        async def get_chat_member(self, chat_id, user_id):
            calls["n"] += 1
            return _M()

    async def _list():
        return [{"chat_id": -100, "link": "https://t.me/x", "title": "X"}]
    monkeypatch.setattr(fsub, "list_fsub", _list)

    assert asyncio.run(fsub.unjoined_channels(_Bot(), 7)) == []
    assert asyncio.run(fsub.unjoined_channels(_Bot(), 7)) == []
    assert calls["n"] == 1            # 2nd check served from cache, no API call
    assert fsub.cache_size() == 1


def test_fsub_cache_never_caches_failures(monkeypatch):
    fsub._member_cache.clear()
    calls = {"n": 0}

    class _M:
        status = "left"

    class _Bot:
        async def get_chat_member(self, chat_id, user_id):
            calls["n"] += 1
            return _M()

    async def _list():
        return [{"chat_id": -100, "link": "https://t.me/x", "title": "X"}]
    async def _no_request(cid, uid):
        return False
    monkeypatch.setattr(fsub, "list_fsub", _list)
    monkeypatch.setattr(fsub.repo, "has_fsub_request", _no_request)

    missing = asyncio.run(fsub.unjoined_channels(_Bot(), 7))
    assert len(missing) == 1
    asyncio.run(fsub.unjoined_channels(_Bot(), 7))
    assert calls["n"] == 2            # failure NOT cached → re-checked (Join+Retry works)


# ---- 2. parallel file delivery ----
def _patch_delivery_common(monkeypatch):
    async def _true(*a, **k): return True
    async def _false(*a, **k): return False
    async def _none(*a, **k): return None
    async def _empty(*a, **k): return []
    async def _cap(*a, **k): return "cap"
    import app.services.fsub as _fs
    import app.services.autodelete as _ad
    monkeypatch.setattr(_fs, "check_or_gate", _true)
    monkeypatch.setattr(_ad, "schedule", _none)
    monkeypatch.setattr(posting, "_protect", _false)
    monkeypatch.setattr(posting, "_spoiler", _false)
    monkeypatch.setattr(posting, "build_cover_caption", _cap)
    monkeypatch.setattr(posting, "build_file_caption", _cap)
    monkeypatch.setattr(posting, "_send_similar", _none)
    monkeypatch.setattr(posting.repo, "record_file_fetch", _none)
    monkeypatch.setattr(posting.repo, "record_fetch_weekly", _none)
    monkeypatch.setattr(posting.repo, "track_user_seen", _none)
    monkeypatch.setattr(posting.repo, "list_favorites", _empty)


def test_delivery_files_go_out_in_parallel(monkeypatch):
    _patch_delivery_common(monkeypatch)
    files = [{"id": i, "media_kind": "document", "source_chat_id": -1,
              "source_message_id": i, "caption": "f"} for i in range(1, 7)]
    inflight = {"cur": 0, "max": 0}

    async def _files_of_cover(a, b): return files
    async def _copy(bot, **kw):
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        await asyncio.sleep(0.02)
        inflight["cur"] -= 1
        return SimpleNamespace(message_id=kw["message_id"] * 10)
    monkeypatch.setattr(posting.repo, "files_of_cover", _files_of_cover)
    monkeypatch.setattr(posting.tg, "copy_message", _copy)

    cover = {"id": 1, "caption": "t", "post_number": 5, "media_kind": "photo",
             "file_id": "", "source_chat_id": -1, "source_message_id": 99}
    res = asyncio.run(posting.deliver_to_user(SimpleNamespace(), 42, cover))
    assert res["ok"] and res["delivered"] == 6 and res["total"] == 6
    assert inflight["max"] >= 2      # genuinely concurrent, not sequential


def test_delivery_one_bad_file_does_not_block_others(monkeypatch):
    _patch_delivery_common(monkeypatch)
    files = [{"id": i, "media_kind": "document", "source_chat_id": -1,
              "source_message_id": i, "caption": "f"} for i in range(1, 5)]

    async def _files_of_cover(a, b): return files
    async def _copy(bot, **kw):
        if kw["message_id"] == 2:
            raise RuntimeError("boom")
        return SimpleNamespace(message_id=kw["message_id"])
    monkeypatch.setattr(posting.repo, "files_of_cover", _files_of_cover)
    monkeypatch.setattr(posting.tg, "copy_message", _copy)

    cover = {"id": 1, "caption": "t", "post_number": 5, "media_kind": "photo",
             "file_id": "", "source_chat_id": -1, "source_message_id": 99}
    res = asyncio.run(posting.deliver_to_user(SimpleNamespace(), 42, cover))
    assert res["ok"] and res["delivered"] == 3 and res["total"] == 4


# ---- 3+4+5. similar card: refresh re-roll, edit-in-place, 60s self-destruct ----
def test_similar_card_has_refresh_and_self_destruct_timer(monkeypatch):
    posting._pending_similar.clear()
    async def _pref(uid): return True
    async def _sims(cover, limit=5):
        return [{"post_number": i, "code": f"c{i}", "caption": f"Title {i}"}
                for i in range(1, 4)]
    async def _uname(bot): return "mybot"
    sent = {}
    async def _send(bot, **kw):
        sent.update(kw)
        return SimpleNamespace(message_id=555)
    monkeypatch.setattr(posting.repo, "get_similar_pref", _pref)
    monkeypatch.setattr(recommend, "similar_covers", _sims)
    monkeypatch.setattr(posting, "get_bot_username", _uname)
    monkeypatch.setattr(posting.tg, "send_message", _send)

    # NOTE: asyncio.run() cancels pending tasks at shutdown and the task's
    # finally-block would pop the dict entry — so drive the loop manually.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(posting._send_similar(
            SimpleNamespace(), 42, {"id": 1, "code": "abc", "caption": "x"}, []))
        rows = sent["reply_markup"].inline_keyboard
        assert rows[-1][0].text == "🔄 Refresh"
        assert rows[-1][0].callback_data == "simref:abc"
        assert (42, 555) in posting._pending_similar   # 60s timer armed
        posting._pending_similar.pop((42, 555)).cancel()
    finally:
        loop.close()


def test_similar_refresh_edits_in_place_and_cancels_timer(monkeypatch):
    posting._pending_similar.clear()
    pool = [{"post_number": i, "code": f"c{i}", "caption": f"Title {i}"}
            for i in range(1, 21)]
    async def _sims(cover, limit=5): return pool[:limit]
    async def _cover(code): return {"id": 1, "kind": "cover", "code": code, "caption": "x"}
    async def _uname(bot): return "mybot"
    monkeypatch.setattr(recommend, "similar_covers", _sims)
    monkeypatch.setattr(callbacks.repo, "get_post_by_code", _cover)
    monkeypatch.setattr(posting, "get_bot_username", _uname)

    cancelled = {"n": 0}
    class _Task:
        def cancel(self): cancelled["n"] += 1
    posting._pending_similar[(7, 55)] = _Task()

    edited = {}
    class _Msg:
        chat = SimpleNamespace(id=7)
        message_id = 55
        async def edit_reply_markup(self, reply_markup=None):
            edited["kb"] = reply_markup
    answers = []
    async def _answer(t="", **k): answers.append(t)
    cb = SimpleNamespace(data="simref:abc", from_user=SimpleNamespace(id=7),
                         message=_Msg(), answer=_answer)

    asyncio.run(callbacks.on_similar_refresh(cb, SimpleNamespace()))
    assert cancelled["n"] == 1                    # auto-delete cancelled
    assert (7, 55) not in posting._pending_similar
    kb = edited["kb"].inline_keyboard
    assert kb[-1][0].text == "🔄 Refresh"          # refresh button preserved
    assert 1 <= len(kb) - 1 <= 5                   # fresh picks rendered
    assert "Fresh picks" in answers[-1]


# ---- 6. customizable /start ----
def test_custom_start_text_buttons_and_photo(monkeypatch):
    async def _gs(key, default=None):
        return {"start_text": "Welcome <blockquote>read rules</blockquote>",
                "start_photo_id": ""}.get(key, default)
    async def _gsj(key, default=None):
        return {"start_buttons": [["📢 Channel", "https://t.me/x"],
                                  ["💬 Chat", "https://t.me/y"]]}.get(key, default)
    monkeypatch.setattr(setup_cmds.repo, "get_setting", _gs)
    monkeypatch.setattr(setup_cmds.repo, "get_setting_json", _gsj)

    sent = {}
    class _M:
        async def answer(self, text, **kw):
            sent["text"] = text; sent["kb"] = kw.get("reply_markup")
        async def answer_photo(self, **kw):
            sent["photo"] = kw
    asyncio.run(setup_cmds._send_start_message(_M()))
    assert "<blockquote>" in sent["text"]
    rows = sent["kb"].inline_keyboard
    assert [r[0].text for r in rows] == ["📢 Channel", "💬 Chat"]
    assert rows[0][0].url == "https://t.me/x"


def test_start_falls_back_to_default_and_sends_photo(monkeypatch):
    async def _gs(key, default=None):
        return {"start_photo_id": "PHOTO_ID"}.get(key, default)
    async def _gsj(key, default=None): return default
    monkeypatch.setattr(setup_cmds.repo, "get_setting", _gs)
    monkeypatch.setattr(setup_cmds.repo, "get_setting_json", _gsj)

    sent = {}
    class _M:
        async def answer(self, text, **kw): sent["text"] = text
        async def answer_photo(self, **kw): sent["photo"] = kw
    asyncio.run(setup_cmds._send_start_message(_M()))
    assert sent["photo"]["photo"] == "PHOTO_ID"
    assert "Welcome" in sent["photo"]["caption"]   # default text as caption


# ---- 7. /debug RAM ----
def test_debug_ram_lines():
    lines = diag_cmds._ram_lines()
    assert isinstance(lines, list) and lines
    assert any("MB" in l or "unavailable" in l for l in lines)
