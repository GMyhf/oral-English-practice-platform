"""在目标机器上实测各种密码哈希方法的耗时、内存和吞吐，给 PASSWORD_HASH_METHOD 选值用。

用法（在部署机上、用服务的虚拟环境跑，结果才算数）：
    python scripts/kdf_bench.py                       # 默认那几档，与 docs/deployment.md 4.1 的表对应
    python scripts/kdf_bench.py scrypt:16384:8:1      # 只测指定的方法，可以给多个
    python scripts/kdf_bench.py --rounds 50

测的是「校验一次」的成本（check_password_hash），这正是登录洪峰时每个请求要付的钱；
注册时的生成成本与之相同。

每种方法单开一个子进程：ru_maxrss 是进程生命周期内的峰值，只涨不跌，
放在同一个进程里测，后面几档的内存会被前面那档的峰值盖住，显示成假的 +0MB。
"""

import argparse
import json
import subprocess
import sys

DEFAULT_METHODS = [
    "scrypt:32768:8:1",
    "scrypt:16384:8:1",
    "scrypt:8192:8:1",
    "pbkdf2:sha256:120000",
    "pbkdf2:sha256:600000",
]

# 子进程里跑的代码：先记基线内存，生成一个哈希，再计时校验 N 次
CHILD = r"""
import json, resource, sys, time
from werkzeug.security import check_password_hash, generate_password_hash

method, rounds = sys.argv[1], int(sys.argv[2])
base_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
hashed = generate_password_hash("bench-password-123", method=method)
check_password_hash(hashed, "bench-password-123")       # 预热
start = time.perf_counter()
for _ in range(rounds):
    check_password_hash(hashed, "bench-password-123")
elapsed = time.perf_counter() - start
peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"ms": elapsed / rounds * 1000, "mb": (peak_kb - base_kb) / 1024}))
"""


def bench(method, rounds):
    result = subprocess.run([sys.executable, "-c", CHILD, method, str(rounds)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None, result.stderr.strip().splitlines()[-1] if result.stderr else "失败"
    return json.loads(result.stdout), None


def main():
    if sys.platform == "win32":
        sys.exit("这个脚本用 resource 模块量内存，只能在 Linux / macOS 上跑（部署机上跑才有意义）。")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("methods", nargs="*", help="要测的方法，默认测 %s" % "、".join(DEFAULT_METHODS))
    parser.add_argument("--rounds", type=int, default=20, help="每种方法校验多少次取平均，默认 20")
    args = parser.parse_args()

    print("%-24s %10s %10s %12s" % ("方法", "单次耗时", "内存增量", "单核吞吐"))
    for method in args.methods or DEFAULT_METHODS:
        stats, error = bench(method, args.rounds)
        if error:
            print("%-24s %s" % (method, error))
            continue
        print("%-24s %8.0fms %8.0fMB %9.1f 次/s" % (
            method, stats["ms"], stats["mb"], 1000 / stats["ms"]))
    print("\n整机登录吞吐 ≈ 单核吞吐 × min(CPU 核数, workers × MAX_CONCURRENT_HASHES)；"
          "内存峰值 ≈ 内存增量 × workers × MAX_CONCURRENT_HASHES。")


if __name__ == "__main__":
    main()
