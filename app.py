import os
import json
import uuid
from functools import wraps

import config
from db import get_db, close_db
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from services import auth, practice

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.teardown_appcontext(close_db)

app.secret_key = config.SECRET_KEY
if not os.environ.get("SECRET_KEY"):
    app.logger.warning(
        "未设置 SECRET_KEY，已改用随机密钥；进程重启后所有登录状态会失效。"
    )

RECORDING_EXTENSIONS = {"webm", "ogg", "wav", "mp4", "m4a", "mp3", "mpeg"}


# ---------------------------------------------------------------- 工具

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
    rec_prefix = config.RECORDING_DIR.replace("\\", "/").rstrip("/") + "/"
    if rel.startswith(rec_prefix):
        # 录音不在 static 下，必须走 /recordings/ 路由
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


# ---------------------------------------------------------------- 静态资源

@app.route("/js/<path:filename>")
def scripts_js(filename):
    """前端 JS 放在 scripts/ 目录，只允许取 .js 文件。"""
    if not filename.endswith(".js"):
        return "Forbidden", 403
    return send_from_directory(os.path.join(BASE_DIR, "scripts"), filename)


@app.route("/recordings/<path:filename>")
def recordings(filename):
    """用户上传的朗读音频，不放在 static 下，避免被当成源码目录。"""
    return send_from_directory(os.path.join(BASE_DIR, config.RECORDING_DIR), filename)


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
        if password != confirm:
            return render_template("register.html", error="两次输入的密码不一致",
                                   username=username, nickname=nickname), 400
        try:
            user = auth.register(username, password, nickname)
        except auth.AuthError as exc:
            return render_template("register.html", error=str(exc),
                                   username=username, nickname=nickname), 400
        session["user_id"] = user["id"]
        return redirect(url_for("practice"))

    return render_template("register.html")


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
            "voice": current_user()["voice"],
        },
    )


@app.route("/practice/read-translate")
@login_required
def practice_read_translate():
    return render_template(
        "practice_read_translate.html",
        defaults={"count": current_user()["default_count"]},
    )


@app.route("/practice/listen-spell")
@login_required
def practice_listen_spell():
    return render_template(
        "practice_listen_spell.html",
        defaults={"count": current_user()["default_count"]},
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
            )
            message = "设置已保存"
        except auth.AuthError as exc:
            message = f"保存失败：{exc}"
        refresh_current_user()
    user = auth.get_user(current_user()["id"])
    return render_template("settings.html", user=user, message=message)


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
    return render_template("help.html")


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

    params = json.loads(row["params_json"] or "{}")
    return render_template("result.html", session_row=dict(row), params=params, items=items)


# ---------------------------------------------------------------- 接口

@app.post("/api/vowel/questions")
@api_login_required
def api_vowel_questions():
    data = json_body()
    mode = data.get("mode", "word")
    if not practice.VOWEL_MODES.get(mode):
        return jsonify({"error": "句子练习的音频题库还没上线，请先选单词练习"}), 400
    try:
        questions = practice.build_vowel_questions(
            data.get("group_key", ""),
            data.get("count", 10),
            data.get("voice", "f"),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    for q in questions:
        q["audio_url"] = audio_url(q["audio"])
        q.pop("audio", None)
    return jsonify({"questions": questions, "time_limit": current_user()["time_limit"]})


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
    words = practice.random_words(data.get("count", config.DEFAULT_QUESTION_COUNT))
    for w in words:
        w["audio_url"] = audio_url(w.pop("audio_path"))
    return jsonify({"words": words})


@app.post("/api/practice/session")
@api_login_required
def api_create_session():
    data = json_body()
    ptype = data.get("practice_type")
    if ptype not in practice.PRACTICE_TYPES:
        return jsonify({"error": "未知的练习类型"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO practice_sessions(user_id, practice_type, params_json) VALUES(?,?,?)",
        (current_user()["id"], ptype, json.dumps(data.get("params") or {}, ensure_ascii=False)),
    )
    db.commit()
    return jsonify({"session_id": cur.lastrowid})


@app.post("/api/practice/finish")
@api_login_required
def api_finish_session():
    """练习结束时一次性提交全部作答记录，题目信息和音频路径都存进 question_json 供回看。"""
    data = json_body()
    session_id = data.get("session_id")
    records = data.get("records") or []
    uid = current_user()["id"]

    db = get_db()
    row = db.execute(
        "SELECT id FROM practice_sessions WHERE id = ? AND user_id = ?",
        (session_id, uid),
    ).fetchone()
    if not row:
        return jsonify({"error": "练习会话不存在"}), 404

    db.execute("DELETE FROM practice_records WHERE session_id = ?", (session_id,))
    correct = 0
    for rec in records:
        is_correct = 1 if rec.get("is_correct") else 0
        correct += is_correct
        payload = rec.get("question") or {}
        payload.setdefault("type", rec.get("type"))
        db.execute(
            "INSERT INTO practice_records(session_id, seq, question_json, user_answer, "
            "is_correct, recording_path) VALUES(?,?,?,?,?,?)",
            (
                session_id,
                int(rec.get("seq", 0)),
                json.dumps(payload, ensure_ascii=False),
                rec.get("user_answer"),
                is_correct,
                rec.get("recording_path"),
            ),
        )
    total = len(records)
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

    rel_dir = config.RECORDING_DIR
    os.makedirs(os.path.join(BASE_DIR, rel_dir), exist_ok=True)
    name = f"u{current_user()['id']}_{uuid.uuid4().hex[:12]}.{ext}"
    file.save(os.path.join(BASE_DIR, rel_dir, name))
    return jsonify({"path": f"{rel_dir}/{name}", "url": url_for("recordings", filename=name)})


@app.get("/api/me")
def api_me():
    user = current_user()
    if not user:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "username": user["username"],
        "nickname": user["nickname"],
        "settings": {
            "default_count": user["default_count"],
            "time_limit": user["time_limit"],
            "voice": user["voice"],
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=config.DEBUG)
