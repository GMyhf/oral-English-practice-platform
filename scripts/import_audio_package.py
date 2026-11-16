# scripts/import_audio_package.py
"""把教材配套录音（E-Prime 版）里的 3B / 3A 音频复制进本项目，命名沿用平台约定。

用法（在项目根目录）：
    python scripts/import_audio_package.py --dry-run          # 只体检，不写文件
    python scripts/import_audio_package.py                    # 复制全部
    python scripts/import_audio_package.py --only vowel       # 只补近似音组
    python scripts/import_audio_package.py --source "D:\\录音包"  # 换源目录

复制完再执行 python scripts/init_db.py，由它把音频登记到 vowel_groups /
vowel_items / words.audio_path。脚本是幂等的：目标文件已存在且大小相同就跳过。
"""
import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from init_db import (  # noqa: E402  复用同一套文件名规则，避免两处各写一份
    AUDIO_NAME_RE,
    BASE_DIR,
    VOWEL_AUDIO_DIR,
    VOWEL_GROUP_LABELS,
    WORD_AUDIO_DIR,
)

DEFAULT_SOURCE = r"C:\Users\10420\Desktop\English\教材配套录音E-prime版\教材配套录音E-prime版"

# 课号 → [(3B 子目录, 组名)]。教材只有第 10 课把近似音拆成 a / c 两个小组。
GROUP_SOURCES = {
    1: [("", "see-say")],
    2: [("", "sheep-ship")],
    3: [("", "big-beg")],
    4: [("", "pan-pen")],
    8: [("", "cub-curb")],
    9: [("", "fond-fund")],
    10: [("a", "boss-bus"), ("c", "burn-barn")],
    11: [("", "full-fall")],
    12: [("", "pull-pool")],
    13: [("", "pain-pen")],
    14: [("", "foal-fall")],
    15: [("", "kite-cat")],
    16: [("", "boil-bawl")],
    17: [("", "pound-pond")],
    19: [("", "woos-ooze")],
    20: [("", "row-low")],
    21: [("", "world-word")],
    22: [("", "no-low")],
    23: [("", "comb-cone")],
    24: [("", "sing-sin")],
    25: [("", "heat-eat")],
    27: [("", "feel-veal")],
    29: [("", "seal-zeal")],
    30: [("", "fin-thin")],
    31: [("", "teethe-tease")],
    32: [("", "shave-save")],
    35: [("", "pin-bin")],
    37: [("", "tin-din")],
    39: [("", "cap-gap")],
    41: [("", "cheer-jeer")],
}

# 第 10 课 c 组每题给了三个选项（burn / barn / born），而练习界面只有 1、2 两个编号，
# 这里先导前两项；/ɔːr/ 的第三项（born、hoard、store、four、cord）等界面支持三选项再补。
ONLY_OPTIONS = {("10", "c"): {1, 2}}

# 单个字母是字母名不是单词；theam / tode 是录音包里的拼写错误，都不进词库
WORD_MIN_LEN = 2
WORD_EXCLUDE = {"theam", "tode"}
WORD_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")


def lesson_dirs(source):
    """{课号: 课目录}，课目录名形如「第07课-newE-prime-Std」。"""
    dirs = {}
    for name in sorted(os.listdir(source)):
        path = os.path.join(source, name)
        if not os.path.isdir(path):
            continue
        match = re.search(r"(\d+)", name)
        if not match:
            continue
        dirs[int(match.group(1))] = path
    return dirs


def copy_file(src, dst, size_box, dry_run):
    size = os.path.getsize(src)
    size_box["bytes"] += size
    if os.path.isfile(dst) and os.path.getsize(dst) == size:
        size_box["skipped"] += 1
        return
    size_box["copied"] += 1
    if dry_run:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def import_vowel(lessons, size_box, dry_run):
    """3B → static/audio/vowel/<组名>/<item>.<option>-<word>.<f|m>.<ext>"""
    groups = 0
    for lesson_no, entries in sorted(GROUP_SOURCES.items()):
        lesson = lessons.get(lesson_no)
        if not lesson:
            print(f"  源目录里找不到第 {lesson_no} 课，跳过")
            continue
        base = os.path.join(lesson, "3B")
        if not os.path.isdir(base):
            print(f"  第 {lesson_no} 课没有 3B 目录，跳过")
            continue
        for subdir, group_key in entries:
            src_dir = os.path.join(base, subdir) if subdir else base
            if not os.path.isdir(src_dir):
                print(f"  第 {lesson_no} 课缺少 3B/{subdir}，跳过 {group_key}")
                continue
            if group_key not in VOWEL_GROUP_LABELS:
                print(f"  组 {group_key} 没有音标对照说明，请在 VOWEL_GROUP_LABELS 里补")
                continue
            dst_dir = os.path.join(VOWEL_AUDIO_DIR, group_key)
            kept = 0
            for fname in sorted(os.listdir(src_dir)):
                match = AUDIO_NAME_RE.match(fname)
                if not match:
                    continue
                allowed = ONLY_OPTIONS.get((str(lesson_no), subdir))
                if allowed and int(match.group(2)) not in allowed:
                    continue
                copy_file(os.path.join(src_dir, fname), os.path.join(dst_dir, fname), size_box, dry_run)
                kept += 1
            print(f"  第 {lesson_no:>2} 课 3B{('/' + subdir) if subdir else '':<4} → {group_key:<13} {kept} 个音频")
            groups += 1
    return groups


def import_words(lessons, size_box, dry_run):
    """3A → static/audio/word/<word>.<ext>，只取女声（words.audio_path 只有一个字段）。"""
    taken = {}
    stats = {"words": 0, "reused": 0, "rejected": []}
    for lesson_no in sorted(lessons):
        src_dir = os.path.join(lessons[lesson_no], "3A")
        if not os.path.isdir(src_dir):
            continue
        for fname in sorted(os.listdir(src_dir)):
            match = AUDIO_NAME_RE.match(fname)
            if not match:
                continue
            word, voice = match.group(3), match.group(4).lower()
            if voice != "f":
                continue
            if len(word) < WORD_MIN_LEN or word in WORD_EXCLUDE or not WORD_NAME_OK.match(word):
                stats["rejected"].append((lesson_no, word))
                continue
            if word.lower() in taken:
                # 同一个词在多课出现（如 fall、ball），音频重复，保留第一次的那份
                stats["reused"] += 1
                continue
            dst = os.path.join(WORD_AUDIO_DIR, word + os.path.splitext(fname)[1].lower())
            copy_file(os.path.join(src_dir, fname), dst, size_box, dry_run)
            taken[word.lower()] = lesson_no
            stats["words"] += 1
    print(f"  单词音频 {stats['words']} 个（重复出现跳过 {stats['reused']} 个）")
    if stats["rejected"]:
        uniq = sorted({word for _, word in stats["rejected"]})
        print(f"  剔除非单词 {len(stats['rejected'])} 个文件：{' '.join(uniq)}")
    return len(taken)


def main():
    parser = argparse.ArgumentParser(description="导入教材配套录音的 3B / 3A 音频")
    parser.add_argument("--source", default=os.environ.get("AUDIO_PACKAGE_DIR", DEFAULT_SOURCE))
    parser.add_argument("--only", choices=("all", "vowel", "word"), default="all")
    parser.add_argument("--dry-run", action="store_true", help="只统计要复制多少文件，不写盘")
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        print("找不到录音包目录：", args.source)
        return 1

    lessons = lesson_dirs(args.source)
    print(f"录音包 {len(lessons)} 课 → 项目 {BASE_DIR}")

    size_box = {"bytes": 0, "copied": 0, "skipped": 0}
    if args.only in ("all", "vowel"):
        print("[3B 近似音组]")
        import_vowel(lessons, size_box, args.dry_run)
    if args.only in ("all", "word"):
        print("[3A 单词音频]")
        import_words(lessons, size_box, args.dry_run)

    print(
        "{tag}合计 {n} 个文件、约 {mb:.1f} MB{note}".format(
            tag="计划" if args.dry_run else "完成",
            n=size_box["copied"] + size_box["skipped"],
            mb=size_box["bytes"] / 1024 / 1024,
            note="（新复制 %d，跳过已存在 %d）" % (size_box["copied"], size_box["skipped"]),
        )
    )
    if args.dry_run:
        print("这是 --dry-run 预览，去掉该参数即真正复制。")
    else:
        print("接着执行 python scripts/init_db.py 登记到数据库。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
