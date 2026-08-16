"""Inline-callback handling (favorites / ratings)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import db
from ..services import users, posting
from ..services.tg import answer_callback

router = Router(name="callbacks")


@router.callback_query(F.data.startswith("fav:"))
async def on_fav(callback: CallbackQuery):
    code = callback.data[4:]
    post = db.query_one("SELECT * FROM posts WHERE code = ?", (code,))
    if not post:
        await answer_callback(callback.id, "Not found")
        return
    users.add_favorite(callback.from_user.id, post["id"])
    await answer_callback(callback.id, "Saved to favorites ❤️")


@router.callback_query(F.data.startswith("rateup:"))
async def on_rate_up(callback: CallbackQuery):
    code = callback.data[7:]
    post = db.query_one("SELECT * FROM posts WHERE code = ?", (code,))
    if not post:
        await answer_callback(callback.id, "Not found")
        return
    db.execute("INSERT INTO post_ratings (post_id, up) VALUES (?, 1) ON CONFLICT(post_id) DO UPDATE SET up = up + 1", (post["id"],))
    db.execute("INSERT INTO user_post_ratings (user_id, post_id, vote) VALUES (?, ?, 'up') ON CONFLICT(user_id, post_id) DO UPDATE SET vote='up'",
               (callback.from_user.id, post["id"]))
    await answer_callback(callback.id, "👍 Thanks!")


@router.callback_query(F.data.startswith("ratedown:"))
async def on_rate_down(callback: CallbackQuery):
    code = callback.data[9:]
    post = db.query_one("SELECT * FROM posts WHERE code = ?", (code,))
    if not post:
        await answer_callback(callback.id, "Not found")
        return
    db.execute("INSERT INTO post_ratings (post_id, down) VALUES (?, 1) ON CONFLICT(post_id) DO UPDATE SET down = down + 1", (post["id"],))
    db.execute("INSERT INTO user_post_ratings (user_id, post_id, vote) VALUES (?, ?, 'down') ON CONFLICT(user_id, post_id) DO UPDATE SET vote='down'",
               (callback.from_user.id, post["id"]))
    await answer_callback(callback.id, "👎 Noted.")
