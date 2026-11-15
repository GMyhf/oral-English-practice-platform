# services/auth.py
"""账号相关：注册、登录校验、改用户名/密码、练习偏好设置。"""
import re

from db import get_db
from werkzeug.security import check_password_hash, generate_password_hash

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{3,32}$")
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128

ALLOWED_VOICES = ("f", "m")


class AuthError(Exception):
    """注册/登录校验失败，消息可以直接展示给用户。"""


def _row_to_user(row):
    if row is None:
        return None
    return dict(row)


def get_user_by_name(username):
    row = get_db().execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    return _row_to_user(row)


def get_user(user_id):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row)


def register(username, password, nickname=None):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("用户名需为 3-32 位字母、数字、下划线或中文")
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise AuthError("密码过长")
    if get_user_by_name(username):
        raise AuthError("该用户名已被注册")

    db = get_db()
    cur = db.execute(
        "INSERT INTO users(username, password_hash, nickname) VALUES(?,?,?)",
        (username, generate_password_hash(password), (nickname or username).strip()),
    )
    db.commit()
    return get_user(cur.lastrowid)


def verify_login(username, password):
    user = get_user_by_name(username)
    if not user or not check_password_hash(user["password_hash"], password or ""):
        raise AuthError("用户名或密码错误")
    return user


def change_password(user_id, old_password, new_password):
    user = get_user(user_id)
    if not user:
        raise AuthError("用户不存在")
    if not check_password_hash(user["password_hash"], old_password or ""):
        raise AuthError("原密码不正确")
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        raise AuthError(f"新密码至少 {MIN_PASSWORD_LEN} 位")

    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    db.commit()


def change_username(user_id, new_username):
    new_username = (new_username or "").strip()
    if not USERNAME_RE.match(new_username):
        raise AuthError("用户名需为 3-32 位字母、数字、下划线或中文")
    existing = get_user_by_name(new_username)
    if existing and existing["id"] != user_id:
        raise AuthError("该用户名已被占用")

    db = get_db()
    db.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
    db.commit()
    return new_username


def update_settings(user_id, default_count=None, time_limit=None, voice=None):
    import config

    db = get_db()
    if default_count is not None:
        count = _clamp(default_count, 1, config.MAX_QUESTION_COUNT, config.DEFAULT_QUESTION_COUNT)
        db.execute("UPDATE users SET default_count = ? WHERE id = ?", (count, user_id))
    if time_limit is not None:
        limit = _clamp(time_limit, 0.2, 30, config.DEFAULT_TIME_LIMIT)
        db.execute("UPDATE users SET time_limit = ? WHERE id = ?", (limit, user_id))
    if voice is not None:
        voice = voice if voice in ALLOWED_VOICES else "f"
        db.execute("UPDATE users SET voice = ? WHERE id = ?", (voice, user_id))
    db.commit()


def _clamp(value, low, high, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))
