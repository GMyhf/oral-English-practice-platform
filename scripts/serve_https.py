# scripts/serve_https.py
"""用 HTTPS 起练习平台，让手机浏览器允许录音。

用法（在项目根目录）：
    venv\\Scripts\\python.exe scripts\\serve_https.py                 # 默认 0.0.0.0:5000
    venv\\Scripts\\python.exe scripts\\serve_https.py --port 8000
    venv\\Scripts\\python.exe scripts\\serve_https.py --regen         # 换了网络 / 证书过期时强制重签
    venv\\Scripts\\python.exe scripts\\serve_https.py --no-run        # 只生成证书，不启动服务
    venv\\Scripts\\python.exe scripts\\serve_https.py --ip 10.0.0.9   # 再往证书里塞一个地址

为什么需要这一步：浏览器只在「安全上下文」里开放 getUserMedia，也就是 https://
或 http://localhost。学生用手机访问 http://192.168.x.x:5000 这种局域网 IP 地址时，
navigator.mediaDevices 直接就是 undefined，麦克风根本申请不到——页面照常显示、
答题照常判分，只有录音失灵。手机既不是 localhost 又没有 https，所以必须给开发服务
套一层 TLS。协议是 https 就够了，浏览器判断安全上下文只看协议，不检查证书可信度，
所以自签证书警告点「继续访问」之后录音就能用。

证书要求（不符合的话 iOS 会直接拒绝连接，而不是只弹警告）：
  - SAN 里必须写 IP 地址（iOS/Chrome 早就不看 CN 了），本机所有网卡地址都放进去；
  - 有效期不超过 825 天（iOS 的硬上限），这里默认 800；
  - SHA-256 签名 + RSA 2048，extendedKeyUsage 带 serverAuth。

证书、私钥和临时 SECRET_KEY 都落在 data/https/（已加进 .gitignore，不进版本库）。
"""
import argparse
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERT_DIR = os.path.join(BASE_DIR, "data", "https")
KEY_PATH = os.path.join(CERT_DIR, "dev-key.pem")
CRT_PATH = os.path.join(CERT_DIR, "dev-cert.pem")
CNF_PATH = os.path.join(CERT_DIR, "san.cnf")
SECRET_PATH = os.path.join(CERT_DIR, "dev-secret.txt")

# 手机访问时用的是 IP，所以 SAN 里 IP 必须齐；host 名一起写上是给以后换域名的时候省事
IOS_MAX_VALID_DAYS = 825
RENEW_AHEAD_DAYS = 30          # 还剩一个月就主动重签，免得练到一半突然失效

OPENSSL_FALLBACKS = [
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\msys64\ucrt64\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
]


def die(msg):
    print(f"[出错] {msg}")
    sys.exit(1)


def find_openssl():
    """openssl 一般在 Git for Windows 的 PATH 里；不在的话去几个常见安装位置找。"""
    exe = shutil.which("openssl")
    if exe:
        return exe
    for path in OPENSSL_FALLBACKS:
        if os.path.exists(path):
            return path
    return None


def openssl_or_die():
    exe = find_openssl()
    if not exe:
        die("找不到 openssl 可执行文件。装了 Git for Windows 就自带，"
            "或者把 openssl 加进 PATH 后再跑；也可以手动生成证书放到 data/https/dev-cert.pem / dev-key.pem")
    return exe


def local_ips(extra=None):
    """这台机器上所有能被子网内设备访问到的 IPv4，127.0.0.1 也一并留着。"""
    ips = {"127.0.0.1"}
    # 默认出口那一块网卡（连不上外网时会抛异常，忽略就好）
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        ips.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    # 其余网卡：按主机名解析一遍，能拿到多块网卡的地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    for value in extra or []:
        try:
            ips.add(str(ipaddress.ip_address(value)))
        except ValueError:
            die(f"--ip {value} 不是合法的 IPv4 地址")
    return sorted(ips, key=lambda s: [int(p) for p in s.split(".")])


def readable_ips(ips):
    """手机真能用的地址：排除回环和 169.254 开头的自动配置地址（那种是连不上网的）。"""
    out = []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or ip.startswith("169.254."):
            continue
        out.append(ip)
    return out


def cert_status():
    """读现有证书：还有多少天到期、SAN 里覆盖了哪些 IP。证书不存在就返回 None。"""
    if not (os.path.exists(CRT_PATH) and os.path.exists(KEY_PATH)):
        return None
    openssl = find_openssl()
    if not openssl:
        return None
    try:
        text = subprocess.run(
            [openssl, "x509", "-in", CRT_PATH, "-noout", "-enddate", "-ext", "subjectAltName"],
            capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    m = re.search(r"notAfter=(.+)", text)
    if not m:
        return None
    # openssl 的日期格式带时区，形如 Nov 16 12:00:00 2027 GMT
    try:
        end = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    days_left = (end - datetime.now(timezone.utc)).days
    # -ext 输出里是「IP Address:10.x.x.x」，手敲 -text 时也有人写成 IP:，两种都认
    hosts = set(re.findall(r"IP(?: Address)?:\s*([0-9.]+)", text))
    return {"days_left": days_left, "ips": hosts}


def make_cnf(ips, hostname):
    # 主机名只有在是合法 ASCII 域名的时候才写进 SAN：
    # 中文计算机名（「奇异果的pc」这种）塞进 DNS 项属于不合规写法，
    # 桌面浏览器不在意，iOS 的严格解析器有可能因此拒绝整张证书。
    safe_host = ""
    if hostname.isascii() and re.fullmatch(r"[A-Za-z0-9._-]{1,253}", hostname or ""):
        safe_host = hostname
    lines = [
        "[req]",
        "distinguished_name = dn",
        "x509_extensions = v3_ca",
        "prompt = no",
        "",
        "[dn]",
        "CN = practice-dev",
        "",
        "[v3_ca]",
        "basicConstraints = critical, CA:TRUE",
        "keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign",
        "extendedKeyUsage = serverAuth",
        "subjectAltName = @alt_names",
        "",
        "[alt_names]",
        "DNS.1 = localhost",
    ]
    if safe_host:
        lines.append(f"DNS.2 = {safe_host}")
    for i, ip in enumerate(ips, start=1):
        lines.append(f"IP.{i} = {ip}")
    with open(CNF_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def ensure_cert(ips, days, force=False):
    hostname = socket.gethostname()
    os.makedirs(CERT_DIR, exist_ok=True)
    status = None if force else cert_status()
    if status:
        missing = [ip for ip in ips if ip not in status["ips"]]
        if status["days_left"] < RENEW_AHEAD_DAYS:
            reason = f"证书只剩 {status['days_left']} 天有效期"
        elif missing:
            reason = "当前地址不在证书里：" + "、".join(missing) + "（换网络后 IP 会变，得重签）"
        else:
            print(f"[证书] 复用 data/https/dev-cert.pem，还剩 {status['days_left']} 天有效，已覆盖全部本机地址")
            return
        print(f"[证书] {reason}，重新签发")

    openssl = openssl_or_die()
    if days > IOS_MAX_VALID_DAYS:
        print(f"[提示] --days {days} 超过 iOS 上限 {IOS_MAX_VALID_DAYS} 天，已按 {IOS_MAX_VALID_DAYS} 天签发")
        days = IOS_MAX_VALID_DAYS
    make_cnf(ips, hostname)
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
        "-days", str(days), "-config", CNF_PATH, "-extensions", "v3_ca",
        "-keyout", KEY_PATH, "-out", CRT_PATH,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        die("生成证书失败：\n" + (res.stderr or res.stdout)[-500:])
    print(f"[证书] 已签发 {CRT_PATH}（{days} 天有效，SAN 含 {len(ips)} 个地址）")


def secret_key():
    """没设 SECRET_KEY 时 Flask 每次重启都会随机换一个，手机登录状态跟着掉。
       这里复用 data/https/dev-secret.txt，只在本地开发用，文件不进版本库。"""
    value = os.environ.get("SECRET_KEY")
    if value:
        return value
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            print("[密钥] 沿用 data/https/dev-secret.txt（重启后手机不用重新登录）")
            return value
    value = secrets.token_hex(32)
    with open(SECRET_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value + "\n")
    print("[密钥] 已生成开发用 SECRET_KEY 存到 data/https/dev-secret.txt（本地开发专用，已 gitignore）")
    return value


def print_handoff(host, port, ips, run_flask):
    usable = readable_ips(ips)
    print("\n===== 手机怎么访问 =====")
    if usable:
        for ip in usable:
            print(f"  https://{ip}:{port}")
    else:
        print("  没找到局域网 IP：检查电脑和手机是否连的是同一个 WiFi / 同一个网段")
    print(f"  本机自测：https://localhost:{port}")
    print("""
首次打开会有「证书不受信任」的警告，这是自签证书的正常表现：
  · iOS Safari：点「显示详细信息」→「访问此网站」，然后在弹窗里确认；
  · 安卓 Chrome：点「高级」→「继续前往」（不安全）。
警告点过之后浏览器就把它当 https 处理，麦克风权限会正常弹窗——
安全上下文只看协议是不是 https，不看证书是谁签的。
（用 Safari / Chrome 打开，微信、钉钉内置浏览器不放麦克风权限。）
""")
    if host in ("0.0.0.0", "::"):
        print("[注意] Windows 防火墙第一次会弹窗问要不要允许 Python 访问网络，勾「专用网络」并允许；"
              "否则手机连不上。已经拒绝过的话去 设置 → Windows 安全中心 → 防火墙和网络保护 里改。")
    print(f"启动命令：{' '.join(run_flask)}\n")


def main():
    parser = argparse.ArgumentParser(description="用 HTTPS 启动练习平台，让手机可以录音")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0 让手机能连进来")
    parser.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    parser.add_argument("--days", type=int, default=800, help="证书有效期天数，默认 800（iOS 上限 825）")
    parser.add_argument("--ip", action="append", default=[], help="额外写进证书的 IP，可重复")
    parser.add_argument("--regen", action="store_true", help="不管现有证书，强制重签")
    parser.add_argument("--no-run", action="store_true", help="只准备证书，不启动 Flask")
    args = parser.parse_args()

    if not os.path.exists(os.path.join(BASE_DIR, "app.py")):
        die("在项目根目录以外跑了这个脚本，找不到 app.py")
    if args.port < 1 or args.port > 65535:
        die(f"--port {args.port} 不在合法范围")

    ips = local_ips(args.ip)
    ensure_cert(ips, args.days, force=args.regen)

    py = sys.executable
    venv_py = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        py = venv_py

    cmd = [py, "-m", "flask", "--app", "app", "run",
           "--host", args.host, "--port", str(args.port),
           "--cert", CRT_PATH, "--key", KEY_PATH, "--no-reload"]
    print_handoff(args.host, args.port, ips, cmd)

    if args.no_run:
        print("[已停] --no-run 只准备了证书，没有启动服务")
        return

    env = dict(os.environ, SECRET_KEY=secret_key())
    print("[启动] Flask HTTPS 开发服务，Ctrl+C 停\n")
    try:
        subprocess.call(cmd, cwd=BASE_DIR, env=env)
    except KeyboardInterrupt:
        print("\n[已停] 收到 Ctrl+C")


if __name__ == "__main__":
    main()
