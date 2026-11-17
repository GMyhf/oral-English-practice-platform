# services/registration.py
"""注册闸门：邀请码的校验、发放、占用与吊销。

为什么是「码」而不是「用户名/学号名单」
------------------------------------
课程名单上有学号和姓名，但注册页上的用户名是学生自己起的，两者对不上，
按名单卡注册只会把全班挡在门外。所以这里卡的是一件他一定会填对的事：
一个由老师发出去的口令。名单如果以后要加，是往 `users` 上补一列的事，
不必改这里的判定。

三条设计约束（都在下面的代码里能看到落点）
----------------------------------------
1. **码存在数据库里，不写在配置文件里**：换码、加临时码、吊销泄露出去的码
   都只跑一条命令，不用重启服务。只有开关 `REGISTER_MODE` 需要重启。
2. **默认关闭（fail-closed）**：`REGISTER_MODE=invite` 而库里一个码都没有时，
   注册页显示「注册尚未开放」而不是「随便注册」。宁可配漏了让老师来问，
   也不要挂到公网那天正好是个敞开的注册口。
3. **校验排在密码哈希之前**（见 services/auth.register）：一次 scrypt 是这台
   1 核机上最贵的动作，也是 2026-11-17 那次「12 人同时登录打死整机」的成因。
   错码如果先哈希再拒，等于送外面一个「花 90ms CPU 换我 0 成本」的放大口子。

关于限流
--------
这里故意没做进程内的尝试次数限制：gunicorn 是 2 个进程，各记各的计数，
全局上限其实是配额的 2 倍，形同虚设；而按 IP 计数在这个部署里拿不到真 IP
（nginx 反代后面 Werkzeug 看到的 remote_addr 全是 127.0.0.1，仓库里没装 ProxyFix）；
更要紧的是「按目标计数」的锁闸本身就是个自我拒绝服务 —— 机器人自己撞满配额，
全班就注册不上了。挡口令爆破靠的是口令的熵（默认 34 字母表 12 位 ≈ 61 bit），
不是靠计数器。真要对公网，把限速加在 nginx 那一层（`limit_req_zone` + `/register`）。

关于明文存码
------------
邀请码是短期、低价值的凭据（一个班的练习口令，泄露了吊销重发即可），
明文存在库里才能做到「老师 list 一眼看到当前码」。换成哈希存储就再也读不出来，
收益（防库文件被读）本来也该由 `data/app.db` 的权限和备份策略去保证。
"""
import re
import secrets
import sqlite3

import config

MODES = ("open", "invite", "closed")

# 去掉容易看错的字符：0/O、1/I/l 在手机上手输几乎必错，而大写字母会被 iOS 的
# 首字母自动大写坑到 —— 所以下面统一「只出小写、比前转小写」。
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

# 建表语句放在这里只留一份：scripts/init_db.py 的 SCHEMA 引用它，
# scripts/manage_invite.py 也用它给老库补表，避免两处定义日后走偏。
TABLE_DDL = (
    """
CREATE TABLE IF NOT EXISTS invite_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL UNIQUE,      -- 统一存小写，见 normalize()
    note         TEXT,                         -- 这批码发给谁，纯备注
    max_uses     INTEGER,                      -- NULL = 不限次数（全班共用一个码）；1 = 一码一号
    use_count    INTEGER NOT NULL DEFAULT 0,   -- 已经成功注册掉几个人
    revoked_at   TIMESTAMP,                    -- 软吊销：留着行是为了「谁用过哪个码」还能查
    created_at   TIMESTAMP DEFAULT (datetime('now','localtime')),
    last_used_at TIMESTAMP
);
""",
    """
CREATE TABLE IF NOT EXISTS invite_uses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id    INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
);
""",
    "CREATE INDEX IF NOT EXISTS ix_invite_uses_code ON invite_uses(code_id);",
    "CREATE INDEX IF NOT EXISTS ix_invite_uses_user ON invite_uses(user_id);",
)


class RegistrationError(Exception):
    """注册被闸门挡下，消息可以直接展示给学生。"""


def mode():
    """当前开关。配置文件里写错了值不当哑巴处理：退回最安全的 invite。"""
    raw = (config.REGISTER_MODE or "").strip().lower()
    return raw if raw in MODES else "invite"


def normalize(code):
    """码的比较口径：去空白 + 转小写。生成、入库、校验三处都过这里。"""
    return (code or "").strip().lower()


def new_code(length=None):
    length = int(length or config.REGISTER_CODE_LENGTH)
    length = max(6, min(32, length))
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def ensure_schema(conn):
    """把两张表建出来（幂等）。老库靠它补表，新库靠 init_db 的 SCHEMA。"""
    for ddl in TABLE_DDL:
        conn.execute(ddl)
    conn.commit()


CLOSED_HINT = "注册已经关闭，请联系任课老师开通账号"
NOT_CONFIGURED_HINT = "老师还没有发放邀请码，注册暂未开放"


def state():
    """注册页要显示的状态。模板只读这里，不自己解析 config、也不自己查库。

    `form_open` 把「闸门关着」和「闸门开着但一个码都没发」合并成同一个结论：
    这两种情况下表单都毫无意义（提交上去必然被拒），所以干脆不收。
    """
    current = mode()
    codes = count_active() if current == "invite" else None
    if current == "closed":
        hint = CLOSED_HINT
    elif current == "invite" and not codes:
        hint = NOT_CONFIGURED_HINT
    else:
        hint = None
    return {
        "mode": current,
        "form_open": hint is None,
        "invite_required": current == "invite",
        "hint": hint,
        "codes_configured": codes,
    }


# 脚本侧（没有 Flask 请求上下文）复用同一个连接：manage_invite.py 一条命令里要
# 建表、插入、回读好几趟，每趟开一个新连接既没必要，还容易让人误以为看到了旧数据。
# 走请求上下文的分支不受影响 —— 那个连接由 db.get_db() 按请求管理、请求结束关闭。
_script_conn = None


def _get_conn():
    """优先用请求上下文里那个连接，脚本里没有上下文就自己开一个（并缓存）。"""
    from db import connect, get_db               # 延迟导入：脚本侧不需要 Flask 上下文
    global _script_conn
    try:
        return get_db()
    except RuntimeError:                          # 不在 app context 里 = 命令行在跑
        if _script_conn is None:
            _script_conn = connect()
            _script_conn.row_factory = sqlite3.Row
        return _script_conn


def count_active():
    """还有几个能用的码。0 就意味着注册页会显示「尚未开放」。"""
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM invite_codes WHERE revoked_at IS NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def check(code):
    """按当前开关校验这个码，可用则返回一个待占用的凭据 {code_id, max_uses}。

    返回 None 表示「这一趟不需要码」（open）；真正的占用在 record() 里做，
    因为要等用户名查重和哈希都过了才该消耗掉一次性码。
    """
    current = mode()
    if current == "open":
        return None
    if current == "closed":
        raise RegistrationError("注册已经关闭，请联系任课老师")

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, code, max_uses, use_count, revoked_at FROM invite_codes "
            "WHERE code = ?",
            (normalize(code),),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # 「no such table: invite_codes」= 部署时表还没补上就重启了服务。
        # 这里必须拒绝而不是放行：放行就等于闸门形同虚设，而这正是最容易
        # 在半夜出问题时悄悄发生的那一类故障。
        if "no such table" in str(exc).lower():
            raise RegistrationError(
                "注册功能暂时不可用（服务器还没配置邀请码），请稍后再试或联系任课老师")
        raise
    if not row or row["revoked_at"]:
        # 比较就是上面那句 `WHERE code = ?`（SQLite 的 TEXT 默认 BINARY 排序，
        # 区分大小写、逐字节相等），不是 startswith / LIKE，猜不出前缀。
        raise RegistrationError("邀请码不正确，请向任课老师索取")
    if row["max_uses"] is not None and row["use_count"] >= row["max_uses"]:
        raise RegistrationError("这个邀请码已经被用完了")
    return {"code_id": row["id"], "max_uses": row["max_uses"]}


def record(claim, user_id):
    """占用这个码，并记下「user_id 用它注册了」——必须和插入用户同处一个事务。

    扣次数用的是带条件的 UPDATE（`use_count < max_uses`）而不是先读后写：
    一次性码被两个人同一秒提交时，先提交的那个把行改掉，后一个 rowcount 为 0，
    于是只有一个人注册得上。换成「SELECT 判断 + UPDATE 加一」两个人都会觉得自己赢了，
    其中一个会在插入用户后撞上外键/唯一约束，变成一个 500。
    """
    if claim is None:
        return
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE invite_codes SET use_count = use_count + 1, "
        "last_used_at = datetime('now','localtime') "
        "WHERE id = ? AND revoked_at IS NULL "
        "AND (max_uses IS NULL OR use_count < max_uses)",
        (claim["code_id"],),
    )
    if cur.rowcount != 1:
        raise RegistrationError("这个邀请码刚刚被用完了，请重新索取")
    conn.execute("INSERT INTO invite_uses(code_id, user_id) VALUES(?, ?)",
                 (claim["code_id"], user_id))


def add(code=None, note=None, max_uses=None, length=None):
    """加一个码。不给 code 就生成一个随机码（推荐）。已存在则原样返回。"""
    code = normalize(code) or new_code(length)
    if not CODE_RE.match(code):
        raise ValueError("邀请码只能是 3-64 位小写字母、数字或 . _ -，"
                         "现有内容：%r（大写在入库前会被转成小写）" % code)
    if max_uses is not None:
        max_uses = int(max_uses)
        if max_uses < 1:
            raise ValueError("次数上限至少为 1；不限次数就不要传 --max-uses")
    conn = _get_conn()
    ensure_schema(conn)
    try:
        conn.execute("INSERT INTO invite_codes(code, note, max_uses) VALUES(?,?,?)",
                     (code, note, max_uses))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()          # 撞了 invite_codes.code 的 UNIQUE：已经有人加过同一个码
    return get(code)


def get(code):
    conn = _get_conn()
    ensure_schema(conn)
    return conn.execute("SELECT * FROM invite_codes WHERE code = ?",
                        (normalize(code),)).fetchone()


def list_codes(include_revoked=False):
    conn = _get_conn()
    ensure_schema(conn)
    sql = "SELECT * FROM invite_codes"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    return conn.execute(sql + " ORDER BY id").fetchall()


def revoke(code):
    """软吊销：留行删资格，「谁用过这个码」还得能查。"""
    conn = _get_conn()
    ensure_schema(conn)
    cur = conn.execute(
        "UPDATE invite_codes SET revoked_at = datetime('now','localtime') "
        "WHERE code = ? AND revoked_at IS NULL",
        (normalize(code),),
    )
    conn.commit()
    return cur.rowcount


def purge(code):
    """硬删除（连带它的 usage 记录）。吊销过的码确认不用追溯了再用。"""
    conn = _get_conn()
    ensure_schema(conn)
    row = get(code)
    if not row:
        return 0
    conn.execute("DELETE FROM invite_uses WHERE code_id = ?", (row["id"],))
    conn.execute("DELETE FROM invite_codes WHERE id = ?", (row["id"],))
    conn.commit()
    return 1


def users_of(code):
    conn = _get_conn()
    ensure_schema(conn)
    row = get(code)
    if not row:
        return []
    return conn.execute(
        "SELECT u.id, u.username, u.nickname, i.created_at "
        "FROM invite_uses i JOIN users u ON u.id = i.user_id "
        "WHERE i.code_id = ? ORDER BY i.id",
        (row["id"],),
    ).fetchall()
