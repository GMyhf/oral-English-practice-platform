# services/practice.py
"""出题逻辑：近似音最小对立对抽题、词库随机抽题。"""
import random

import config
from db import get_db

PRACTICE_TYPES = ("vowel", "read-translate", "listen-spell")

# 近似音的句子练习需要句子音频，目前 static/audio 里只有单词，所以先不开放
VOWEL_MODES = {"word": True, "sentence": False}


def list_vowel_groups():
    rows = get_db().execute(
        "SELECT id, group_key, label, description, pair_count "
        "FROM vowel_groups ORDER BY group_key"
    ).fetchall()
    return [dict(r) for r in rows]


def build_vowel_questions(group_key, count, voice="f"):
    """按设计文档：每题从题库随机抽一组最小对立对，屏幕上标 1/2，随机播放其中一个。

    返回题目列表，每题含 options / audio / answer（answer 是展示编号 1 或 2）。
    """
    if voice not in ("f", "m"):
        voice = "f"
    audio_column = "audio_f" if voice == "f" else "audio_m"

    rows = get_db().execute(
        f"SELECT i.item_no, i.option_no, i.word, i.{audio_column} AS audio "
        "FROM vowel_items i JOIN vowel_groups g ON g.id = i.group_id "
        "WHERE g.group_key = ? ORDER BY i.item_no, i.option_no",
        (group_key,),
    ).fetchall()
    if not rows:
        # 区分"组不存在"和"组里没音频"：前者是参数错了，后者是库没初始化好
        known = get_db().execute(
            "SELECT 1 FROM vowel_groups WHERE group_key = ?", (group_key,)
        ).fetchone()
        raise ValueError(
            "该近似音组还没有音频题目，请先执行 scripts/init_db.py"
            if known else "近似音组不存在，请重新选择一组近似音"
        )

    by_item = {}
    for row in rows:
        by_item.setdefault(row["item_no"], []).append(
            {"word": row["word"], "audio": row["audio"]}
        )
    # 只有凑成一对（两个选项）的题目才能出
    pairs = [opts for opts in by_item.values() if len(opts) >= 2]
    if not pairs:
        raise ValueError("该近似音组没有完整的对立对题目")

    # 上限只由设置项决定：以前这里是 len(pairs)*4，用户选 100 题会被静默砍成 20 题
    count = max(1, min(int(count), config.MAX_QUESTION_COUNT))
    if count <= len(pairs):
        chosen = random.sample(pairs, count)
    else:
        # 题量超过题库对数时允许重复抽，保证用户指定的题量
        chosen = [random.choice(pairs) for _ in range(count)]

    questions = []
    for seq, opts in enumerate(chosen, start=1):
        shuffled = list(opts[:2])
        random.shuffle(shuffled)  # 避免正确项总落在同一个编号上
        options = [{"index": i + 1, **opt} for i, opt in enumerate(shuffled)]
        played = random.randrange(len(options))
        questions.append({
            "seq": seq,
            "group_key": group_key,
            "options": options,
            "audio": options[played]["audio"],
            "answer": options[played]["index"],
        })
    return questions


def random_words(count):
    """看读译 / 听读写：从词库随机抽 count 个单词。

    返回 (抽到的单词, 词库总数)：词库比请求的题量小时，调用方要告诉用户实际练了几题。
    """
    count = max(1, min(int(count), config.MAX_QUESTION_COUNT))
    db = get_db()
    bank_size = db.execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"]
    rows = db.execute(
        "SELECT id, word, phonetic, meaning_cn, example, audio_path "
        "FROM words ORDER BY RANDOM() LIMIT ?",
        (count,),
    ).fetchall()
    return [dict(r) for r in rows], bank_size


def words_by_ids(ids):
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"SELECT id, word, phonetic, meaning_cn, example, audio_path "
        f"FROM words WHERE id IN ({marks})",
        ids,
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}
