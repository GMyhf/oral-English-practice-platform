# 英语词汇发音练习平台

按 `docs/design.md` 实现的 MVP：近似音区分、看读译、听读写三个练习模块，
Flask + SQLite 后端，前端原生 HTML/CSS/JS + Bootstrap 5（CDN），录音用 MediaRecorder API。

## 环境

- Python 3.11+（开发环境为 3.14）
- 浏览器：Chrome / Edge 最新版。录音用到 `getUserMedia`，
  必须通过 `localhost` 或 HTTPS 访问，用局域网 IP 走 HTTP 时浏览器会禁用麦克风。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt

python scripts/init_db.py         # 建表 + 导入 30 个种子单词 + 扫描近似音音频
python app.py                     # 打开 http://127.0.0.1:5000
```

`init_db.py` 会重复执行（`CREATE TABLE IF NOT EXISTS`）。想清空重来用
`python scripts/init_db.py --reset`，这会删除所有账号和练习记录。

## 目录结构

```
app.py                路由与 API
config.py             题量/限时默认值、音频目录等
db.py                 SQLite 连接
services/auth.py      注册、登录、改密码、设置
services/practice.py  抽题（近似音对立对随机、乱序选项）
scripts/init_db.py    建库、种子数据、扫描 static/audio/vowel 音频
scripts/*.js          前端脚本（由 /js/<name> 路由提供，只放行 .js）
templates/            Jinja2 模板
static/css/style.css  自定义样式
static/audio/vowel/   近似音真人音频，命名 n.m-word.f.wav / n.m-word.m.wav
data/app.db           SQLite 数据库
data/recordings/      用户朗读录音（已 gitignore，不进版本库）
```

## 三个模块的流程

| 模块 | 第一轮 | 第二轮 |
|---|---|---|
| 近似音区分 `/practice/vowel` | 屏幕标出 1 / 2 两个单词，随机播放其中一个 | 限时内按键盘 `1` / `2`，默认 1 秒，判完立刻下一题 |
| 看读译 `/practice/read-translate` | 单词展示 1 秒并录音 | 单词展示 5 秒，键入中文译文（任写一项即可） |
| 听读写 `/practice/listen-spell` | 播放音频后录音 1 秒 | 播放音频，5 秒内键入拼写 |

结束后统一提交到 `/api/practice/finish`，跳转 `/result/<会话 id>` 逐题回看，
可以重听标准发音和自己的朗读。

## 音频来源优先级

看读译 / 听读写练习页底部有「音频导入」面板，词库单词默认没有真人音频：

1. 服务器音频（`words.audio_path`）
2. 本地导入的音频（存在浏览器 IndexedDB，文件名与单词同名即自动匹配，如 `abandon.mp3`）
3. 浏览器语音合成兜底

近似音模块的音频来自 `static/audio/vowel/`，由 `init_db.py` 扫描入库。

## 已知限制

- 近似音只做单词模式，句子音频题库还没上线（选项在页面上是禁用的）。
- iOS Safari 不支持 MediaRecorder 的 MP3 编码，`scripts/common.js` 在
  所有 `MediaRecorder` 类型都不支持时，回退用 Web Audio 自己编码 16-bit PCM WAV。
- 开发服务器 `debug` 默认关闭（`FLASK_DEBUG=true` 可打开），
  模板改动需要重启进程才会生效。
