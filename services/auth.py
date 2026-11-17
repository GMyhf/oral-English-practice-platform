# services/auth.py
"""账号相关：注册、登录校验、改用户名/密码、练习偏好设置。"""
import re
import sqlite3
import threading

import config
from db import get_db
from werkzeug.security import check_password_hash, generate_password_hash

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{3,32}$")
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128

ALLOWED_VOICES = ("f", "m")


class AuthError(Exception):
    """注册/登录校验失败，消息可以直接展示给用户。"""


class HashQueueFull(Exception):
    """哈希闸门等不到位置。

    正常情况下不会触发：单核机器上排队深度最多是「处理线程数 - 闸门数」，
    等的是几十毫秒量级。真等到超时，说明前面有一大群人挤在登录页，
    这时快速回 503 让人重试，比让请求一直挂着、最后被 nginx 60 秒超时切成 504 好。
    """


# ------------------------------------------------------------------ 密码哈希
#
# 所有 generate/check 都必须过这道信号量，理由见 config.PASSWORD_HASH_METHOD 上面
# 那段实测数字：一次 scrypt 要 16MB，放开 8 条处理线一起算就是 128MB，
# 而 2026-11-17 之前那次「12 人同时点登录把整机拖进直接回收死循环」正是这个形状。
# 信号量是 per-process 的（gunicorn preload 之后 fork，每个 worker 各有一份），
# 所以整机上限 = workers × MAX_CONCURRENT_HASHES = 2 × 2 = 4 次，约 64MB。
_hash_gate = threading.BoundedSemaphore(max(1, int(config.MAX_CONCURRENT_HASHES)))


def _hash_acquire():
    if not _hash_gate.acquire(timeout=config.HASH_QUEUE_TIMEOUT):
        raise HashQueueFull()


def hash_password(password):
    """按当前策略生成哈希。"""
    _hash_acquire()
    try:
        return generate_password_hash(password, method=config.PASSWORD_HASH_METHOD)
    finally:
        _hash_gate.release()


def check_password(stored, password):
    _hash_acquire()
    try:
        return check_password_hash(stored, password or "")
    finally:
        _hash_gate.release()


def hash_method(stored):
    """哈希串的第一段就是生成它的参数：scrypt:16384:8:1$盐$摘要。"""
    return (stored or "").split("$", 1)[0]


# 用户名不存在时也要烧一次哈希的时间，否则「响应立刻返回」就等于告诉外面
# 这个用户名没被注册过 —— 而学生的用户名常常就是真名。内容和强弱都无所谓，
# 只要成本一致，所以拿当前策略签一个固定假哈希出来用。
# 这是导入时算的：每个进程一次约 90ms，不是每个请求一次。
_DUMMY_USERNAME = "__timing_pad__"
_DUMMY_HASH = generate_password_hash(_DUMMY_USERNAME, method=config.PASSWORD_HASH_METHOD)


def _align_hash(user, password):
    """校验通过后，把哈希改写成当前策略的参数。

    调参数（比如 N 从 32768 降到 16384，或者以后又调回去）之后不需要强制全班
    重置密码：谁登录一次，谁的哈希就换过来了，代价是那一次多做一遍哈希。
    所以真要改 PASSWORD_HASH_METHOD，挑课后时间改。

    这一步失败绝不影响登录本身 —— 密码已经验过了，能登就得让人登进去。
    """
    if hash_method(user["password_hash"]) == config.PASSWORD_HASH_METHOD:
        return
    if not password:          # 空密码不该走到这里，但别让它把 werkzeug 炸成 500
        return
    try:
        db = get_db()
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (hash_password(password), user["id"]))
        db.commit()
    except Exception:
        get_db().rollback()


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

    # 先算哈希再开事务：哈希要 90ms，别把写事务的锁拿着不放
    digest = hash_password(password)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users(username, password_hash, nickname) VALUES(?,?,?)",
            (username, digest, (nickname or username).strip()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # 上面那次「有没有重名」的检查挡不住真正的并发：两个人同一瞬间注册同名，
        # 后一个会撞 users.username 的 UNIQUE 约束。以前这会让请求直接 500，
        # 学生看到的是「服务器内部错误」而不是「该用户名已被注册」。
        db.rollback()
        raise AuthError("该用户名已被注册")
    return get_user(cur.lastrowid)


def verify_login(username, password):
    user = get_user_by_name(username)
    if not user:
        # 查无此人也要花掉一次哈希的时间，别让响应快慢泄露「这个用户名注册过吗」
        check_password(_DUMMY_HASH, password or _DUMMY_USERNAME)
        raise AuthError("用户名或密码错误")
    if not check_password(user["password_hash"], password):
        raise AuthError("用户名或密码错误")
    _align_hash(user, password)
    return user


def change_password(user_id, old_password, new_password):
    user = get_user(user_id)
    if not user:
        raise AuthError("用户不存在")
    if not check_password(user["password_hash"], old_password):
        raise AuthError("原密码不正确")
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        raise AuthError(f"新密码至少 {MIN_PASSWORD_LEN} 位")
    if len(new_password) > MAX_PASSWORD_LEN:
        raise AuthError("新密码过长")

    digest = hash_password(new_password)
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (digest, user_id),
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
    try:
        db.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
        db.commit()
    except sqlite3.IntegrityError:
        # 和 register() 同一个坑：上面的查重挡不住同一秒内的两次改名，
        # 撞了 users.username 的 UNIQUE 约束别让它冒成 500。
        db.rollback()
        raise AuthError("该用户名已被占用")
    return new_username


def update_settings(user_id, default_count=None, time_limit=None, voice=None):
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
