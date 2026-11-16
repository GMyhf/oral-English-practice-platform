# scripts/build_quiz3000_words.py
"""从 Quiz3000 朗读包的文件名里取出单词，用 ECDICT 词典补齐音标与中文释义，生成词表。

用法（在项目根目录）：
    python scripts/build_quiz3000_words.py --dict C:\\dict\\ecdict.csv
    python scripts/build_quiz3000_words.py --source "D:\\In-class RecitingQuiz3000-Std" \\
                                           --dict ecdict.csv --out scripts/quiz3000_words.json

为什么要这一步：录音包里每个 mp3 的文件名只有「单词.性别.mp3」，没有任何释义。
看读译要键入中文译文、听读写要核对拼写，没有释义的词出成题就是错题，所以词表
必须补齐 meaning_cn；同时「文件名长得像单词」并不等于真是单词（包里混着提示音
400hz-200ms、RecitingBoy-QuizPractice3000 这类非单词文件），词典能不能查到就是
判断依据——查得到才进词表，查不到就打印出来给人过目。

词典不进取版本库（60 多 MB 的单文件），要重新生成词表时自己下载：
    https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
下载完直接指路径即可，脚本只读不写词典。
"""
import argparse
import csv
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = r"C:\Users\10420\Desktop\English\In-class RecitingQuiz3000-Std\In-class RecitingQuiz3000-Std"
DEFAULT_OUT = os.path.join(BASE_DIR, "scripts", "quiz3000_words.json")

# <词>.<f|m>.mp3：性别标记是最后一段，前面整串都是词形
AUDIO_NAME_RE = re.compile(r"^(?P<stem>.+)\.(?P<voice>[fm])$", re.IGNORECASE)

# 文件名里词形后面挂的词性标记（教材给同形异音词分文件，如 abuse n. / abuse v.）。
# 只按整词匹配，不做前缀猜测，免得把 bow、row 这类本身是词的部分吃掉。
POS_TAGS = {
    "n", "ns", "v", "vs", "vt", "vi", "a", "ad", "adj", "adv", "pp", "p",
    "prep", "conj", "pron", "num", "int", "aux", "abbr", "art",
}

# 词典里只有大写形式、中文释义又完全跑偏的几个词形，手写纠正（其余大小写不一致的
# 条目词典释义本来就覆盖了录音包要的那个意思，直接用最长的那条即可）。
# Ms：词典里只有 MS，译文联想着微软和毫秒，而录音包读的是 /mɪz/「女士」。
# driving-license、ringroad：教材词表里的连写复合词，ECDICT 没收（词典把它们当
# driving licence / ring road），但它们是正经单词，音频也就留着。
# 后一组是 ECDICT 自己写错了的译文：download 给了「卸载, 下栽」（应为下载，且「卸载」
# 是 unload）、firewall 给了「放火墙」（应为防火墙）、short-wave 只剩一个缩写标记。
# 看读译是拿这行中文判分的，留着错释义会把答对的学生判错，所以按词典原义纠正。
MANUAL_GLOSSES = {
    "Ms": "n. 女士（用于女子姓名前，不表明婚姻状况）",
    "driving-license": "n. 驾驶执照（亦作 driving licence）",
    "ringroad": "n. 环形公路，环路（英式，亦作 ring road）",
    "download": "n. 下载，可下载的文件；vt. 下载",
    "firewall": "n. 防火墙（电脑上隔离内外网的安全程序）",
    "short-wave": "a. 短波的；n. 短波",
    "air-conditioning": "n. 空气调节，空调",
    "laptop": "n. 笔记本电脑，手提电脑",
    "online": "a. 在线的，联网的；adv. 在线地",
    "hotdog": "n. 热狗",
}

# 词形允许的字符：字母开头，后面可以是字母、数字、撇号、连字符（T-shirt、X-ray、TV）
WORD_OK = re.compile(r"^[A-Za-z][A-Za-z0-9'\-]*$")

# ECDICT 音标里的字符 → 通用 IPA（也和词库里 3A 种子词的写法对齐）
#   ә(U+04D9) 西里尔 schwa → ə      є(U+0454)/ε(U+03B5) → ɛ
#   '(半角)    重音 → ˈ              :  长音符 → ː
#   g(半角)    → ɡ(U+0261)          ^  是 g 的乱码（god /^ɔd/、geese /^i:s/）
PHONETIC_CHARS = str.maketrans({
    "ә": "ə", "є": "ɛ", "ε": "ɛ",
    "'": "ˈ", ":": "ː",
    "g": "ɡ", "^": "ɡ",
})


def normalize_stem(stem):
    """文件名里的词形 → 进词典查的拼写；返回 (词形, 说明)。失败返回 (None, 原因)。

    录音包里的命名不规范，同一个词会出现好几种写法：
        "abuse n." / "abstract a.v."  词性挂在后面 → 剥掉词性，两个读音并成一个词
        "false.a"                     词性和词之间没有空格 → 同样剥掉
        "may (2)" / "china "          带序号或尾随空格 → 剥掉
        "sow. v."                     多余的空格和点 → 清掉
        "skil(l)ful"                  括号是可省字母，不是词形 → 原样返回，交给词典判定
    """
    text = stem.strip()
    text = re.sub(r"\s*\(\d+\)\s*$", "", text).strip()      # 末尾的 (2) 是义项序号

    tokens = text.split()
    while len(tokens) > 1 and is_pos_token(tokens[-1]):
        tokens.pop()
    text = " ".join(tokens).strip()

    # 词性和词粘在一起写的：false.a.f.mp3、sow. v..mp3 这类，点号后面全是词性才剥
    chunks = text.split(".")
    if len(chunks) > 1:
        tail = [chunk for chunk in chunks[1:] if chunk]
        if all(is_pos_token(chunk) for chunk in tail):
            text = chunks[0]

    text = text.strip().rstrip(".").strip()

    if not text:
        return None, "剥掉词性后没有词形"
    if len(text) < 2:
        return None, "单个字母是字母名，不是单词"
    if not WORD_OK.match(text):
        # skil(l)ful、forward(s) 这种带括号的「可省字母」写法不是单词本身，不进词库
        return None, "词形里有非字母字符"
    return text, ""


def is_pos_token(token):
    """token 是不是纯粹由词性缩写拼成：n. / v. / a.v. / n1. / pp. 都算。"""
    parts = [re.sub(r"[^a-z&]", "", p).lower() for p in token.replace("&", ".").split(".")]
    parts = [p for p in parts if p]
    if not parts:
        return False
    return all(part in POS_TAGS for part in parts)


def load_dictionary(dict_path, wanted):
    """流式读 ECDICT，只留下 wanted（小写词形集合）里的行。

    返回 {小写词形: [(词典里的词形, phonetic, translation), …]}。ECDICT 有 77 万行，
    全量读进内存没必要；同一个词在词典里可能有大小写两条（God / god）甚至多来源重复，
    所以按小写归堆，取哪一条由 dictionary_entry 决定。
    """
    found = {}
    csv.field_size_limit(10_000_000)
    with open(dict_path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            word = (row.get("word") or "").strip()
            if not word or word.lower() not in wanted:
                continue
            found.setdefault(word.lower(), []).append(
                (word, (row.get("phonetic") or "").strip(),
                 (row.get("translation") or "").strip()))
    return found


def dictionary_entry(rows_by_lower, spelling):
    """按录音包里的词形找词典条目：大小写完全一致优先，否则取释义最长的那条。

    返回 (phonetic, translation, 词典里的词形)。ECDICT 有不少词只收了大写形式
    （CORE、FAX、God、China），录音包给的是小写，直接查会查不到；反过来也一样
    （August、March、May 在词典里只有小写 august/march/may，意思是「威严的/行军/可能」，
    这种取错义的少数词由 MANUAL_GLOSSES 手工纠正）。
    音标只在选中的这条本身空着时才从同小写的其他条目借（air 没有、Air 有 /ɛə/，
    同一个词的同一套符号，空着的地方填上即可）。
    """
    candidates = rows_by_lower.get(spelling.lower(), [])
    if not candidates:
        return "", "", None
    for word, phonetic, translation in candidates:
        if word == spelling:
            return borrow_phonetic(phonetic, spelling, candidates), translation, word
    best = max(candidates, key=lambda item: len(clean_translation(item[2])))
    return borrow_phonetic(best[1], spelling, candidates), best[2], best[0]


def borrow_phonetic(phonetic, spelling, candidates):
    target = spelling.lower()
    if phonetic and phonetic.lower() != target:
        return phonetic
    for _, other, _ in candidates:
        if other and other.lower() != target:
            return other
    return phonetic


def clean_translation(text):
    """ECDICT 的译文压成一行：先丢掉 [计] [经] [医] [网络] 这类专业域补充，换行改成分号。

    背单词练的是通用词义，「[计] 主存储器」这种行业联想既不好看也容易把答案判宽；
    但有些冷门词词典里只给了域标注的译文，全丢掉就没释义了，所以留一手兜底。
    """
    lines = re.split(r"\\n|\r|\n", text)
    def kept(drop_domains):
        out = []
        for line in lines:
            line = re.sub(r"<[^>]+>", "", line).strip()
            if not line or line.startswith("[网络]"):
                continue
            if drop_domains and line.startswith("["):
                continue
            out.append(line)
        return out
    for drop_domains in (True, False):
        merged = "；".join(kept(drop_domains))
        merged = re.sub(r"；+", "；", merged).strip("；")
        if merged:
            return merged
    return ""


def format_phonetic(phonetic):
    """ECDICT 的音标不带斜杠，个别条目首字符是逗号，清一遍再包 /…/。

    词典里那套音标符号是几十年前的排版习惯，和项目里手写的 3A 种子词不是同一套字符，
    直接显示出来会在同一页上出现两种写法（`ә` 是西里尔字母、`^` 是 g 的乱码、
    重音用了半角引号）。这里逐个字符换成通用 IPA，只换字符不改音，读法不变。
    """
    text = phonetic.strip().strip(",").strip()
    if not text:
        return None
    text = text.translate(PHONETIC_CHARS)
    if not re.search(r"[A-Za-z]", text):
        return None        # 只有重音符号之类，不是能看的音标
    if text.startswith("/") and text.endswith("/"):
        return text
    return "/" + text + "/"


def scan_package(source):
    """录音包 → ({小写词形: 分组}, 丢弃的文件, 解析成功的文件数)

    分组里有：spellings（原大小写的词形，可能 China 和 china 两个）、
    files（{性别: [(词形, 文件名), …]} 按文件名排序，同形异音词会在这里并列）、
    stems（去重后的原始词形，用来报告折叠掉了几个）。
    """
    groups = {}
    unparsed = []
    parsed = 0
    for fname in sorted(os.listdir(source)):
        path = os.path.join(source, fname)
        if not os.path.isfile(path) or not fname.lower().endswith((".mp3", ".wav")):
            continue
        base, _ext = os.path.splitext(fname)
        match = AUDIO_NAME_RE.match(base)
        if not match:
            unparsed.append(fname)
            continue
        stem, voice = match.group("stem"), match.group("voice").lower()
        word, reason = normalize_stem(stem)
        if word is None:
            unparsed.append(fname + "（" + reason + "）")
            continue
        parsed += 1
        key = word.lower()
        group = groups.setdefault(key, {"spellings": [], "stems": [], "files": {}})
        if word not in group["spellings"]:
            group["spellings"].append(word)
        if stem not in group["stems"]:
            group["stems"].append(stem)
        group["files"].setdefault(voice, []).append((stem, fname))
    return groups, unparsed, parsed


def main():
    parser = argparse.ArgumentParser(description="用 ECDICT 给 Quiz3000 的词补释义")
    parser.add_argument("--source", default=os.environ.get("QUIZ3000_DIR", DEFAULT_SOURCE))
    parser.add_argument("--dict", required=True, help="ECDICT 的 ecdict.csv 路径")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    for path, label in ((args.source, "录音包目录"), (args.dict, "ECDICT 词典")):
        if not os.path.exists(path):
            print("找不到%s：%s" % (label, path))
            return 1

    groups, unparsed, parsed = scan_package(args.source)
    print("录音包里 %d 个单词音频，折叠成 %d 个词形；%d 个文件不是「词.性别.后缀」的形式，丢弃"
          % (parsed, len(groups), len(unparsed)))
    if unparsed:
        print("  丢弃的文件：" + " ".join(unparsed))

    dict_rows = load_dictionary(args.dict, set(groups))
    entries = []
    no_gloss = []
    no_phonetic = []
    case_fallback = []
    for key in sorted(groups):
        group = groups[key]
        for spelling in group["spellings"]:
            # 先按原大小写查（专有名词 China / 普通名词 china 是两道题），
            # 大小写查不到再用同小写的条目：词典里 CORE、FAX、God 只有大写，
            # August、May 只有小写，而它们的释义正好也覆盖月份那层意思
            phonetic, translation, matched = dictionary_entry(dict_rows, spelling)
            meaning = clean_translation(translation)
            if spelling in MANUAL_GLOSSES:
                meaning = MANUAL_GLOSSES[spelling]
            if not meaning:
                no_gloss.append(spelling)
                continue
            if matched and matched != spelling:
                case_fallback.append("%s←%s" % (spelling, matched))
            show = format_phonetic(phonetic)
            if not show:
                no_phonetic.append(spelling)
            entries.append({
                "word": spelling,
                "phonetic": show,
                "meaning_cn": meaning,
            })

    merged = sorted(key for key, g in groups.items() if len(g["stems"]) > 1)
    both = sum(1 for g in groups.values() if set(g["files"]) == {"f", "m"})
    one_voice = sorted(key for key, g in groups.items() if set(g["files"]) != {"f", "m"})

    if no_gloss:
        print("  词典里查不到、或没有中文释义的 %d 个词形不进词表：%s"
              % (len(no_gloss), " ".join(no_gloss[:60])))
    if case_fallback:
        print("  %d 个词形靠同小写的词典条目补的释义（大小写不同，已核对词义覆盖）：%s"
              % (len(case_fallback), " ".join(case_fallback)))
    print("  %d 个词形把名词/动词两个文件折叠成一个拼写（词典把两义合在一条释义里）：%s"
          % (len(merged), " ".join(merged[:40])))
    if one_voice:
        print("  只有一种性别的词形 %d 个：%s" % (len(one_voice), " ".join(one_voice[:40])))
    if no_phonetic:
        print("  词典里连同小写条目都没给音标的 %d 个词形，前端只显示音频不显示音标：%s"
              % (len(no_phonetic), " ".join(no_phonetic[:60])))

    payload = {
        "generated_by": "scripts/build_quiz3000_words.py",
        "dictionary": "ECDICT (skywind3000/ECDICT) ecdict.csv",
        "note": "Quiz3000 朗读包里的词形，已按词典过滤并补齐中文释义；删掉某行即从词库排除该词",
        "words": entries,
    }
    # 一个词条一行：四千多行的词表在 git diff 里看得清改了哪个词
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("{\n")
        for key in ("generated_by", "dictionary", "note"):
            fh.write('  %s: %s,\n' % (json.dumps(key), json.dumps(payload[key], ensure_ascii=False)))
        fh.write('  "words": [\n')
        for index, entry in enumerate(entries):
            fh.write("    " + json.dumps(entry, ensure_ascii=False) +
                     (",\n" if index + 1 < len(entries) else "\n"))
        fh.write("  ]\n}\n")

    print("词表写出 %d 个词条 → %s（男女声齐全的词形 %d 个）"
          % (len(entries), os.path.relpath(args.out, BASE_DIR), both))
    print("下一步：python scripts/import_quiz3000.py 复制音频，再 python scripts/init_db.py 登记")
    return 0


if __name__ == "__main__":
    sys.exit(main())
