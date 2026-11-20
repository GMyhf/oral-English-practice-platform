import sqlite3
import os

import config
from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_db_path():
    """把 config.DATABASE_PATH 解析成绝对路径，相对路径按项目根算。

    以前这里写死成 BASE_DIR/data/app.db，config 里那条 DATABASE_PATH 根本没人读，
    改环境变量只会让人以为换了库。数据库以后要挪去 /var/lib（部署机的常规做法）
    就真的只需要改环境变量。
    """
    path = config.DATABASE_PATH or "data/app.db"
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(BASE_DIR, path))


def resolve_recording_dir():
    """把 config.RECORDING_DIR 解析成绝对路径，规则同 resolve_db_path。

    放在这里而不是 app.py：init_db.py --reset 也要知道录音在哪儿，
    而脚本不该为了一个路径去 import 整个 Flask 应用。
    """
    raw = (config.RECORDING_DIR or "data/recordings").replace("\\", "/")
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(BASE_DIR, raw))


# 模块级常量留着：脚本和测试都从这里取路径，也是「只有一个真来源」的意思
DB_PATH = resolve_db_path()

# 每个连接都要执行的 pragma。
# 为什么不「在建库时设一次就够了」：journal_mode 确实写进库文件头，是永久的；
# 但 busy_timeout 和 foreign_keys 是【连接级】设置，新连接不设就等于没设。
connection_pragmas = (
    # WAL：读不再阻塞写，写也不再阻塞读。整班同时交卷时不至于互相排队，
    # 而且提交省掉一次 fsync —— 机械盘/云盘上差别明显。
    # 代价是 data/ 目录里会多出 app.db-wal / app.db-shm 两个文件，
    # 备份时要么一起拷，要么先 `PRAGMA wal_checkpoint(TRUNCATE)`。
    # 前提：库文件不能在 NFS 等网络文件系统上，这台部署机是本地 ext4，可以用。
    "PRAGMA journal_mode = WAL",
    # WAL 下 NORMAL 已经是安全的：掉电最坏丢最后几条事务，不会把库写坏。
    "PRAGMA synchronous = NORMAL",
    # 撞锁时先等 5 秒再报错。sqlite3.connect 的 timeout 参数本来就是这个东西，
    # 写出来是为了让它一眼可见，也顺便压过任何奇怪默认值。
    "PRAGMA busy_timeout = 5000",
    # schema 里一直写着 REFERENCES / ON DELETE CASCADE，但 SQLite 默认不执行外键，
    # 所以那些约束此前从来没真正生效过。打开之后 practice_records 不可能再挂到
    # 一个不存在的 session 上。
    "PRAGMA foreign_keys = ON",
)


def connect(path=None):
    """建一个连接，带全套 pragma。给 get_db / 脚本 / 测试复用。"""
    target = os.path.normpath(path or DB_PATH)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    conn = sqlite3.connect(target)
    for pragma in connection_pragmas:
        conn.execute(pragma)
    return conn


def get_db():
    """同一请求内复用同一个连接，没有就新建。"""
    if "db" not in g:
        g.db = connect()
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """请求结束时关闭连接。"""
    db = g.pop("db", None)
    if db is not None:
        db.close()
