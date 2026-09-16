"""Inline-button callbacks for ❤️ Save / 🗑 Remove on delivered files."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import CallbackQuery

from ..services import repo

log = logging.getLogger("callbacks")
router = Router(name="callbacks")


@router.callback_query(lambda c: (c.data or "").startswith("save:"))
async def on_save(cb: CallbackQuery) -> None:
    try:
        pid = int((cb.data or "").split(":", 1)[1])
    except Exception:
        await cb.answer("Bad data.")
        return
    await repo.add_favorite(cb.from_user.id, pid)
    await cb.answer("❤️ Saved!")


@router.callback_query(lambda c: (c.data or "").startswith("unsave:"))
async def on_unsave(cb: CallbackQuery) -> None:
    try:
        pid = int((cb.data or "").split(":", 1)[1])
    except Exception:
        await cb.answer("Bad data.")
        return
    await repo.remove_favorite(cb.from_user.id, pid)
    await cb.answer("🗑 Removed.")


@router.callback_query(lambda c: (c.data or "").startswith("simref:"))
async def on_similar_refresh(cb: CallbackQuery, bot: Bot) -> None:
    import asyncio
    import random
    from ..services import posting
    from ..services import recommend as _rec
    code = (cb.data or "").split(":", 1)[1].strip()
    cover = await repo.get_post_by_code(code)
    if not cover or cover.get("kind") != "cover":
        await cb.answer("❌ This post is no longer available.")
        return
    chat_id = cb.message.chat.id if cb.message else cb.from_user.id
    mid = cb.message.message_id if cb.message else 0
    task = posting._pending_similar.pop((chat_id, mid), None)
    if task:
        task.cancel()
    pool = await _rec.similar_covers(cover, limit=20)
    if not pool:
        await cb.answer("😕 No more recommendations right now.")
        return
    pick = random.sample(pool, k=min(_rec.MAX_RESULTS, len(pool)))
    username = await posting.get_bot_username(bot)
    rows = posting._similar_rows(pick, username)
    if not rows:
        await cb.answer("😕 No more recommendations right now.")
        return
    kb = posting._similar_markup(rows, code)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
        await cb.answer("🔄 Fresh picks!")
        return
    except Exception:
        pass
    if cb.message:
        try:
            await cb.message.delete()
        except Exception:
            pass
    r = await bot.send_message(chat_id, posting.SIMILAR_TEXT, reply_markup=kb, parse_mode="HTML")
    new_mid = getattr(r, "message_id", None)
    if new_mid:
        t = asyncio.create_task(posting._delete_similar_later(bot, chat_id, new_mid))
        posting._pending_similar[(chat_id, new_mid)] = t
    await cb.answer("🔄 Fresh picks!")


@router.callback_query(lambda c: (c.data or "").startswith("simref:"))
async def on_similar_refresh(cb: CallbackQuery, bot: Bot) -> None:
    """🔄 Refresh — re-roll the Similar Doujinshi pool (v4.2).

    Preferred behaviour: EDIT the existing card in place (no chat spam).
    If the message is too old to edit, delete it and post a fresh card.
    Tapping Refresh also cancels the card's 60-second auto-delete — the
    user is clearly engaged, so the card gets to stay."""
    import asyncio
    import random

    from ..services import posting
    from ..services import recommend as _rec

    code = (cb.data or "").split(":", 1)[1].strip()
    cover = await repo.get_post_by_code(code)
    if not cover or cover.get("kind") != "cover":
        await cb.answer("❌ This post is no longer available.")
        return

    chat_id = cb.message.chat.id if cb.message else cb.from_user.id
    mid = cb.message.message_id if cb.message else 0

    # Engaged → stop this card's pending self-destruct.
    task = posting._pending_similar.pop((chat_id, mid), None)
    if task:
        task.cancel()

    # Re-roll: rank a wider pool (top-20), then random-sample the final 5 so
    # every tap shows a fresh mix instead of the same deterministic top-5.
    pool = await _rec.similar_covers(cover, limit=20)
    if not pool:
        await cb.answer("😕 No more recommendations right now.")
        return
    pick = random.sample(pool, k=min(_rec.MAX_RESULTS, len(pool)))
    username = await posting.get_bot_username(bot)
    rows = posting._similar_rows(pick, username)
    if not rows:
        await cb.answer("😕 No more recommendations right now.")
        return
    kb = posting._similar_markup(rows, code)

    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
        await cb.answer("🔄 Fresh picks!")
        return
    except Exception:
        pass  # message too old to edit → replace it below

    if cb.message:
        try:
            await cb.message.delete()
        except Exception:
            pass
    r = await bot.send_message(chat_id, posting.SIMILAR_TEXT,
                               reply_markup=kb, parse_mode="HTML")
    new_mid = getattr(r, "message_id", None)
    if new_mid:
        t = asyncio.create_task(posting._delete_similar_later(bot, chat_id, new_mid))
        posting._pending_similar[(chat_id, new_mid)] = t
    await cb.answer("🔄 Fresh picks!")


@router.callback_query(lambda c: (c.data or "").startswith("vrfchk:"))
async def on_verify_check(cb: CallbackQuery, bot: Bot) -> None:
    """v4.3.6: '✅ I've Verified — Continue' is a STATUS CHECK, not redemption.

    Redemption happens ONLY through the shortener-gated deep link
    (start=verify_TOKEN). A pending token in MongoDB proves nothing — it was
    minted the moment the gate was shown. This closes the bypass where
    tapping the button right after the gate verified the user without ever
    solving the short link."""
    from ..services import shortener as _sh
    from ..services import posting
    if not await _sh.is_verified(cb.from_user.id):
        await cb.answer(
            "❌ Not verified yet — tap 🔓 Verify & Unlock, finish the short "
            "link, and press Start when Telegram opens.",
            show_alert=True)
        return
    await cb.answer("✅ You're verified!")
    _sh.clear_gate(cb.from_user.id)  # v4.3.7: gate becomes the success text
    try:
        await cb.message.edit_text(await _sh.get_verify_text(), parse_mode="HTML")
    except Exception:
        pass
    # Deliver the cover this gate was for (rescue path: user verified via the
    # deep link but came back to the gate message).
    code = (cb.data or "").split(":", 1)[1].strip()
    if code:
        cover = await repo.get_post_by_code(code)
        if cover and cover.get("kind") == "cover":
            await posting.deliver_to_user(bot, cb.from_user.id, cover)
