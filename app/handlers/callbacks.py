"""Inline-callback handling (favorites + ratings)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import db
from ..services import users
from ..services.tg import answer_callback

router = Router(name="callbacks")


def _get_post(code: str):
    return db.query_one("SELECT * FROM posts WHERE code=?", (code,))


@router.callback_query(F.data.startswith("fav:"))
async def on_fav(callback: CallbackQuery):
    post = _get_post(callback.data[4:])
    if not post:
        await answer_callback(callback.id, "Not found", show_alert=True); return
    users.add_favorite(callback.from_user.id, post["id"])
    await answer_callback(callback.id, "Saved to favorites ❤️")


@router.callback_query(F.data.startswith("unfav:"))
async def on_unfav(callback: CallbackQuery):
    post = _get_post(callback.data[6:])
    if not post:
        await answer_callback(callback.id, "Not found", show_alert=True); return
    users.remove_favorite(callback.from_user.id, post["id"])
    await answer_callback(callback.id, "Removed 🗑")


@router.callback_query(F.data.startswith("rateup:"))
async def on_rate_up(callback: CallbackQuery):
    post = _get_post(callback.data[7:])
    if not post:
        await answer_callback(callback.id, "Not found", show_alert=True); return
    db.execute("INSERT INTO post_ratings (post_id, up) VALUES (?,1) ON CONFLICT(post_id) DO UPDATE SET up=up+1", (post["id"],))
    db.execute("INSERT INTO user_post_ratings (user_id, post_id, vote) VALUES (?,?,'up') ON CONFLICT(user_id, post_id) DO UPDATE SET vote='up'",
               (callback.from_user.id, post["id"]))
    await answer_callback(callback.id, "👍 Thanks!")


@router.callback_query(F.data.startswith("ratedown:"))
async def on_rate_down(callback: CallbackQuery):
    post = _get_post(callback.data[9:])
    if not post:
        await answer_callback(callback.id, "Not found", show_alert=True); return
    db.execute("INSERT INTO post_ratings (post_id, down) VALUES (?,1) ON CONFLICT(post_id) DO UPDATE SET down=down+1", (post["id"],))
    db.execute("INSERT INTO user_post_ratings (user_id, post_id, vote) VALUES (?,?,'down') ON CONFLICT(user_id, post_id) DO UPDATE SET vote='down'",
               (callback.from_user.id, post["id"]))
    await answer_callback(callback.id, "👎 Noted.")
