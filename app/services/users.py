"""User registration, roles, moderation, streaks, favorites."""
from __future__ import annotations

import datetime as _dt
import json
from typing import Optional

from .. import db
from ..utils import now_iso


def upsert_user(user_id, username=None, first_name=None, last_name=None):
    db.execute(
        "INSERT INTO users (telegram_user_id, username, first_name, last_name, joined_at, last_active_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(telegram_user_id) DO UPDATE SET "
        "username=COALESCE(excluded.username, users.username), "
        "first_name=COALESCE(excluded.first_name, users.first_name), "
        "last_name=COALESCE(excluded.last_name, users.last_name), "
        "last_active_at=excluded.last_active_at",
        (user_id, username, first_name, last_name, now_iso(), now_iso()))


def is_admin(user_id) -> bool:
    return db.query_scalar("SELECT 1 FROM admins WHERE telegram_user_id=?", (user_id,)) is not None


def is_super_admin(user_id) -> bool:
    return db.query_scalar("SELECT 1 FROM admins WHERE telegram_user_id=? AND is_super_admin=1", (user_id,)) is not None


def add_admin(user_id, username, first_name, is_super, added_by):
    db.execute(
        "INSERT INTO admins (telegram_user_id, username, first_name, is_super_admin, added_by) "
        "VALUES (?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET "
        "is_super_admin=excluded.is_super_admin, "
        "username=COALESCE(excluded.username, admins.username), "
        "first_name=COALESCE(excluded.first_name, admins.first_name)",
        (user_id, username, first_name, 1 if is_super else 0, added_by))


def remove_admin(user_id):
    db.execute("DELETE FROM admins WHERE telegram_user_id=?", (user_id,))


def list_admins() -> list[dict]:
    return db.query_all("SELECT * FROM admins ORDER BY is_super_admin DESC, id ASC")


def is_banned(user_id) -> bool:
    return db.query_scalar("SELECT is_banned FROM users WHERE telegram_user_id=?", (user_id,)) == 1


def set_ban(user_id, banned: bool, reason=None):
    db.execute("UPDATE users SET is_banned=?, ban_reason=? WHERE telegram_user_id=?",
               (1 if banned else 0, reason, user_id))


def list_banned() -> list[dict]:
    return db.query_all("SELECT * FROM users WHERE is_banned=1")


def unban_all() -> int:
    n = int(db.query_scalar("SELECT COUNT(*) FROM users WHERE is_banned=1") or 0)
    db.execute("UPDATE users SET is_banned=0, ban_reason=NULL WHERE is_banned=1")
    return n


def user_count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM users") or 0)


def banned_count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM users WHERE is_banned=1") or 0)


def _today() -> str:
    return _dt.date.today().isoformat()


def _yesterday() -> str:
    return (_dt.date.today() - _dt.timedelta(days=1)).isoformat()


def bump_streak(user_id):
    row = db.query_one("SELECT current, longest, last_fetch_day FROM user_streaks WHERE user_id=?", (user_id,))
    today = _today()
    if row is None:
        db.execute("INSERT INTO user_streaks (user_id, current, longest, last_fetch_day) VALUES (?,1,1,?)",
                   (user_id, today))
        return
    if row.get("last_fetch_day") == today:
        return
    current = int(row["current"] or 0)
    longest = int(row["longest"] or 0)
    new_current = current + 1 if row.get("last_fetch_day") == _yesterday() else 1
    db.execute("UPDATE user_streaks SET current=?, longest=?, last_fetch_day=? WHERE user_id=?",
               (new_current, max(longest, new_current), today, user_id))


def get_streak(user_id) -> dict:
    return db.query_one("SELECT current, longest FROM user_streaks WHERE user_id=?", (user_id,)) or {"current": 0, "longest": 0}


def log_activity(actor_id, action, details=None):
    db.execute("INSERT INTO activity_log (actor_id, action, details) VALUES (?,?,?)",
               (actor_id, action, json.dumps(details, ensure_ascii=False, default=str) if details else None))


def write_audit(admin_id, action, target=None, details=None):
    db.execute("INSERT INTO admin_audit (admin_id, action, target, details) VALUES (?,?,?,?)",
               (admin_id, action, target, json.dumps(details, ensure_ascii=False, default=str) if details else None))


def add_favorite(user_id, post_id):
    db.execute("INSERT INTO favorites (user_id, post_id) VALUES (?,?) ON CONFLICT DO NOTHING",
               (user_id, post_id))


def remove_favorite(user_id, post_id):
    db.execute("DELETE FROM favorites WHERE user_id=? AND post_id=?", (user_id, post_id))


def list_favorites(user_id, limit=50) -> list[dict]:
    return db.query_all(
        "SELECT p.* FROM favorites f JOIN posts p ON p.id=f.post_id "
        "WHERE f.user_id=? ORDER BY f.created_at DESC LIMIT ?",
        (user_id, limit))


def add_warning(user_id, admin_id, reason) -> int:
    db.execute("INSERT INTO warnings (user_id, admin_id, reason) VALUES (?,?,?)",
               (user_id, admin_id, reason))
    db.execute("UPDATE users SET warn_count=warn_count+1 WHERE telegram_user_id=?", (user_id,))
    return int(db.query_scalar("SELECT warn_count FROM users WHERE telegram_user_id=?", (user_id,)) or 0)


def list_warnings(user_id) -> list[dict]:
    return db.query_all("SELECT * FROM warnings WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,))


def clear_warnings(user_id):
    db.execute("DELETE FROM warnings WHERE user_id=?", (user_id,))
    db.execute("UPDATE users SET warn_count=0 WHERE telegram_user_id=?", (user_id,))
