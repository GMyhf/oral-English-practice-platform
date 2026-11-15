# scripts/init_db.py
"""建表 + 灌入种子数据。

用法（在项目根目录）：
    python scripts/init_db.py            # 幂等，重复执行不会重复插入
    python scripts/init_db.py --reset    # 先删掉已有表再重建
"""
import os
import re
import sqlite3
import sys

# Windows 控制台默认 GBK，打印音标（/iː/ 之类）会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")
VOWEL_AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio", "vowel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    nickname      TEXT,
    default_count INTEGER NOT NULL DEFAULT 20,   -- 默认练习题量
    time_limit    REAL    NOT NULL DEFAULT 1,    -- 近似音每题限时秒数
    voice         TEXT    NOT NULL DEFAULT 'f',  -- f=女声 m=男声
    created_at    TIMESTAMP DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS words (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word       TEXT NOT NULL UNIQUE,
    phonetic   TEXT,
    meaning_cn TEXT,
    example    TEXT,
    audio_path TEXT,               -- 有真人音频时填相对路径，否则前端用合成发音
    category   TEXT
);

CREATE TABLE IF NOT EXISTS vowel_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key   TEXT NOT NULL UNIQUE,   -- 目录名，如 sheep-ship
    label       TEXT NOT NULL,          -- 如 /iː/ 与 /ɪ/
    description TEXT,
    pair_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vowel_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES vowel_groups(id) ON DELETE CASCADE,
    item_no    INTEGER NOT NULL,        -- 第几组最小对立对，如 3
    option_no  INTEGER NOT NULL,        -- 1 或 2，对应屏幕上的两个编号
    word       TEXT    NOT NULL,
    audio_f    TEXT    NOT NULL,
    audio_m    TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vowel_item ON vowel_items(group_id, item_no, option_no);

CREATE TABLE IF NOT EXISTS practice_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    practice_type TEXT    NOT NULL,     -- vowel / read-translate / listen-spell
    params_json   TEXT,
    total         INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    wrong_count   INTEGER NOT NULL DEFAULT 0,
    finished      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS practice_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES practice_sessions(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    question_json  TEXT    NOT NULL,    -- 该题的完整信息，供结果页逐题回看
    user_answer    TEXT,
    is_correct     INTEGER NOT NULL DEFAULT 0,
    recording_path TEXT,                -- 用户上传的朗读音频相对路径
    created_at     TIMESTAMP DEFAULT (datetime('now','localtime'))
);
"""

DROP = """
DROP TABLE IF EXISTS practice_records;
DROP TABLE IF EXISTS practice_sessions;
DROP TABLE IF EXISTS vowel_items;
DROP TABLE IF EXISTS vowel_groups;
DROP TABLE IF EXISTS words;
DROP TABLE IF EXISTS users;
"""

# 近似音组目录名 → 音标对照说明
VOWEL_GROUP_LABELS = {
    "see-say":     ("/iː/ 与 /eɪ/", "长元音 /iː/ 与双元音 /eɪ/ 的对立，如 see / say"),
    "sheep-ship":  ("/iː/ 与 /ɪ/",  "长元音 /iː/ 与短元音 /ɪ/ 的对立，如 sheep / ship"),
    "big-beg":     ("/ɪ/ 与 /e/",   "短元音 /ɪ/ 与 /e/ 的对立，如 big / beg"),
    "pull-pool":   ("/ʊ/ 与 /uː/",  "短元音 /ʊ/ 与长元音 /uː/ 的对立，如 pull / pool"),
}

# 音频文件名：n.m-word.f.wav / n.m-word.m.wav
AUDIO_NAME_RE = re.compile(r"^(\d+)\.(\d+)-(.+)\.([fm])\.wav$")

SEED_WORDS = [
    ("abandon", "/əˈbændən/", "放弃；抛弃", "He abandoned his car in the snow."),
    ("ability", "/əˈbɪləti/", "能力；才能", "She has the ability to solve hard problems."),
    ("absent", "/ˈæbsənt/", "缺席的；缺少的", "He was absent from school yesterday."),
    ("achieve", "/əˈtʃiːv/", "实现；达到", "You can achieve your goal with effort."),
    ("active", "/ˈæktɪv/", "积极的；活跃的", "She is very active in class."),
    ("admire", "/ədˈmaɪər/", "钦佩；赞赏", "I admire your courage."),
    ("advance", "/ədˈvæns/", "前进；进步", "Please advance to the next room."),
    ("analysis", "/əˈnæləsɪs/", "分析", "The analysis took three weeks."),
    ("ancient", "/ˈeɪnʃənt/", "古代的；古老的", "We visited an ancient temple."),
    ("anxious", "/ˈæŋkʃəs/", "焦虑的；渴望的", "She felt anxious about the exam."),
    ("apparent", "/əˈpærənt/", "明显的；表面上的", "It was apparent that he was lying."),
    ("appreciate", "/əˈpriːʃieɪt/", "感激；欣赏", "I really appreciate your help."),
    ("approach", "/əˈproʊtʃ/", "方法；接近", "Try a different approach."),
    ("appropriate", "/əˈproʊpriət/", "适当的；恰当的", "Wear appropriate clothes."),
    ("arrange", "/əˈreɪndʒ/", "安排；整理", "Let me arrange a meeting."),
    ("attitude", "/ˈætɪtjuːd/", "态度；看法", "She has a positive attitude."),
    ("available", "/əˈveɪləbl/", "可用的；有空的", "Is this seat available?"),
    ("balance", "/ˈbæləns/", "平衡；余额", "Keep a balance between work and rest."),
    ("barrier", "/ˈbæriər/", "障碍；屏障", "Language is a barrier to communication."),
    ("behavior", "/bɪˈheɪvjər/", "行为；举止", "His behavior surprised everyone."),
    ("benefit", "/ˈbenɪfɪt/", "利益；好处", "Exercise has many benefits."),
    ("capacity", "/kəˈpæsəti/", "容量；能力", "The stadium has a capacity of 60,000."),
    ("challenge", "/ˈtʃælɪndʒ/", "挑战", "This job is a real challenge."),
    ("circumstance", "/ˈsɜːrkəmstæns/", "环境；情况", "Under no circumstances give up."),
    ("commit", "/kəˈmɪt/", "承诺；投入", "She committed to finishing the project."),
    ("community", "/kəˈmjuːnəti/", "社区；团体", "Our community is friendly."),
    ("compete", "/kəmˈpiːt/", "竞争；比赛", "Ten students competed for the prize."),
    ("complex", "/kəmˈpleks/", "复杂的", "This is a complex issue."),
    ("consider", "/kənˈsɪdər/", "考虑；思考", "Please consider my suggestion."),
    ("constant", "/ˈkɑːnstənt/", "持续的；恒定的", "The machine needs constant attention."),
]


def scan_vowel_audio(conn):
    """扫描 static/audio/vowel，把最小对立对音频写进 vowel_groups / vowel_items。"""
    if not os.path.isdir(VOWEL_AUDIO_DIR):
        print("未找到近似音音频目录，跳过：", VOWEL_AUDIO_DIR)
        return 0

    group_count = 0
    for group_key in sorted(os.listdir(VOWEL_AUDIO_DIR)):
        subdir = os.path.join(VOWEL_AUDIO_DIR, group_key)
        if not os.path.isdir(subdir):
            continue

        # 先按 (item_no, option_no) 收集，把同一题的男女声合并成一行
        items = {}
        for fname in sorted(os.listdir(subdir)):
            match = AUDIO_NAME_RE.match(fname)
            if not match:
                print("  跳过无法解析的文件名：", group_key, "/", fname)
                continue
            item_no, option_no, word, voice = match.groups()
            rel = f"static/audio/vowel/{group_key}/{fname}"
            key = (int(item_no), int(option_no))
            row = items.setdefault(key, {"word": word, "f": None, "m": None})
            row[voice] = rel

        label, desc = VOWEL_GROUP_LABELS.get(group_key, (group_key, ""))
        conn.execute(
            "INSERT INTO vowel_groups(group_key, label, description) VALUES(?,?,?) "
            "ON CONFLICT(group_key) DO UPDATE SET label=excluded.label, description=excluded.description",
            (group_key, label, desc),
        )
        group_id = conn.execute(
            "SELECT id FROM vowel_groups WHERE group_key=?", (group_key,)
        ).fetchone()["id"]

        inserted = 0
        for (item_no, option_no), row in sorted(items.items()):
            if not (row["f"] and row["m"]):
                print(f"  缺少男声或女声，跳过 {group_key} 第{item_no}组选项{option_no}")
                continue
            conn.execute(
                "INSERT INTO vowel_items(group_id, item_no, option_no, word, audio_f, audio_m) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(group_id, item_no, option_no) DO UPDATE SET "
                "word=excluded.word, audio_f=excluded.audio_f, audio_m=excluded.audio_m",
                (group_id, item_no, option_no, row["word"], row["f"], row["m"]),
            )
            inserted += 1

        pair_count = len({no for (no, _) in items})
        conn.execute("UPDATE vowel_groups SET pair_count=? WHERE id=?", (pair_count, group_id))
        print(f"  {group_key:<12} {label:<12} 对立对 {pair_count} 组，音频 {inserted} 条")
        group_count += 1

    return group_count


def seed_words(conn):
    for word, phonetic, cn, example in SEED_WORDS:
        conn.execute(
            "INSERT INTO words(word, phonetic, meaning_cn, example, category) "
            "VALUES(?,?,?,?,'vocabulary') ON CONFLICT(word) DO UPDATE SET "
            "phonetic=excluded.phonetic, meaning_cn=excluded.meaning_cn, example=excluded.example",
            (word, phonetic, cn, example),
        )
    return len(SEED_WORDS)


def init_db(reset=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if reset:
        print("--reset：删除旧表")
        conn.executescript(DROP)

    conn.executescript(SCHEMA)

    words = seed_words(conn)
    groups = scan_vowel_audio(conn)
    conn.commit()

    print(
        "数据库初始化完成 → {path}\n  单词 {w} 条，近似音组 {g} 组".format(
            path=os.path.relpath(DB_PATH, BASE_DIR), w=words, g=groups
        )
    )
    conn.close()


if __name__ == "__main__":
    init_db(reset="--reset" in sys.argv)
