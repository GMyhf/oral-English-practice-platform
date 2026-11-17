import os
import json
import re
import uuid
from functools import wraps

import config
from db import get_db, close_db
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from services import auth, practice, registration

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.teardown_appcontext(close_db)

app.secret_key = config.SECRET_KEY
if not os.environ.get("SECRET_KEY"):
    app.logger.warning(
        "未设置 SECRET_KEY，已改用随机密钥；进程重启后所有登录状态会失效。"
    )

# 上传体积上限由应用自己把住，不能只指望前置的 nginx client_max_body_size：
# 换一台前面没有 nginx 的机器（本地调试、直接 gunicorn、以后换反代）就是裸奔，
# 而一个 20MB 的录音包在 1 核机上足以把内存和磁盘一起顶穿。
# 超限后 Werkzeug 在读取请求体时就抛 413，body 根本不会落地，见下面的 errorhandler。
app.config["MAX_CONTENT_LENGTH"] = int(config.MAX_UPLOAD_MB * 1024 * 1024)

RECORDING_EXTENSIONS = {"webm", "ogg", "wav", "mp4", "m4a", "mp3", "mpeg"}

# 近似音两种题型走各自的出题函数，回包结构完全一样
VOWEL_BUILDERS = {
    "word": practice.build_vowel_questions,
    "sentence": practice.build_sentence_questions,
}

# 录音文件名一律由后端生成：u<用户 id>_<12 位十六进制>.<扩展名>
RECORDING_NAME_RE = re.compile(r"^u(\d+)_[0-9a-f]{12}\.[A-Za-z0-9]{1,8}$")


# ---------------------------------------------------------------- 工具

def recording_dir():
    """录音目录在文件系统上的绝对路径（RECORDING_DIR 相对/绝对都接受）。"""
    raw = (config.RECORDING_DIR or "data/recordings").replace("\\", "/")
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(BASE_DIR, raw))


def recording_prefix():
    """录音路径存进库、以及从库里比对时统一用这个前缀：永远相对项目根。

    这是 RECORDING_DIR 那个坑的正解。以前四处直接拼 config.RECORDING_DIR，
    一旦部署时填成绝对路径（/opt/english-practice/app/data/recordings），
    库里就存成绝对路径，而 audio_url() 比对前会先 lstrip("/")，
    前缀怎么都对不上，最后落到 `return "/" + rel` 那个兜底分支 ——
    结果页的录音链接变成 /opt/english-practice/... 这种 404，
    而且只在生产机上出现，本地永远测不出来。
    """
    target = recording_dir()
    try:
        rel = os.path.relpath(target, BASE_DIR)
    except ValueError:      # Windows 上跨盘符时 relpath 会抛，退回原样
        rel = target
    return rel.replace("\\", "/").rstrip("/")


def recording_owner(filename):
    """从文件名里取出后端当初登记的归属 id；文件名不合规则返回 None。"""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = RECORDING_NAME_RE.match(name)
    return int(match.group(1)) if match else None


def own_recording_path(rel_path, user_id):
    """只承认后端为当前用户生成的录音文件。

    提交成绩时 recording_path 来自前端，不加校验就能把别人的录音路径写进
    自己的记录里回放，也允许塞进任意字符串。不匹配时按「没有录音」处理。
    """
    if not rel_path:
        return None
    name = str(rel_path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if recording_owner(name) != user_id:
        return None
    return f"{recording_prefix()}/{name}"


MISSING = object()


def as_count(value, fallback):
    """把前端传来的题量规整成 1..MAX_QUESTION_COUNT，非法值抛 ValueError 让接口回 400。

    键根本没出现时用 fallback；显式传 null 是前端的 bug，不能被默认值悄悄掩盖。
    """
    if value is MISSING:
        return fallback
    if value is None:
        raise ValueError("题目数量不能是 null")
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("题目数量必须是数字")
    if number < 1:
        raise ValueError("题目数量至少为 1")
    return min(number, config.MAX_QUESTION_COUNT)

def current_user():
    if "user_id" not in g:
        g.user_id = session.get("user_id")
        g.user = auth.get_user(g.user_id) if g.user_id else None
        if g.user_id and not g.user:      # 账号被删了，清掉过期会话
            session.pop("user_id", None)
            g.user_id = None
    return g.user


def refresh_current_user():
    """改完资料后重新查库，让本次请求的导航栏也显示新值。"""
    g.pop("user_id", None)
    g.pop("user", None)
    return current_user()


@app.context_processor
def inject_user():
    return {"current_user": current_user(), "nav_active": request.endpoint}


def safe_next(target):
    """只允许跳回本站相对路径，避免开放重定向。"""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "请先登录"}), 401
        return redirect(url_for("login", next=request.path))

    return wrapped


def api_login_required(view):
    """接口专用：只接受 session 登录，未登录返回 401 JSON。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "请先登录"}), 401
        return view(*args, **kwargs)

    return wrapped


def audio_url(rel_path):
    """把数据库里的相对路径转成可访问 URL。"""
    if not rel_path:
        return None
    rel = rel_path.replace("\\", "/").lstrip("/")
    if rel.startswith("static/"):
        return url_for("static", filename=rel[len("static/"):])
    rec_prefix = recording_prefix() + "/"
    if rel.startswith(rec_prefix):
        # 录音不在 static 下，必须走 /recordings/ 路由（那里有归属校验）
        return url_for("recordings", filename=rel[len(rec_prefix):])
    return "/" + rel


def json_body():
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------- 错误页

@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在", "path": request.path}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e):
    return "服务器内部错误，请稍后重试", 500


@app.errorhandler(403)
def forbidden(e):
    return "没有权限访问", 403


@app.errorhandler(413)
def payload_too_large(e):
    """超过 config.MAX_UPLOAD_MB 时，Werkzeug 在读请求体阶段就抛到这里。

    回 JSON 而不是默认那张 HTML 错误页：前端这条链路全是 fetch，
    拿到 HTML 只会显示「提交失败」，学生根本不知道是自己录音太长。
    最常见的原因是 iOS Safari 走的是未压缩 WAV 兜底路径（44.1kHz 16bit，
    一分钟就是 10MB 上下），一题读久了就会顶到上限。
    """
    limit = int(config.MAX_UPLOAD_MB)
    detail = f"上传内容超过 {limit}MB，这一题的录音太长了，请重新录短一些"
    if request.path.startswith("/api/"):
        return jsonify({"error": detail}), 413
    return detail, 413


# ---------------------------------------------------------------- 静态资源

@app.route("/js/<path:filename>")
def scripts_js(filename):
    """前端 JS 放在 scripts/ 目录，只允许取 .js 文件。"""
    if not filename.endswith(".js"):
        return "Forbidden", 403
    return send_from_directory(os.path.join(BASE_DIR, "scripts"), filename)


@app.route("/recordings/<path:filename>")
def recordings(filename):
    """用户上传的朗读音频，不放在 static 下，避免被当成源码目录。

    这里必须做归属校验。以前是裸 `send_from_directory`，防线只有「文件名里那
    12 位十六进制猜不到」—— 可 uuid 不是机密：结果页的 HTML 里就写着完整 URL，
    任何人拿到链接（截图、分享、浏览器历史、访问日志）就能永久回放别人的朗读，
    而录音里往往有学生自报家门。写入侧 own_recording_path() 早就在比归属了，
    读取侧不比对等于是半个漏洞。

    不是本人一律 404，不用 403 —— 403 等于承认「这个文件确实存在」。
    """
    name = filename.replace("\\", "/")
    if "/" in name:                       # 录音是平铺的，带子目录的都不是我们发的
        abort(404)
    user = current_user()
    if not user or recording_owner(name) != user["id"]:
        abort(404)
    return send_from_directory(recording_dir(), name)


# ---------------------------------------------------------------- 页面路由

@app.route("/")
def index():
    groups = practice.list_vowel_groups()
    word_count = get_db().execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"]
    return render_template("index.html", vowel_groups=groups, word_count=word_count)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            user = auth.verify_login(username, password)
        except auth.HashQueueFull:
            # 哈希闸门排不下 = 登录的人实在太多。快速回一句话让人重试，
            # 比让请求挂在那儿、最后被 nginx 的 60 秒超时切成 504 好得多。
            return render_template(
                "login.html", error="同时登录的人有点多，请过几秒再试一次"), 503
        except auth.AuthError as exc:
            return render_template("login.html", error=str(exc)), 401
        session["user_id"] = user["id"]
        return redirect(safe_next(request.values.get("next")) or url_for("practice"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        nickname = request.form.get("nickname", "")
        # 码原样回显：填错了不用重敲，学生手机上一个一个字补很难受
        invite_code = request.form.get("invite_code", "").strip()
        # 提交时的口径以服务端为准，不看前端有没有把输入框渲染出来：
        # 拿着 open 状态下缓存的表单 POST 到 invite / closed 状态，一样会被挡。
        gate = registration.state()

        def fail(message, status=400):
            return render_template("register.html", error=message, gate=gate,
                                   username=username, nickname=nickname,
                                   invite_code=invite_code), status

        if not gate["form_open"]:
            return fail(gate["hint"], 403)
        if password != confirm:
            return fail("两次输入的密码不一致")
        try:
            user = auth.register(username, password, nickname, invite_code)
        except auth.HashQueueFull:
            return fail("同时注册的人有点多，请过几秒再试一次", 503)
        except auth.AuthError as exc:
            return fail(str(exc))
        session["user_id"] = user["id"]
        return redirect(url_for("practice"))

    return render_template("register.html", gate=registration.state())


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))


@app.route("/practice", endpoint="practice")
@login_required
def practice_page():
    return render_template("practice.html", vowel_groups=practice.list_vowel_groups())


@app.route("/practice/vowel")
@login_required
def practice_vowel():
    return render_template(
        "practice_vowel.html",
        vowel_groups=practice.list_vowel_groups(),
        modes=practice.VOWEL_MODES,
        defaults={
            "count": current_user()["default_count"],
            "time_limit": current_user()["time_limit"],
            # 没有 voice：近似音每题随机抽男声/女声，账号里的旧设置不再参与出题
        },
    )


def _fmt_seconds(value):
    """输入框里的数：5.0 写成 5、1.50 写成 1.5。挂一串 .0 既难看也更占地方。"""
    return f"{round(float(value), 2):g}"


def rhythm_defaults(user=None):
    """看读译 / 听读写那三个时长输入框的 min / max / 默认值。

    边界只有 `config.RHYTHM_FIELDS` 这一份；**默认值优先取账号里存的那份**（在 /settings 改），
    没存过就退回 config 的默认值，存了个出格的值（比如以后把上限调小了）就夹回边界。
    库里连这一列都还没有（老库没跑 scripts/init_db.py 补列）也当「没存过」处理，
    练习页照常能开，只是设置保存不下来。

    模板负责渲染边界，tworounds.js 再按输入框的 min/max 夹一遍，
    不会出现「页面允许 999 秒而脚本按 5 秒跑」这种两边不一致。
    """
    out = {}
    for name, (column, (lo, hi, default)) in config.RHYTHM_FIELDS.items():
        value = default
        stored = user.get(column) if isinstance(user, dict) else None
        if stored is not None:
            try:
                value = max(lo, min(hi, float(stored)))
            except (TypeError, ValueError):
                value = default          # 库里是脏值就当没存过，别把练习页弄白屏
        out[name] = {"min": lo, "max": hi, "value": _fmt_seconds(value)}
    return out


def overlap_default(user=None):
    """听读写第一轮「边播边录」这个开关的用户默认值；没存过按以前的默认（开着）。"""
    if not isinstance(user, dict):
        return True
    stored = user.get("overlap_record")
    return True if stored is None else bool(stored)


# 会话 params 里存的是毫秒（JS 那边只用毫秒），结果页要换回人能读的秒
RHYTHM_LABELS = {
    "round1_ms": "第一轮（秒）",
    "round2_ms": "第二轮（秒）",
    "round_gap_ms": "题间停顿（秒）",
}

# 开关项不是时长，不能跟着除以 1000，单独给一套中文值
SWITCH_LABELS = {
    "overlap_record": ("第一轮录音方式", {True: "边播边录", False: "先听后录"}),
}


def humanize_params(params):
    """把 round1_ms=1000 这类内部字段渲染成「第一轮（秒）= 1」。"""
    out = {}
    for key, value in params.items():
        label = RHYTHM_LABELS.get(key)
        if label is not None:
            try:
                out[label] = f"{round(float(value) / 1000, 2):g}"
            except (TypeError, ValueError):
                out[label] = value      # 老数据里万一存了字符串，原样显示而不是报错
            continue
        if key in SWITCH_LABELS:
            name, mapping = SWITCH_LABELS[key]
            out[name] = mapping.get(bool(value), str(value))
            continue
        out[key] = value
    return out


@app.route("/practice/read-translate")
@login_required
def practice_read_translate():
    user = current_user()
    return render_template(
        "practice_read_translate.html",
        defaults={"count": user["default_count"]},
        rhythm=rhythm_defaults(user),
    )


@app.route("/practice/listen-spell")
@login_required
def practice_listen_spell():
    user = current_user()
    return render_template(
        "practice_listen_spell.html",
        defaults={"count": user["default_count"]},
        rhythm=rhythm_defaults(user),
        overlap=overlap_default(user),
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    message = None
    if request.method == "POST":
        try:
            auth.update_settings(
                current_user()["id"],
                default_count=request.form.get("default_count"),
                time_limit=request.form.get("time_limit"),
                voice=request.form.get("voice"),
                # 三段时长：和练习页用同一套 name，越界由 update_settings 夹回 config 的边界
                round1_seconds=request.form.get("round1_seconds"),
                round2_seconds=request.form.get("round2_seconds"),
                round_gap_seconds=request.form.get("round_gap"),
                # 复选框不勾就整个字段不提交，必须补一个 "0"，否则「关掉边播边录」存不下去
                overlap_record=request.form.get("overlap_record", "0"),
            )
            message = "设置已保存"
        except auth.AuthError as exc:
            message = f"保存失败：{exc}"
        refresh_current_user()
    user = auth.get_user(current_user()["id"])
    return render_template("settings.html", user=user, message=message,
                           rhythm=rhythm_defaults(user),
                           overlap=overlap_default(user))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    message = None
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        uid = current_user()["id"]
        try:
            if action == "username":
                new_name = auth.change_username(uid, request.form.get("new_username"))
                message = f"用户名已改为 {new_name}"
            elif action == "password":
                auth.change_password(
                    uid, request.form.get("old_password"), request.form.get("new_password")
                )
                message = "密码已修改"
            else:
                error = "未知操作"
        except auth.AuthError as exc:
            error = str(exc)
        except auth.HashQueueFull:
            # 哈希闸门（config.MAX_CONCURRENT_HASHES）排队超时，不是用户输错了什么，
            # 措辞别和「原密码不正确」混在一起，免得人去反复试密码。
            error = "同时操作的人有点多，请过几秒再试一次"
        refresh_current_user()

    user = auth.get_user(current_user()["id"])
    recent = get_db().execute(
        "SELECT id, practice_type, total, correct_count, wrong_count, created_at "
        "FROM practice_sessions WHERE user_id = ? AND finished = 1 "
        "ORDER BY id DESC LIMIT 10",
        (user["id"],),
    ).fetchall()
    return render_template("account.html", user=user, message=message,
                           error=error, recent_sessions=recent)


@app.route("/help")
def help_page():
    # 帮助页要告诉学生词库有多大，数字从库里现取，加词之后不用改文案
    word_count = get_db().execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"]
    return render_template("help.html", word_count=word_count)


@app.route("/result/<int:session_id>")
@login_required
def result_page(session_id):
    row = get_db().execute(
        "SELECT * FROM practice_sessions WHERE id = ? AND user_id = ?",
        (session_id, current_user()["id"]),
    ).fetchone()
    if not row:
        return render_template("404.html"), 404

    records = get_db().execute(
        "SELECT seq, question_json, user_answer, is_correct, recording_path "
        "FROM practice_records WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    items = []
    for rec in records:
        item = json.loads(rec["question_json"])
        item["seq"] = rec["seq"]
        item["user_answer"] = rec["user_answer"]
        item["is_correct"] = bool(rec["is_correct"])
        item["recording_url"] = audio_url(rec["recording_path"]) or ""
        # Jinja 会把 None 渲染成字符串 "None"，模板里的 if (url) 判断会被骗过
        item["audio_url"] = item.get("audio_url") or ""
        items.append(item)

    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}     # 早期脏数据里 params 可能是字符串，避免结果页整页 500
    return render_template("result.html", session_row=dict(row),
                           params=humanize_params(params), items=items)


# ---------------------------------------------------------------- 接口

@app.post("/api/vowel/questions")
@api_login_required
def api_vowel_questions():
    data = json_body()
    mode = data.get("mode", "word")
    builder = VOWEL_BUILDERS.get(mode)
    if builder is None:
        return jsonify({"error": "未知的练习类型，请选单词练习或句子练习"}), 400
    try:
        requested = as_count(data.get("count", MISSING), 10)
        questions = builder(
            data.get("group_key", ""),
            requested,
            # 不再按账号设置固定性别：默认随机，只有显式传 f/m 才锁定
            data.get("voice", practice.DEFAULT_VOWEL_VOICE),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    for q in questions:
        q["audio_url"] = audio_url(q["audio"])
        q.pop("audio", None)
    return jsonify({
        "questions": questions,
        "mode": mode,
        "time_limit": current_user()["time_limit"],
        "requested": requested,
        "actual": len(questions),
    })


@app.get("/api/words/all")
@api_login_required
def api_words_all():
    """完整词表，只用于前端「音频导入」的文件名匹配。"""
    rows = get_db().execute("SELECT word FROM words ORDER BY word").fetchall()
    return jsonify({"words": [{"word": r["word"]} for r in rows]})


@app.post("/api/words")
@api_login_required
def api_words():
    data = json_body()
    try:
        requested = as_count(data.get("count", MISSING), config.DEFAULT_QUESTION_COUNT)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    words, bank_size = practice.random_words(requested)
    for w in words:
        # practice.random_words 已经按题随机挑好性别，这里只把相对路径换成 URL
        w["audio_url"] = audio_url(w.pop("audio"))
    # actual < requested 说明词库不够，前端要提示用户实际只练了 actual 题
    return jsonify({
        "words": words,
        "requested": requested,
        "actual": len(words),
        "bank_size": bank_size,
    })


@app.post("/api/practice/session")
@api_login_required
def api_create_session():
    data = json_body()
    ptype = data.get("practice_type")
    if ptype not in practice.PRACTICE_TYPES:
        return jsonify({"error": "未知的练习类型"}), 400
    params = data.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return jsonify({"error": "params 必须是对象"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO practice_sessions(user_id, practice_type, params_json) VALUES(?,?,?)",
        (current_user()["id"], ptype, json.dumps(params, ensure_ascii=False)),
    )
    db.commit()
    return jsonify({"session_id": cur.lastrowid})


@app.post("/api/practice/finish")
@api_login_required
def api_finish_session():
    """练习结束时一次性提交全部作答记录，题目信息和音频路径都存进 question_json 供回看。"""
    data = json_body()
    session_id = data.get("session_id")
    records = data.get("records")
    uid = current_user()["id"]

    if not isinstance(records, list):
        return jsonify({"error": "records 必须是数组"}), 400
    if len(records) > config.MAX_QUESTION_COUNT:
        return jsonify({"error": f"单次最多提交 {config.MAX_QUESTION_COUNT} 题"}), 400

    db = get_db()
    row = db.execute(
        "SELECT id FROM practice_sessions WHERE id = ? AND user_id = ?",
        (session_id, uid),
    ).fetchone()
    if not row:
        return jsonify({"error": "练习会话不存在"}), 404

    # 先把所有记录校验完再动数据库：边校验边写会让一条脏数据把已有成绩删掉后 500
    parsed = []
    for index, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            return jsonify({"error": f"第 {index} 条记录格式不正确"}), 400
        try:
            seq = int(rec.get("seq", index))
        except (TypeError, ValueError):
            return jsonify({"error": f"第 {index} 条记录的题号不是数字"}), 400

        # question 缺省或 null 容忍（老客户端可能不带），但带了就必须是对象，
        # 否则结果页会渲染出一张空白题卡，把前端的 bug 藏成"没记录"
        payload = rec.get("question", MISSING)
        if payload is None:
            payload = {}
        if payload is not MISSING and not isinstance(payload, dict):
            return jsonify({"error": f"第 {index} 条记录的 question 必须是对象"}), 400
        if payload is MISSING:
            payload = {}
        payload.setdefault("type", rec.get("type"))

        answer = rec.get("user_answer")
        # 只收标量：dict/list 直接 str() 会存成 "{'a': 1}" 这样的脏答案显示在结果页上
        if answer is not None and not isinstance(answer, (str, int, float, bool)):
            return jsonify({"error": f"第 {index} 条记录的 user_answer 必须是文本或数字"}), 400
        parsed.append((
            seq,
            json.dumps(payload, ensure_ascii=False),
            None if answer is None else str(answer)[:200],
            1 if rec.get("is_correct") else 0,
            own_recording_path(rec.get("recording_path"), uid),
        ))

    db.execute("DELETE FROM practice_records WHERE session_id = ?", (session_id,))
    correct = 0
    for seq, payload_json, answer, is_correct, recording in parsed:
        correct += is_correct
        db.execute(
            "INSERT INTO practice_records(session_id, seq, question_json, user_answer, "
            "is_correct, recording_path) VALUES(?,?,?,?,?,?)",
            (session_id, seq, payload_json, answer, is_correct, recording),
        )
    total = len(parsed)
    db.execute(
        "UPDATE practice_sessions SET total = ?, correct_count = ?, wrong_count = ?, "
        "finished = 1 WHERE id = ?",
        (total, correct, total - correct, session_id),
    )
    db.commit()
    return jsonify({
        "total": total,
        "correct": correct,
        "wrong": total - correct,
        "result_url": url_for("result_page", session_id=session_id),
    })


@app.post("/api/recording")
@api_login_required
def api_upload_recording():
    """保存用户朗读录音，返回可回放的 URL。"""
    file = request.files.get("audio")
    if not file or not file.filename:
        return jsonify({"error": "没有收到音频"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in RECORDING_EXTENSIONS:
        ext = "webm"

    rel_dir = recording_prefix()
    os.makedirs(recording_dir(), exist_ok=True)
    name = f"u{current_user()['id']}_{uuid.uuid4().hex[:12]}.{ext}"
    file.save(os.path.join(recording_dir(), name))
    return jsonify({"path": f"{rel_dir}/{name}", "url": url_for("recordings", filename=name)})


@app.get("/api/me")
def api_me():
    user = current_user()
    if not user:
        return jsonify({"logged_in": False})
    rhythm = rhythm_defaults(user)
    return jsonify({
        "logged_in": True,
        "username": user["username"],
        "nickname": user["nickname"],
        "settings": {
            "default_count": user["default_count"],
            "time_limit": user["time_limit"],
            "voice": user["voice"],       # 历史列：近似音现在每题随机抽男女声
            # 看读译 / 听读写那四个走的是渲染练习页同一套函数（并且一样夹回边界），
            # 这样「API 报的默认值」和「学生打开练习页看到的默认值」不可能对不上。
            "round1_seconds": float(rhythm["round1"]["value"]),
            "round2_seconds": float(rhythm["round2"]["value"]),
            "round_gap_seconds": float(rhythm["gap"]["value"]),
            "overlap_record": overlap_default(user),
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=config.DEBUG)
