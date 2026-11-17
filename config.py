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