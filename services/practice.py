# services/practice.py
"""出题逻辑：近似音最小对立对抽题、词库随机抽题。"""
import random

import config
from db import get_db

PRACTICE_TYPES = ("vowel", "read-translate", "listen-spell")

# 近似音两种题型：word = 3B 的单词对立对，sentence = 4A 的句子（选项只给句中变化的两个词）
VOWEL_MODES = {"word": True, "sentence": True}

# 音频性别：random = 每题自己抽，f/m = 锁定（留给测试脚本和分性别统计）
VOWEL_VOICES = ("f", "m")
DEFAULT_VOWEL_VOICE = "random"


def list_vowel_groups():
    """近似音组列表，带上单词题与句子题各有多少组对立对，前端好按题型提示。"""
    rows = get_db().execute(
        "SELECT g.id, g.group_key, g.label, g.description, g.pair_count, "
        "(SELECT COUNT(DISTINCT s.item_no) FROM sentence_items s WHERE s.group_id = g.id) "
        "AS sentence_pair_count "
        "FROM vowel_groups g ORDER BY g.group_key"
    ).fetchall()
    return [dict(r) for r in rows]


def _load_pairs(table, group_key, with_sentence=False):
    """按对立对读出两个选项的文本与两套性别音频。

    返回 {item_no: [{word, sentence, f, m}, ...]}。某个性别的音频缺失不影响出题
    （4A 的两个发音人不是每课都分得开），只有两个性别都没有的选项才丢掉。
    """
    columns = "i.word, i.audio_f, i.audio_m" + (", i.sentence" if with_sentence else "")
    rows = get_db().execute(
        f"SELECT i.item_no, i.option_no, {columns} "
        f"FROM {table} i JOIN vowel_groups g ON g.id = i.group_id "
        "WHERE g.group_key = ? ORDER BY i.item_no, i.option_no",
        (group_key,),
    ).fetchall()
    if not rows:
        # 区分"组不存在"和"组里没音频"：前者是参数错了，后者是库没初始化好
        known = get_db().execute(
            "SELECT 1 FROM vowel_groups WHERE group_key = ?", (group_key,)
        ).fetchone()
        if not known:
            raise ValueError("近似音组不存在，请重新选择一组近似音")
        raise ValueError(
            "这一组还没有句子音频，请先跑 scripts/import_audio_package.py --only sentence"
            if table == "sentence_items"
            else "该近似音组还没有音频题目，请先执行 scripts/init_db.py"
        )

    by_item = {}
    for row in rows:
        if not (row["audio_f"] or row["audio_m"]):
            continue
        option = {"word": row["word"], "f": row["audio_f"], "m": row["audio_m"]}
        if with_sentence:
            option["sentence"] = row["sentence"]
        by_item.setdefault(row["item_no"], []).append(option)
    return by_item


def _assemble_questions(by_item, group_key, count, voice, with_sentence=False):
    """抽 count 组对立对，每题打乱编号、随机决定播哪个选项、随机决定播哪个性别。"""
    locked = voice if voice in VOWEL_VOICES else None

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
        # 选项只给编号和单词（句子题多给整句文本，作答时不显示，回看时才用得上）
        options = [{"index": i + 1, "word": opt["word"]} for i, opt in enumerate(shuffled)]
        if with_sentence:
            for option, source in zip(options, shuffled):
                option["sentence"] = source["sentence"]
        played = random.randrange(len(options))
        source = shuffled[played]
        available = [g for g in VOWEL_VOICES if source.get(g)] or ["f"]
        used_voice = locked if (locked and locked in available) else random.choice(available)
        questions.append({
            "seq": seq,
            "group_key": group_key,
            "options": options,
            "audio": source[used_voice],
            "voice": used_voice,
            "answer": options[played]["index"],
        })
    return questions


def build_vowel_questions(group_key, count, voice=DEFAULT_VOWEL_VOICE):
    """按设计文档：每题从题库随机抽一组最小对立对，屏幕上标 1/2，随机播放其中一个。

    返回题目列表，每题含 options / audio / voice / answer（answer 是展示编号 1 或 2）。

    音频性别默认每题随机抽（voice="random"）：练的是 /iː/ 和 /ɪ/ 的差别，
    不该被固定成某个人的音色，否则用户是在记发音人而不是记音。
    显式传 "f" 或 "m" 时锁定性别；该条音频缺这个性别就退回随机。
    """
    by_item = _load_pairs("vowel_items", group_key)
    return _assemble_questions(by_item, group_key, count, voice)


def build_sentence_questions(group_key, count, voice=DEFAULT_VOWEL_VOICE):
    """句子近似音：屏幕上只给句中变化的那两个词，播放的是含该词的整句。

    题目结构与单词题完全一致（options / audio / voice / answer），只是选项多了
    sentence 字段，供答完题和结果页回看整句。
    """
    by_item = _load_pairs("sentence_items", group_key, with_sentence=True)
    return _assemble_questions(by_item, group_key, count, voice, with_sentence=True)


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
