"""User registration, roles, stats, engagement."""
from __future__ import annotations

from typing import Any, Optional

from .. import db
from ..utils import now_iso


def upsert_user(user_id: int, username: str | None = None,
                first_name: str | None = None, last_name: str | None = None) -> None:
    db.execute(
        "INSERT INTO users (telegram_user_id, username, first_name, last_name, joined_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(telegram_user_id) DO UPDATE SET "
        "username = COALESCE(excluded.username, users.username), "
        "first_name = COALESCE(excluded.first_name, users.first_name), "
        "last_name = COALESCE(excluded.last_name, users.last_name), "
        "last_active_at = excluded.last_active_at",
        (user_id, username, first_name, last_name, now_iso()),
    )


def is_admin(user_id: int) -> bool:
    return db.query_scalar(
        "SELECT 1 FROM admins WHERE telegram_user_id = ?", (user_id,)
    ) is not None


def is_super_admin(user_id: int) -> bool:
    return db.query_scalar(
        "SELECT 1 FROM admins WHERE telegram_user_id = ? AND is_super_admin = 1", (user_id,)
    ) is not None


def add_admin(user_id: int, username: str | None, first_name: str | None,
              is_super: bool, added_by: int) -> None:
    db.execute(
        "INSERT INTO admins (telegram_user_id, username, first_name, is_super_admin, added_by) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(telegram_user_id) DO UPDATE SET "
        "is_super_admin = excluded.is_super_admin",
        (user_id, username, first_name, 1 if is_super else 0, added_by),
    )


def remove_admin(user_id: int) -> None:
    db.execute("DELETE FROM admins WHERE telegram_user_id = ?", (user_id,))


def list_admins() -> list[dict]:
    return db.query_all("SELECT * FROM admins ORDER BY is_super_admin DESC, id ASC")


def is_banned(user_id: int) -> bool:
    return db.query_scalar(
        "SELECT is_banned FROM users WHERE telegram_user_id = ?", (user_id,)
    ) == 1


def set_ban(user_id: int, banned: bool, reason: str | None = None) -> None:
    db.execute(
        "UPDATE users SET is_banned = ?, ban_reason = ? WHERE telegram_user_id = ?",
        (1 if banned else 0, reason, user_id),
    )


def bump_streak(user_id: int) -> None:
    row = db.query_one("SELECT current, longest, last_fetch_day FROM user_streaks WHERE user_id = ?", (user_id,))
    today = now_iso()[:10]
    if row is None:
        db.execute(
            "INSERT INTO user_streaks (user_id, current, longest, last_fetch_day) VALUES (?, 1, 1, ?)",
            (user_id, today),
        )
        return
    last = row.get("last_fetch_day")
    current = row["current"]
    longest = row["longest"]
    if last == today:
        return  # already counted today
    new_current = current + 1 if last and last == _days_ago(1) else 1
    db.execute(
        "UPDATE user_streaks SET current = ?, longest = MAX(longest, ?), last_fetch_day = ? WHERE user_id = ?",
        (new_current, new_current, today, user_id),
    )
    if new_current > longest:
        db.execute("UPDATE user_streaks SET longest = ? WHERE user_id = ?", (new_current, user_id))


def _days_ago(n: int) -> str:
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def log_activity(actor_id: int, action: str, details: dict | None = None) -> None:
    import json
    db.execute(
        "INSERT INTO activity_log (actor_id, action, details) VALUES (?, ?, ?)",
        (actor_id, action, json.dumps(details, ensure_ascii=False, default=str) if details else None),
    )


def write_audit(admin_id: int, action: str, target: str | None = None,
                details: dict | None = None) -> None:
    import json
    db.execute(
        "INSERT INTO admin_audit (admin_id, action, target, details) VALUES (?, ?, ?, ?)",
        (admin_id, action, target, json.dumps(details, ensure_ascii=False, default=str) if details else None),
    )


def add_favorite(user_id: int, post_id: int) -> None:
    db.execute(
        "INSERT INTO favorites (user_id, post_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (user_id, post_id),
    )


def remove_favorite(user_id: int, post_id: int) -> None:
    db.execute("DELETE FROM favorites WHERE user_id = ? AND post_id = ?", (user_id, post_id))


def list_favorites(user_id: int, limit: int = 50) -> list[dict]:
    return db.query_all(
        "SELECT p.* FROM favorites f JOIN posts p ON p.id = f.post_id "
        "WHERE f.user_id = ? ORDER BY f.created_at DESC LIMIT ?",
        (user_id, limit),
    )
