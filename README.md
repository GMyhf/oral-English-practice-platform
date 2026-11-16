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
scripts/init_db.py    建库、种子数据、扫描 static/audio 下的近似音 / 句子 / 单词音频
scripts/import_audio_package.py
                      把教材配套录音（3B 单词对立对 / 4A 句子对立词 / 3A 单词朗读）
                      复制进 static/audio，命名沿用平台约定，幂等可重跑
scripts/sentence_texts.json
                      4A 整句文本（由语音转写脚本生成后人工校验，进版本库）
scripts/*.js          前端脚本（由 /js/<name> 路由提供，只放行 .js）
templates/            Jinja2 模板
static/css/style.css  自定义样式
static/audio/vowel/   近似音单词音频，命名 n.m-word.f.wav / n.m-word.m.wav
static/audio/sentence/
                      近似音整句音频，命名 n.m-word.f.mp3 / n.m-word.m.mp3
static/audio/word/    单词朗读音频（教材 3A），登记到 words.audio_path
data/app.db           SQLite 数据库
data/recordings/      用户朗读录音（已 gitignore，不进版本库）
```

## 三个模块的流程

| 模块 | 第一轮 | 第二轮 |
|---|---|---|
| 近似音区分 `/practice/vowel` | 屏幕标出 1 / 2 两个词，随机播放其中一个（单词练习播 3B 的单词，句子练习播 4A 的整句，选项都只给变化的那两个词） | 限时内按键盘 `1` / `2`，默认 1 秒，音频播完才开始计时，判完立刻下一题 |
| 看读译 `/practice/read-translate` | 单词展示一轮时长并录音（默认 1 秒） | 单词展示二轮时长，键入中文译文（任写一项即可，默认 5 秒） |
| 听读写 `/practice/listen-spell` | 播放音频后按一轮时长录音跟读（默认 1 秒） | 再播一次音频，二轮时长内键入拼写（默认 5 秒） |

看读译 / 听读写页的「练习设置」里有三个时长输入框——第一轮、第二轮、题目之间的停顿——
可以在开始前自己改，边界写在 `config.py` 的 `ROUND1_SECONDS` / `ROUND2_SECONDS` /
`ROUND_GAP_SECONDS`（依次 0.3~20、1~60、0~3 秒，默认 1 / 5 / 0.2 就是以前写死的那三个值）。
点「开始练习」时读一次并夹到边界内，中途改框不影响正在跑的这轮；三段毫秒数会存进会话
params，结果页标成「第一轮（秒） = 3」这样的中文徽章。

结束后统一提交到 `/api/practice/finish`，跳转 `/result/<会话 id>` 逐题回看，
可以重听标准发音和自己的朗读。

## 音频来源优先级

看读译 / 听读写练习页底部有「音频导入」面板，词库单词默认没有真人音频：

1. 服务器音频（`words.audio_path`）
2. 本地导入的音频（存在浏览器 IndexedDB，文件名与单词同名即自动匹配，如 `abandon.mp3`）
3. 浏览器语音合成兜底

近似音模块的音频来自 `static/audio/vowel/`（单词）和 `static/audio/sentence/`（整句），
由 `init_db.py` 扫描入库；词库单词的音频来自 `static/audio/word/`，登记到
`words.audio_path`。三类音频都由 `scripts/import_audio_package.py` 从教材配套录音复制而来：

```bash
python scripts/import_audio_package.py --dry-run        # 只体检
python scripts/import_audio_package.py --only vowel     # 3B 单词对立对
python scripts/import_audio_package.py --only word      # 3A 单词朗读
python scripts/import_audio_package.py --only sentence --manifest <转写清单>   # 4A 整句
python scripts/init_db.py                               # 扫描入库
```

4A 的文件名只有「课.组.选项.对立对」，不含整句文本，所以要先用语音转写生成清单
（本机脚本，不进版本库），再由导入脚本落地音频和 `scripts/sentence_texts.json`。
转写会把教材刻意对比的词听成同音词（`pull`→`Paul`、`wooed`→`wood`），因此整句是
按「两个选项只差一个词」重建的：槽位用实际听到的词形，其余位置取四条转写的多数票。

## 已知限制

- 句子练习覆盖 31 组近似音里的 29 组：`burn-barn` 的课次没有配套 4A 录音；
  `fin-thin`（第 30 课）的 4A 录音和同课 3B 单词对不上，为避免把句子挂到错误的
  音对上，整块不导入。选到这组时页面会直接说明，出题接口返回可读的 400。
- iOS Safari 不支持 MediaRecorder 的 MP3 编码，`scripts/common.js` 在
  所有 `MediaRecorder` 类型都不支持时，回退用 Web Audio 自己编码 16-bit PCM WAV。
- 开发服务器 `debug` 默认关闭（`FLASK_DEBUG=true` 可打开），
  模板改动需要重启进程才会生效。
