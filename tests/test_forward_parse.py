"""v3.2.1 regression: /forward must accept space- OR comma-separated destinations."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.handlers import forward_cmds


class _Bot:
    async def send_message(self, *a, **k): return SimpleNamespace(message_id=1)


def _mk(text):
    replies = []
    async def _reply(t, **kw): replies.append(str(t)); return None
    m = SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), reply=_reply)
    return m, replies


@pytest.fixture
def env(monkeypatch):
    cap = {}
    async def _false(_m): return False
    async def _start(bot, admin_id, source_ref, dest_refs, a, b):
        cap.update(source=source_ref, dests=list(dest_refs), a=a, b=b)
        return True, "started"
    async def _resolve(uname): return f"entity:{uname}"
    monkeypatch.setattr(forward_cmds, "_reject_non_admin", _false)
    monkeypatch.setattr(forward_cmds.ub, "forward_start", _start)
    monkeypatch.setattr(forward_cmds.ub, "resolve_channel_ref", _resolve)
    return cap


def test_space_separated_dests_user_command(env):
    """The EXACT command from the bug report must parse."""
    m, replies = _mk("/forward -1001789258409 -1004303729375 "
                     "https://t.me/Skeleton_Soldier_Couldnt_Protec/952 "
                     "https://t.me/Skeleton_Soldier_Couldnt_Protec/963")
    asyncio.run(forward_cmds.cmd_forward(m, _Bot()))
    assert env["dests"] == [-1001789258409, -1004303729375]
    assert env["a"] == 952 and env["b"] == 963
    assert env["source"] == "entity:Skeleton_Soldier_Couldnt_Protec"
    assert replies == ["started"]


def test_comma_separated_dests_still_work(env):
    m, _ = _mk("/forward -100111,-100222 https://t.me/c/555/10 https://t.me/c/555/20")
    asyncio.run(forward_cmds.cmd_forward(m, _Bot()))
    assert env["dests"] == [-100111, -100222]
    assert env["source"] == -100555          # t.me/c/ links carry the id inline
    assert env["a"] == 10 and env["b"] == 20


def test_single_dest_and_reversed_links_swapped(env):
    m, _ = _mk("/forward -100111 https://t.me/c/555/963 https://t.me/c/555/952")
    asyncio.run(forward_cmds.cmd_forward(m, _Bot()))
    assert env["a"] == 952 and env["b"] == 963   # auto-swapped


def test_links_from_different_channels_rejected(env):
    m, replies = _mk("/forward -100111 https://t.me/c/555/10 https://t.me/c/777/20")
    asyncio.run(forward_cmds.cmd_forward(m, _Bot()))
    assert env == {} and any("different channels" in r for r in replies)


def test_no_dests_shows_usage(env):
    m, replies = _mk("/forward https://t.me/c/555/10 https://t.me/c/555/20")
    asyncio.run(forward_cmds.cmd_forward(m, _Bot()))
    assert env == {} and any("Usage" in r for r in replies)
