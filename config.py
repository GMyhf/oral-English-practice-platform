import os

# 写死的默认密钥等于公开密钥（任何人都能伪造登录 cookie），所以未配置时随机生成。
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(24).hex()

# 路径类配置一律「相对项目根」或「绝对路径」都接受，解析在 db.resolve_db_path()
# 和 app.recording_dir() 里。
#
# 这一段是 2026-11-17 修过来的：以前 DATABASE_PATH 只有这里在读，db.py 和
# scripts/init_db.py 各自把路径写死成 BASE_DIR/data/app.db，改环境变量根本不生效；
# RECORDING_DIR 反过来——真的生效，但填绝对路径会让 audio_url() 的
# 前缀比对失配（比对前先 lstrip("/")），结果页的录音链接变成 /opt/... 的 404。
# 现在两头都归一成「相对项目根」再比对，两种写法都不会出事。
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/app.db")
RECORDING_DIR = os.environ.get("RECORDING_DIR", "data/recordings")

# 原来这里还有一条 AUDIO_DIR，全仓库没有任何使用点：音频路径是相对项目根存在
# 数据库里的（static/audio/vowel/...），实际由 Flask 的 static 路由和 nginx 直出，
# 改这个变量什么都改变不了，留着只会误导，删了。

# ------------------------------------------------------------------ 密码哈希
# 一次哈希就是登录请求的全部 CPU 成本，所以这两个参数直接决定这台机能扛住
# 多少人「同一秒钟点登录」。2026-11-17 在部署机（1 核 / 764MB / Werkzeug 3.1.9）上
# 逐个量的（每个方法单开一个子进程测，内存那栏是 ru_maxrss 增量 —— 同进程里连着测
# 第二个方法的增量恒为 0，因为 ru_maxrss 是只涨不跌的高水位，那串 0 是假的）：
#
#   scrypt:32768:8:1（werkzeug 默认，之前就在用）  校验 176ms  峰值内存 +34MB   5.7 次/s
#   scrypt:16384:8:1（现在用的）                    校验  72ms  峰值内存 +18MB  13.8 次/s
#   scrypt:8192:8:1 （再降一档，没采用）             校验  36ms  峰值内存 +10MB  27.7 次/s
#   pbkdf2:sha256:600000（werkzeug 默认迭代数）      校验 437ms  峰值内存  ~2MB   2.3 次/s
#   pbkdf2:sha256:120000                            校验  88ms  峰值内存  ~2MB  11.3 次/s
#
# 换机器记得重测：容量上限基本就是 1000/校验毫秒数，这个数跟 CPU 强相关。
#
# 选 scrypt:16384:8:1：吞吐 5.7 → 13.8 次/s（端到端实测 5.2 → 12.3 次/s，已吃到上限的
# 89%），内存减半，同时保住「内存困难」这个性质。
# 网上常见的「换成 pbkdf2 就好」在这台机上是反效果，两条都实测过：
#   1) OWASP 给 pbkdf2-HMAC-SHA256 的建议就是 600000 轮，这里要 437ms —— 比原来的
#      scrypt 还慢一倍；
#   2) 砍到 120000 轮才勉强打平 16384 的速度，但它只占 2MB —— 省内存对防守方是好事，
#      对拿 GPU 批量爆破的人更是好事，等于白送强度。
#
# 把代价说清楚，别把这条当无害优化：OWASP Password Storage Cheat Sheet 给的 scrypt
# **下限是 N=2^17、r=8、p=1（约 128MB）**。也就是说 werkzeug 默认的 2^15 本来就已经低于
# 建议值，我们降到 2^14 是低于建议三档。之所以仍然这么选：
#   - 2^17 在这台 764MB 的机器上不可行（cgroup 只留 384MB，4 路并行就要 512MB）；
#   - 站里是校内英语练习账号，没有支付、没有隐私数据，这里的密码不复用于任何别处；
#   - 补偿措施在 services/auth.py：查无此人也会烧一次等价的假哈希，且哈希并发有上限。
# 以后换到正经机器、或者上了域名想调回 32768 起步，只改这里（或环境变量）就行：
# 老用户的哈希照样能验（check_password_hash 按哈希串里写的方法名走），并且登录时会
# 顺手按当前策略改写 —— 不需要强制任何人重置密码，见 services/auth.py。
PASSWORD_HASH_METHOD = os.environ.get("PASSWORD_HASH_METHOD", "scrypt:16384:8:1")

# 同时算密码的并发上限（每个 worker 进程各限各的，跨进程不共享）。
# 这条不是吞吐优化，纯粹是内存保险：整机并行数 = workers × 本值 = 2×2 = 4，
# 峰值 4×18MB ≈ 72MB —— 是 384MB 闸门里最贵的一块，但至少是可控的。
# 不限的话就是 8 条处理线一起 scrypt ≈ 256MB，2026-11-17 之前那次
# 「12 人同时登录把整机拖进直接回收死循环」正是那个形状。
# 单核本来也是串行，所以限流几乎不亏吞吐：轻负载下最多排 1 轮 ≈ 72ms。
MAX_CONCURRENT_HASHES = int(os.environ.get("MAX_CONCURRENT_HASHES", "2"))

# 等不到哈希闸门的耐心值（秒）。这个数是照着最坏场景算出来的，不是随手写的：
# 240 人同一秒点登录时整池排队 ≈ 240 / 13.8 ≈ 17 秒，实测最慢那一个 19.4 秒，
# 刚好走不出这条线。再大的洪峰就会开始有人收到 503、过几秒重试，
# 而不是让请求一直挂着，直到 nginx 60 秒超时吐一个 504。
HASH_QUEUE_TIMEOUT = float(os.environ.get("HASH_QUEUE_TIMEOUT", "20"))

# 录音上传体积上限（MB）。以前只有 nginx 的 client_max_body_size 兜着，
# 应用自己不限 —— 换台前面没有 nginx 的机器就是裸奔。
MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "20"))

# ------------------------------------------------------------------ 运行模式
# 这台机是校内云主机，两种用法差别很大，用一个开关切：
#
#   server（默认）—— 要登录，练习记录和录音都进服务器的 data/app.db 与 data/recordings/。
#                    老师能看到全班的进度，换台设备接着练也在。这是现在线上跑的那套。
#   local         —— 不登录（也没有注册口），练习记录和录音全部留在**这台设备浏览器**的
#                    IndexedDB 里，服务器一个字都不写。
#
# local 模式不是为了省流量，是为了这两种场景：
#   1) 机房/教室共用一台机器，不想给每个人建账号；
#   2) 服务器还没部署好、或者入站没放行（见 docs/deployment.md），先用一台能跑的机器把课开了。
#
# 切到 local 时服务器侧仍然要数据库 —— 词库和近似音题库（words / vowel_items / sentence_items）
# 是出题的数据源，只读不写。所以 scripts/init_db.py 这一步在两种模式下都不能省。
#
# 默认取 server 是有意的：写错值（LOCAL / LocalMode / 多打个空格）就退回「要登录」那一档，
# 宁可让老师发现「怎么还要登录」，也不要本来的班级账号体系被一个拼写错误悄悄关掉。
# 这个值和 REGISTER_MODE 一样是 import 时读一次，改完要重启服务才生效。
_APP_MODE_RAW = os.environ.get("APP_MODE", "server").strip().lower()
APP_MODE = _APP_MODE_RAW if _APP_MODE_RAW in ("server", "local") else "server"
if _APP_MODE_RAW != APP_MODE:
    # 走 stderr 而不是 logger：config 被 import 的时候 Flask 的 logger 还没配好，
    # 而这条偏偏是「你以为是本地模式其实不是」这种一定要看见的信息。
    import sys
    print(
        f"[config] APP_MODE={_APP_MODE_RAW!r} 不是 server/local 之一，已退回 server（要登录）。",
        file=sys.stderr,
    )


# ------------------------------------------------------------------ 注册闸门
#
# 这门课的站只要挂在能被扫到的地址上，开放注册就等于请扫描器进来：机器人在几天内
# 会把注册页试一遍，注册进来的假账号既占库又会产生录音文件（磁盘 33G 空闲）。
# 所以注册改成要邀请码，码存在数据库里（见 services/registration.py），
# 增删换码都不用重启服务。
#
#   open     —— 谁都能注册。只适合本地开发。
#   invite   —— 必须填对邀请码（**默认值，也是部署该用的值**）。
#   closed   —— 彻底停止注册，连码都不收。第一周开课后用它。
#
# 默认取 invite 是有意的：宁可「忘了配码 → 没人注册得上」，
# 也不要「忘了配 → 全班匿名注册口对公网敞开」。本地开发要放开就显式设
# FLASK 环境里的 REGISTER_MODE=open，或者跑一句 scripts/manage_invite.py add 造个码。
# 注意这个值在 import 时读一次，改完要重启服务才生效（码本身不用）。
REGISTER_MODE = os.environ.get("REGISTER_MODE", "invite").strip().lower()

# 邀请码的尝试结果不在这里限流：挡爆破靠的是码的熵（manage_invite.py 不给参数时
# 从 31 个易辨认字符里随机取 12 位，约 59 bit），而不是靠计数器。进程内计数器在多
# worker 下各记各的，还会被人故意触发把全班锁在门外。真挂公网要在 nginx 那一层
# 给 /register 加 limit_req，见 docs/deployment.md 7.3。
REGISTER_CODE_LENGTH = int(os.environ.get("REGISTER_CODE_LENGTH", "12"))

DEFAULT_QUESTION_COUNT = 20
DEFAULT_TIME_LIMIT = 1      # 近似音区分每题默认秒数
MAX_QUESTION_COUNT = 100    # 防止用户输入离谱的值

# 看读译 / 听读写各三段节奏，都是 (下限, 上限, 默认)，单位秒。
# 第二轮 5 秒、题间停顿 0.2 秒是以前就写死的那两个数；第一轮（录音那一段）
# 2026-11-17 从 1 秒调到 1.5 秒 —— 一个单词念 1 秒普遍偏赶，尤其听读写是
# 「音频放完之后再走满第一轮」，那 1 秒只是音频之后的尾巴。
# 这三段现在都能存进账号（设置页 → 看读译 / 听读写），(下限, 上限) 仍然是唯一一份边界：
# 练习页和设置页的输入框、services/auth.update_settings 的夹取、tworounds.js 的
# 「输入框不在时的兜底值」都以这里为准。
ROUND1_SECONDS = (0.3, 20, 1.5)
ROUND2_SECONDS = (1, 60, 5)
ROUND_GAP_SECONDS = (0, 3, 0.2)

# 三段节奏 → users 表里那一列。app.rhythm_defaults() 与 auth.update_settings()
# 共用这张表，免得「设置页能填的字段」和「练习页读的字段」两处各写一遍、日后走偏。
# 键名跟着模板里的 rhythm.round1 / round2 / gap 走。
RHYTHM_FIELDS = {
    "round1": ("round1_seconds", ROUND1_SECONDS),
    "round2": ("round2_seconds", ROUND2_SECONDS),
    "gap": ("round_gap_seconds", ROUND_GAP_SECONDS),
}


DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"