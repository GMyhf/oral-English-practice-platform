import os

# 写死的默认密钥等于公开密钥（任何人都能伪造登录 cookie），所以未配置时随机生成。
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(24).hex()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/app.db")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "static/audio")
RECORDING_DIR = os.environ.get("RECORDING_DIR", "data/recordings")





DEFAULT_QUESTION_COUNT = 20
DEFAULT_TIME_LIMIT = 1      # 近似音区分每题默认秒数
MAX_QUESTION_COUNT = 100    # 防止用户输入离谱的值

# 看读译 / 听读写各三段节奏，都是 (下限, 上限, 默认)，单位秒。
# 默认值就是以前写死的那几个数：第一轮 1 秒（看词朗读 / 跟读录音）、
# 第二轮 5 秒（写译文或拼写）、题目之间停一下再进下一题。
# 现在这三段都能在练习设置里自己改，边界只有这一份，前端输入框和 JS 都从这里取。
ROUND1_SECONDS = (0.3, 20, 1)
ROUND2_SECONDS = (1, 60, 5)
ROUND_GAP_SECONDS = (0, 3, 0.2)


DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"