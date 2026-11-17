# 英语词汇发音练习平台

按 `docs/design.md` 实现的 MVP：近似音区分、看读译、听读写三个练习模块，
Flask + SQLite 后端，前端原生 HTML/CSS/JS + Bootstrap 5（CDN），录音用 MediaRecorder API。

## 环境

- Python 3.11+（开发环境为 3.14）
- 浏览器：Chrome / Edge 最新版，手机上是 Chrome Android 与 iOS Safari。
  录音用到 `getUserMedia`，必须通过 `localhost` 或 HTTPS 访问，
  用局域网 IP 走 HTTP 时浏览器会禁用麦克风。
- 推荐佩戴有线耳机：蓝牙耳机 100~300 毫秒的延迟会直接影响近似音辨听，
  听读写的「边播边录」还会把外放的读音一起录进去。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt

python scripts/init_db.py         # 建表 + 306 个手写种子词 + 扫描 static/audio（词库共 5033 词）
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
services/practice.py  抽题（近似音对立对随机、乱序选项、单词每题随机抽男声/女声）
scripts/init_db.py    建库、种子数据、扫描 static/audio 下的近似音 / 句子 / 单词音频
scripts/import_audio_package.py
                      把教材配套录音（3B 单词对立对 / 4A 句子对立词 / 3A 单词朗读）
                      复制进 static/audio，命名沿用平台约定，幂等可重跑
scripts/build_quiz3000_words.py
                      读 Quiz3000 课堂背诵录音包 + ECDICT 词典，生成 scripts/quiz3000_words.json
scripts/quiz3000_words.json
                      背单词包的词形、音标与中文释义（4977 个词形，一个词条一行，进版本库）
scripts/import_quiz3000.py
                      按上面的词表把男女声朗读复制进 static/audio/word3000，幂等可重跑
scripts/sentence_texts.json
                      4A 整句文本（由语音转写脚本生成后人工校验，进版本库）
scripts/*.js          前端脚本（由 /js/<name> 路由提供，只放行 .js）
scripts/serve_https.py
                      生成带本机 IP SAN 的自签证书，并用 HTTPS 起 Flask——
                      手机要用浏览器录音就得走 https 或 localhost，见「移动端」一节
templates/            Jinja2 模板
static/css/style.css  自定义样式
static/audio/vowel/   近似音单词音频，命名 n.m-word.f.wav / n.m-word.m.wav
static/audio/sentence/
                      近似音整句音频，命名 n.m-word.f.mp3 / n.m-word.m.mp3
static/audio/word/    单词朗读音频（教材 3A），登记到 words.audio_path
static/audio/word3000/
                      课堂背诵词男女声各一份，命名 word.f.mp3 / word.m.mp3，
                      登记到 words.audio_f / audio_m
data/app.db           SQLite 数据库
data/recordings/      用户朗读录音（已 gitignore，不进版本库）
```

## 三个模块的流程

| 模块 | 第一轮 | 第二轮 |
|---|---|---|
| 近似音区分 `/practice/vowel` | 屏幕标出 1 / 2 两个词，随机播放其中一个（单词练习播 3B 的单词，句子练习播 4A 的整句，选项都只给变化的那两个词） | 限时内点选项卡作答（电脑上也可以按键盘 `1` / `2`），默认 1 秒，音频播完才开始计时，判完立刻下一题 |
| 看读译 `/practice/read-translate` | 单词展示一轮时长并录音（默认 1.5 秒） | 单词展示二轮时长，键入中文译文（任写一项即可，默认 5 秒） |
| 听读写 `/practice/listen-spell` | 默认「边播边录」：单词音频和录音同一时刻开始，跟着标准音念；音频放完后再走满一轮时长，每词录「音频本身 + 一轮时长」（默认那 1.5 秒就是音频之后的尾巴）。关掉开关则回到先听后录 | 再播一次音频，二轮时长内键入拼写（默认 5 秒） |

看读译 / 听读写的单词音频每题随机播男声或女声（`words.audio_f` / `audio_m` 二选一），
用户不能也不该去选发音人：练的是听出这个词，不是听惯某个人的音色。抽到的性别随题目
一起返回并在结果页标出来，方便回看时对上「刚才听到的是哪一轨」。

看读译 / 听读写页的「练习设置」里有三个时长输入框——第一轮、第二轮、题目之间的停顿——
可以在开始前自己改，边界写在 `config.py` 的 `ROUND1_SECONDS` / `ROUND2_SECONDS` /
`ROUND_GAP_SECONDS`（依次 0.3~20、1~60、0~3 秒，默认 1.5 / 5 / 0.2）。
点「开始练习」时读一次并夹到边界内，中途改框不影响正在跑的这轮；三段毫秒数会存进会话
params，结果页标成「第一轮（秒） = 3」这样的中文徽章。

这三个时长连同题量、听读写的「边播边录」开关都能在 **`/settings` 练习设置**里存进账号
（`users.round1_seconds` / `round2_seconds` / `round_gap_seconds` / `overlap_record`），
下次打开练习页就是自己习惯的那套；练习页上的框仍然可以临时改，不改就直接按保存的默认值跑。
近似音那一栏存的是 `default_count` / `time_limit`，四个练习共用的题量也是同一个字段。

听读写还多一个「第一轮边播边录」开关（`tworounds.js` 里的 `overlap`，默认开着）。开着时
点「开始练习」就先 `Recorder.prime()` 申请一次麦克风，之后每题复用同一条音轨——
`getUserMedia` 每次要 0.1~0.5 秒，逐词重新申请会把标准音的开头吃掉，手机上还要逐题弹权限。
每词的录音长度取「音频本身 + `round1_ms`」：音频还在放就一直录，放完之后再走满设置的那几秒，
所以 0.9 秒的单词既不会把跟读切成半个音节，学生也有一段完整的时间自己念一遍；
「第一轮」里填的秒数就是这段尾巴的长度（0.3~20 秒），音频那一段另算。
代价是「我的朗读」里会混进标准音，戴耳机就干净了；开关状态一并存进 params，结果页显示
「第一轮录音方式 = 边播边录 / 先听后录」，回看时能对上当时是按哪种方式录的。

结束后统一提交到 `/api/practice/finish`，跳转 `/result/<会话 id>` 逐题回看，
可以重听标准发音和自己的朗读。

## 移动端

- 近似音的两张选项卡本身就是 `<button>`，点一下就算作答，不用再找键盘；判完立即
  `disabled`，连点不会把下一题的选择带跑。`static/css/style.css` 末尾的
  `@media (pointer: coarse)` 把可点的元素抬到 44px，并加上 `touch-action: manipulation`
  去掉双击缩放带来的约 300ms 点击延迟；`@media (hover: hover)` 让卡片浮起效果只在真
  鼠标上出现，触屏点完不会粘住一个 `:hover`。
- 打字环节：输入框固定 16 号字（低于这个值 iOS 会把整页放大）、`autocapitalize=off` +
  `autocorrect=off` + `spellcheck=false`（免得手机把 `abandon` 改成 `Abandon` 或自作主张
  换词）、`enterkeyhint=done`（键盘上那个键就是「提交」）。软键盘弹出时 `tworounds.js`
  的 `revealAnswer()` 按 `visualViewport` 把输入框滚回视野中间，一题判完主动 `blur()`
  把键盘收掉，别压着下一题。
- 音频：`Common.playUrl` 整页复用同一个 `<audio>`，并在点「开始练习」的那个手势里先
  `unlockAudio()` 放一段 1 毫秒静音——iOS Safari 只放行「用户手势里真的播过一次」的那个
  元素，逐题 `new Audio()` 的话从第二题起就被当成自动播放拒掉，手机上等于全程静音。
- 录音：`Recorder` 的麦克风流与 WAV 兜底用的 `AudioContext` 都是整场练习共用的，
  练习结束（或启动失败）时 `releaseStream()` 关掉，浏览器那个录音红点不会一直亮着。

### 手机上想录音，必须用 https:// 或 localhost 打开

浏览器只在「安全上下文」里开放 `getUserMedia`：协议是 `https://`，或者地址是环回
（`http://localhost`、`http://127.0.0.1`）。用手机访问 `http://192.168.x.x:5000` 这类
局域网地址时 `navigator.mediaDevices` 直接就是 `undefined`——症状正好是「页面都正常，
只有录不了音」，跟移动端适配没关系。三条路子：

```bash
# 1）自签 HTTPS，安卓和 iOS 都能用（自动把本机所有网卡 IP 写进证书 SAN）
venv\Scripts\python.exe scripts\serve_https.py            # 手机访问它打印出来的 https://IP:5000
venv\Scripts\python.exe scripts\serve_https.py --regen    # 换了 WiFi、IP 变了就重签
venv\Scripts\python.exe scripts\serve_https.py --no-run   # 只准备证书，先不启动

# 2）安卓 + USB，连证书都不用：手机那边变成环回地址，浏览器认它是安全上下文
adb reverse tcp:5000 tcp:5000      # 然后在手机 Chrome 里打开 http://localhost:5000

# 3）要给一个班的人用：挂到真有证书的域名下（nginx + Let's Encrypt，或校内服务器）
```

自签证书第一次访问会警告「证书不受信任」，iOS 点「显示详细信息 → 访问此网站」、
安卓 Chrome 点「高级 → 继续前往」就行：安全上下文只看协议是不是 https，不看证书是谁签的，
所以点过警告之后麦克风权限照常弹。签发时三个坑写在 `scripts/serve_https.py` 里：
有效期默认 800 天（iOS 直接拒收超过 825 天的证书）、SAN 里必须列 IP（浏览器早就不看 CN 了）、
中文计算机名不塞进 DNS 项（不合规，严格的解析器可能整张证书都拒）。
还要记得用系统 Safari 或 Chrome 打开，微信 / 钉钉的内置浏览器不放麦克风权限；
Windows 第一次会问是否允许 Python 访问网络，勾「专用网络」，不然手机连不上。

## 音频来源优先级

看读译 / 听读写的标准发音按这个顺序取，第一条有就用：

1. 服务器真人朗读（`words.audio_f` / `audio_m` 随机选一轨，或老数据 `words.audio_path`）
2. 练习页底部「音频导入」面板里自己上传的音频（存在浏览器 IndexedDB，文件名与单词
   同名即自动匹配，如 `abandon.mp3`）
3. 浏览器语音合成兜底

词库里的 5033 个词现在每一条都有真人朗读了，所以正常情况下第 2、3 条轮不到；
本地导入留给「以后新加的词还没配音频」和「想换成自己的录音」这两种场景。

近似音模块的音频来自 `static/audio/vowel/`（单词）和 `static/audio/sentence/`（整句），
词库单词的音频来自 `static/audio/word/`（教材 3A，登记到 `words.audio_path`）和
`static/audio/word3000/`（课堂背诵词男女声各一份，登记到 `words.audio_f` / `audio_m`）。
前三类由 `scripts/import_audio_package.py` 从教材配套录音复制，最后一类单独走两个脚本：

```bash
python scripts/import_audio_package.py --dry-run        # 只体检
python scripts/import_audio_package.py --only vowel     # 3B 单词对立对
python scripts/import_audio_package.py --only word      # 3A 单词朗读
python scripts/import_audio_package.py --only sentence --manifest <转写清单>   # 4A 整句
python scripts/init_db.py                               # 扫描入库
```

```bash
# 课堂背诵词（Quiz3000）：录音包 + ECDICT 词典 → 词表 → 复制音频 → 登记
# 词典从 https://github.com/skywind3000/ECDICT 取 ecdict.csv（约 66 MB，不进版本库）
python scripts/build_quiz3000_words.py --dict <下载目录>/ecdict.csv
python scripts/import_quiz3000.py                       # 只复制词表里有的词形
python scripts/init_db.py                               # 扫描并写 audio_f / audio_m
```

`build_quiz3000_words.py` 用词典做「这是不是一个单词」的判定：录音包文件名里的词形
查不到中文释义就不进词表，`import_quiz3000.py` 也就不会复制它的音频。所以
`400hz-200ms.mp3`、`Reciting*-QuizPractice*.mp3` 这类提示音和 `skil(l)ful`、
`forward(s)` 这种带可省字母括写的文件名都进不了仓库。改完词表要重跑 `init_db.py`，
它会连已入库词条的音标和释义一起刷新（手写的 3A 种子词不受影响）。

4A 的文件名只有「课.组.选项.对立对」，不含整句文本，所以要先用语音转写生成清单
（本机脚本，不进版本库），再由导入脚本落地音频和 `scripts/sentence_texts.json`。
转写会把教材刻意对比的词听成同音词（`pull`→`Paul`、`wooed`→`wood`），因此整句是
按「两个选项只差一个词」重建的：槽位用实际听到的词形，其余位置取四条转写的多数票。

## 已知限制

- 句子练习覆盖 31 组近似音里的 29 组：`burn-barn` 的课次没有配套 4A 录音；
  `fin-thin`（第 30 课）的 4A 录音和同课 3B 单词对不上，为避免把句子挂到错误的
  音对上，整块不导入。选到这组时页面会直接说明，出题接口返回可读的 400。
- Quiz3000 那批词只有单词、音标和中文释义，没有例句；词典里 54 个词形（`air`、
  `their`、`share` 这类）连词典自己都没给音标，这些词只显示音频不显示音标。
- 词库按拼写建行，所以同形异音词是一行一个释义：`abuse`（名词/动词两个录音文件）
  折叠成一条，读哪个都算对；`China`/`china`、`May`/`may`、`March`/`march` 是两道题，
  但 Windows 上一个文件名存不下两份朗读，共用同一个音频、释义把两层意思合在一起。
- iOS Safari 不支持 MediaRecorder 的 MP3 编码，`scripts/common.js` 在
  所有 `MediaRecorder` 类型都不支持时，回退用 Web Audio 自己编码 16-bit PCM WAV。
  这条兜底路径在手机上更依赖「用户手势里建好 `AudioContext`」，所以边播边录的麦克风
  是在点「开始练习」时就 `Recorder.prime()` 起好的；万一还是起不来，本题会跳过录音
  只放音频，答题和判分不受影响，只是少一段回放。
- 边播边录录到的音频里混着标准音：桌面 Chrome / Edge 的回声消除能压掉大半，
  戴耳机最干净，蓝牙外放则几乎一定听得到底音。要只含自己声音的回放就把开关关掉。
- 开发服务器 `debug` 默认关闭（`FLASK_DEBUG=true` 可打开），
  模板改动需要重启进程才会生效。
