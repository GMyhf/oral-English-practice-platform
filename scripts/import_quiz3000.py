# scripts/import_quiz3000.py
"""把 Quiz3000 课堂背诵词的男女声朗读复制进 static/audio/word3000，文件名改成平台约定。

用法（在项目根目录）：
    python scripts/import_quiz3000.py --dry-run         # 只看要复制多少文件
    python scripts/import_quiz3000.py                   # 复制
    python scripts/import_quiz3000.py --source "D:\\In-class RecitingQuiz3000-Std"
    python scripts/init_db.py                           # 登记进 words.audio_f / audio_m

源文件名的样子：<Word>.<f|m>.mp3，词性挂在词后面时还有 abuse n..f.mp3 这种。
复制时一律按 scripts/quiz3000_words.json 走：词表里没有的词形一个文件都不拷，
这样「不是单词的音频」（提示音 400hz-200ms、RecitingBoy-QuizPractice3000 之类）
根本进不了仓库。目标文件名用小写词形，因为 Windows 文件系统不分大小写，
China / china 这种同名异义词只能共用一个文件；同一个词的名词、动词两个源文件
（abuse n. / abuse v.）也并成一个，取按文件名排序后的第一个，脚本会把这些列出来。
脚本幂等：目标已存在且大小相同就跳过，可以反复重跑。
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_quiz3000_words import scan_package  # noqa: E402  同一套词形折叠规则，别写两份
from init_db import BASE_DIR, QUIZ3000_WORDS, WORD3000_AUDIO_DIR  # noqa: E402

DEFAULT_SOURCE = r"C:\Users\10420\Desktop\English\In-class RecitingQuiz3000-Std\In-class RecitingQuiz3000-Std"


def load_allowed():
    """词表里允许出现的词形（小写）。"""
    if not os.path.isfile(QUIZ3000_WORDS):
        return None
    with open(QUIZ3000_WORDS, encoding="utf-8") as fh:
        return {entry["word"].lower() for entry in json.load(fh)["words"]}


def main():
    parser = argparse.ArgumentParser(description="导入 Quiz3000 单词朗读音频")
    parser.add_argument("--source", default=os.environ.get("QUIZ3000_DIR", DEFAULT_SOURCE))
    parser.add_argument("--dry-run", action="store_true", help="只统计要复制多少文件，不写盘")
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        print("找不到录音包目录：", args.source)
        return 1
    allowed = load_allowed()
    if allowed is None:
        print("词表不存在，先跑 python scripts/build_quiz3000_words.py --dict <ecdict.csv>")
        return 1

    groups, unparsed, parsed = scan_package(args.source)
    kept = {key: group for key, group in groups.items() if key in allowed}
    dropped = sorted(set(groups) - set(kept))

    size_box = {"bytes": 0, "copied": 0, "skipped": 0}
    merged = []
    single_voice = []
    for key in sorted(kept):
        group = kept[key]
        for voice in sorted(group["files"]):
            sources = sorted(group["files"][voice])
            if len(sources) > 1:
                merged.append((key, voice, [stem for stem, _ in sources]))
            src_name = sources[0][1]
            ext = os.path.splitext(src_name)[1].lower()
            dst = os.path.join(WORD3000_AUDIO_DIR, key + "." + voice + ext)
            src = os.path.join(args.source, src_name)
            size = os.path.getsize(src)
            size_box["bytes"] += size
            if os.path.isfile(dst) and os.path.getsize(dst) == size:
                size_box["skipped"] += 1
                continue
            size_box["copied"] += 1
            if args.dry_run:
                continue
            os.makedirs(WORD3000_AUDIO_DIR, exist_ok=True)
            shutil.copy2(src, dst)
        if set(group["files"]) != {"f", "m"}:
            single_voice.append((key, sorted(group["files"])))

    print("录音包解析 %d 个文件，其中 %d 个词形在词表里，复制 %d 个音频"
          % (parsed, len(kept), size_box["copied"] + size_box["skipped"]))
    if dropped:
        print("  词表里没有、因此一个字节都没拷的词形 %d 个：%s"
              % (len(dropped), " ".join(dropped[:40])))
    if unparsed:
        print("  文件名不是「词.性别.mp3」的 %d 个文件已忽略：%s"
              % (len(unparsed), " ".join(unparsed[:20])))
    if merged:
        print("  %d 个词形把同形异音词的多个源文件并成一个（取排序后的第一个）：" % len(merged))
        for key, voice, stems in merged[:12]:
            print("    %s.%s ← %s" % (key, voice, " / ".join(stems)))
    if single_voice:
        print("  只有一种性别的词形 %d 个（出题时只用得了这一个声音）：" % len(single_voice))
        for key, voices in single_voice[:12]:
            print("    %s → %s" % (key, "/".join(v.upper() for v in voices)))

    print("{tag}合计 {n} 个文件、约 {mb:.1f} MB（新复制 {c}，已存在跳过 {s}）".format(
        tag="计划" if args.dry_run else "完成",
        n=size_box["copied"] + size_box["skipped"],
        mb=size_box["bytes"] / 1024 / 1024,
        c=size_box["copied"], s=size_box["skipped"]))
    if args.dry_run:
        print("这是 --dry-run 预览，去掉该参数即真正复制。")
    else:
        print("接着执行 python scripts/init_db.py 登记到 words.audio_f / audio_m。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
