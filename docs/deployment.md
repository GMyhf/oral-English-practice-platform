# 生产部署指南

本文档说明如何把「英语词汇发音练习平台」部署到生产服务器，供一个班（约 240 人）同时使用。

**本地开发**请看仓库根目录的 `README.md`，那里只讲 `python app.py` 跑起来。

---

## 1. 系统要求

| 资源 | 最低配置 | 推荐配置 | 说明 |
|------|---------|---------|------|
| CPU | 1 核 | 2 核 | 密码哈希是 CPU 密集型，1 核够 240 人用，但登录洪峰时会排队 |
| 内存 | 1 GB | 2 GB | 空闲时整个服务占 58MB，洪峰时（240 人同时登录）涨到 400MB+ |
| 磁盘 | 5 GB | 10 GB | 代码 273MB（音频）+ 数据库 + 用户录音，按每人每课 10MB 估算 |
| 系统 | Rocky 9 / Ubuntu 22.04+ | 同左 | 需要 systemd、nginx 1.20+、Python 3.11+ |
| 网络 | 内网可达 | 公网 IPv4/IPv6 | 学生设备必须能访问服务器 IP；校内用内网 IP 即可，校外需要公网 IP 或 VPN |

**实测数据**（1 核 / 764MB / Rocky 9.8）：
- 240 人各自走完整一轮近似音 20 题 → 0 失败、当天 0 次 5xx
- 做题接口 ~120 req/s
- 静态音频聚合 102MB/s（nginx 直出）
- 登录接口 ~12 次/s（scrypt:16384:8:1，每进程 2 并发，2 个 worker）

---

## 2. 部署步骤

### 2.1 创建服务账号

```bash
# 创建系统用户 englishapp（无登录权限）
sudo useradd -r -s /sbin/nologin -d /opt/english-practice englishapp

# 创建部署目录
sudo mkdir -p /opt/english-practice
sudo chown englishapp:englishapp /opt/english-practice
```

### 2.2 克隆代码并安装依赖

```bash
# 以 englishapp 身份克隆（或先 clone 再 chown）
sudo -u englishapp git clone https://github.com/your-org/oral-English-practice-platform.git /opt/english-practice/app

# 创建虚拟环境
sudo -u englishapp python3 -m venv /opt/english-practice/venv

# 安装依赖
sudo -u englishapp /opt/english-practice/venv/bin/pip install --upgrade pip
sudo -u englishapp /opt/english-practice/venv/bin/pip install -r /opt/english-practice/app/requirements.txt
sudo -u englishapp /opt/english-practice/venv/bin/pip install gunicorn
```

### 2.3 初始化数据库

```bash
# 以 englishapp 身份建库 + 扫描音频（约 11000 个文件，1-2 分钟）
sudo -u englishapp /opt/english-practice/venv/bin/python /opt/english-practice/app/scripts/init_db.py
```

`init_db.py` 会创建 `data/app.db`（SQLite）并扫描 `static/audio/` 下的音频文件登记到数据库。重复执行是安全的（`CREATE TABLE IF NOT EXISTS`）。

### 2.4 配置环境变量

```bash
# 复制模板
sudo cp /opt/english-practice/app/deploy/env.example /etc/english-practice/env

# 生成 SECRET_KEY 并编辑
python3 -c "import os; print(os.urandom(32).hex())"
sudo vim /etc/english-practice/env   # 把 {{GENERATE_A_RANDOM_64_CHAR_HEX_STRING}} 替换成上面那串

# 设置权限（englishapp 可读，其他用户不可读）
sudo chown root:englishapp /etc/english-practice/env
sudo chmod 640 /etc/english-practice/env
```

**关键配置项**：
- `SECRET_KEY`：**必填**，Flask session 签名密钥，必须用真随机值
- `REGISTER_MODE`：`invite`（默认，需邀请码）/ `open`（任何人可注册）/ `closed`（关闭注册）
- `PASSWORD_HASH_METHOD`：`scrypt:16384:8:1`（推荐）或 `scrypt:32768:8:1`（更慢但更安全）
- `MAX_CONCURRENT_HASHES`：每个 worker 进程同时算密码的上限，默认 2

改完 `REGISTER_MODE` 要重启服务才生效；改其他项不用。

### 2.5 配置 gunicorn

```bash
sudo cp /opt/english-practice/app/deploy/gunicorn.conf.py.example /etc/english-practice/gunicorn.conf.py
sudo chown root:englishapp /etc/english-practice/gunicorn.conf.py
sudo chmod 640 /etc/english-practice/gunicorn.conf.py
```

**关键参数**：
- `workers = 2`：1 核机器开 2 个 worker，每多一个 +90MB 常驻内存
- `threads = 4`：每个 worker 4 个线程，2×4 = 8 条并发处理线
- `worker_class = "gthread"`：线程模式，适合本项目 80% 时间在等音频 IO 的负载
- `timeout = 60`：一次提交要写 100 条作答 + 上传录音，30 秒偏紧
- `max_requests = 2000`：跑够请求数就轮换 worker，兜住内存缓慢增长

### 2.6 配置 systemd 单元

```bash
sudo cp /opt/english-practice/app/deploy/english-practice.service.example /etc/systemd/system/english-practice.service
sudo systemctl daemon-reload
sudo systemctl enable --now english-practice.service

# 检查状态
sudo systemctl status english-practice.service
```

**关键配置**：
- `MemoryHigh=384M` / `MemoryMax=512M`：内存闸门，防止洪峰时 OOM 拖垮整机
- `LimitNOFILE=65536`：文件描述符上限，1.1 万个音频文件 + 几百个并发连接会撑爆默认的 1024
- `ProtectSystem=full`：加固，/usr /boot /etc 只读

如果你的机器内存更大（4GB+），可以按比例放宽到 `MemoryHigh=1G` / `MemoryMax=2G`。

### 2.7 配置 nginx

```bash
# 复制模板并替换占位符
sudo cp /opt/english-practice/app/deploy/nginx-english-practice.conf.example /etc/nginx/conf.d/english-practice.conf
sudo cp /opt/english-practice/app/deploy/nginx-english-practice-app.loc.example /etc/nginx/conf.d/english-practice-app.loc

# 替换占位符（假设域名 example.com，IPv4 10.0.0.1，IPv6 2001:db8::1）
sudo sed -i 's/{{DOMAIN}}/example.com/g' /etc/nginx/conf.d/english-practice.conf
sudo sed -i 's/{{IP_V4}}/10.0.0.1/g' /etc/nginx/conf.d/english-practice.conf
sudo sed -i 's/{{IP_V6}}/2001:db8::1/g' /etc/nginx/conf.d/english-practice.conf
sudo sed -i 's/{{DEPLOY_DIR}}/\/opt\/english-practice\/app/g' /etc/nginx/conf.d/english-practice-app.loc

# 测试配置
sudo nginx -t

# 重载
sudo systemctl reload nginx
```

**关键配置**：
- `client_max_body_size 20m`：学生整场练习的录音要一次性打包上传，默认 1m 会 413
- `location /static/` 的 `alias` 必须是 `/opt/english-practice/app/static/`（注意有 `app/` 这一层），少一层就全站 404
- `proxy_set_header X-Forwarded-Proto $scheme`：决定 Flask 看到的协议，影响麦克风权限（必须 https 或 localhost）

### 2.8 配置防火墙

```bash
# Rocky / CentOS / Fedora（firewalld）
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload

# Ubuntu / Debian（ufw）
sudo ufw allow 'Nginx Full'
```

**不要**开放 8000 端口（gunicorn 只监听 127.0.0.1:8000，外部一律走 nginx）。

### 2.9 配置 SELinux（Rocky / CentOS / Fedora）

```bash
# 允许 nginx 连接网络（反向代理需要）
sudo setsebool -P httpd_can_network_connect 1

# 给静态文件目录打标签
sudo semanage fcontext -a -t httpd_sys_content_t "/opt/english-practice/app/static(/.*)?"
sudo restorecon -Rv /opt/english-practice/app/static
```

---

## 3. 证书配置

### 3.1 自签证书（开发 / 内网测试）

```bash
# 生成自签证书（SAN 含 IP + 域名，有效期 800 天）
cd /opt/english-practice/app
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/serve_https.py --no-run

# 证书会放在 data/https/，把它们复制到 nginx 期望的位置
sudo cp data/https/cert.pem /etc/pki/tls/certs/english-practice.crt
sudo cp data/https/key.pem /etc/pki/tls/private/english-practice.key
```

自签证书第一次访问会弹「不受信任」警告：
- iOS：点「显示详细信息 → 访问此网站」
- Android Chrome：点「高级 → 继续前往」
- 桌面浏览器：点「高级 → 继续前往」

**麦克风权限**：浏览器只在「安全上下文」（https 或 localhost）里开放 `getUserMedia`，自签证书点过警告后麦克风照常弹。

### 3.2 Let's Encrypt（生产推荐）

**前提**：你有一个公网可达的域名（A 记录指向服务器 IP）。

```bash
# 安装 certbot
sudo dnf install certbot python3-certbot-nginx   # Rocky / CentOS
sudo apt install certbot python3-certbot-nginx    # Ubuntu / Debian

# 签发证书（dns-01 验证，不需要公网入站）
sudo certbot certonly --manual --preferred-challenges dns -d example.com

# 按提示在 DNS 服务商处添加 TXT 记录（_acme-challenge.example.com），等待生效后继续
```

签发完成后，证书在 `/etc/letsencrypt/live/example.com/`，nginx 配置里的 `ssl_certificate` 和 `ssl_certificate_key` 指向这里。

**自动续期**：
```bash
# 启用 certbot-renew.timer（每天检查一次，到期前 30 天自动续）
sudo systemctl enable --now certbot-renew.timer

# 续期后自动 reload nginx（在 /etc/letsencrypt/renewal/example.com.conf 里加）
# deploy_hook = systemctl reload nginx
```

---

## 4. 内存与并发调优

### 4.1 密码哈希成本

| 方法 | 单次耗时 | 内存峰值 | 吞吐 | 适用场景 |
|------|---------|---------|------|---------|
| scrypt:32768:8:1 | 176ms | 34MB | 5.7 次/s | 高安全需求，机器内存 ≥ 4GB |
| **scrypt:16384:8:1** | 72ms | 18MB | 13.8 次/s | **推荐**，1 核 / 1GB 机器够用 |
| scrypt:8192:8:1 | 36ms | 10MB | 27.7 次/s | 机器资源极紧张，安全折中 |
| pbkdf2:sha256:120000 | 88ms | 2MB | 11.3 次/s | 省内存但抗 GPU 爆破能力弱 |
| pbkdf2:sha256:600000 | 437ms | 2MB | 2.3 次/s | **不推荐**，比 scrypt 还慢 |

**调优方法**：
```bash
# 在目标机器上跑基准测试（每个方法单开子进程，否则 ru_maxrss 只涨不跌会显示假的 0MB）
python3 scripts/kdf_bench.py
```

### 4.2 并发上限

**登录洪峰**（全班同一秒点登录）：
- 整机上限 = `workers × MAX_CONCURRENT_HASHES` = 2 × 2 = 4 个并发哈希
- 240 人排队，每人 72ms，总耗时 ≈ 240 × 72ms / 4 ≈ 4.3 秒
- 但实际还有网络延迟 + 数据库写入，端到端约 17 秒（`HASH_QUEUE_TIMEOUT=20`）

**做题接口**：
- 8 条并发处理线（`workers × threads` = 2 × 4）
- 静态音频由 nginx 直出，不占 gunicorn 线程
- 实测 32 并发 = 11.9 次/s，p95 2.68s，0 失败

**调优建议**：
- 机器内存 ≥ 4GB：`workers = 4`，`threads = 4`，`MemoryHigh=1G`，`MemoryMax=2G`
- 机器内存 1-2GB：保持默认（`workers = 2`，`threads = 4`，`MemoryHigh=384M`，`MemoryMax=512M`）
- 机器内存 < 1GB：`workers = 1`，`threads = 2`，`MemoryHigh=256M`，`MemoryMax=384M`

---

## 5. 维护操作

### 5.1 更新代码

```bash
# 拉取最新代码
cd /opt/english-practice/app
sudo -u englishapp git pull origin main

# 如果表结构有变更（看 git log 里的说明），先补列再重启
sudo -u englishapp /opt/english-practice/venv/bin/python -c "
from scripts.init_db import migrate_users_rhythm_columns, migrate_words_voice_columns
import sqlite3
conn = sqlite3.connect('data/app.db')
migrate_users_rhythm_columns(conn)
migrate_words_voice_columns(conn)
conn.commit()
"

# 重启服务
sudo systemctl restart english-practice.service
```

**注意**：不要跑 `scripts/init_db.py --reset`，那会删掉所有账号和练习记录。

### 5.2 备份数据库

```bash
# 拍快照（WAL 模式下用 sqlite3.backup()，不要用 cp）
sudo -u englishapp /opt/english-practice/venv/bin/python -c "
import sqlite3
src = sqlite3.connect('data/app.db')
dst = sqlite3.connect('data/app.db.bak-$(date +%Y%m%d-%H%M%S)')
with dst:
    src.backup(dst)
src.close()
dst.close()
"

# 备份用户录音
tar -czf /var/backups/english-practice/recordings-$(date +%Y%m%d).tar.gz data/recordings/
```

### 5.3 查看日志

```bash
# 应用日志（gunicorn 输出，由 journald 收）
sudo journalctl -u english-practice.service -f

# nginx 访问日志
sudo tail -f /var/log/nginx/english-practice.access.log

# nginx 错误日志
sudo tail -f /var/log/nginx/english-practice.error.log
```

### 5.4 管理邀请码

```bash
cd /opt/english-practice/app

# 发一个不限次数的班用码
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/manage_invite.py add

# 发一个一码一号的 personalized 码
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/manage_invite.py add pku2026 --note "群里发"

# 查看现有的码、各用掉了几个
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/manage_invite.py list

# 查某个码注册过谁
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/manage_invite.py users pku2026

# 码泄露了：立刻失效，记录留着
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/manage_invite.py revoke pku2026
```

发码 / 换码 / 吊销都不用重启服务；只有改 `REGISTER_MODE` 要重启。

### 5.5 清理测试数据

```bash
# 按命名规则批量清探测号（probe_* / ld* / dbg*）
sudo -u englishapp /opt/english-practice/venv/bin/python scripts/cleanup_testdata.py

# 删单个账号（会先列出名下的会话/作答/录音，再拍库，再停服务删除）
sudo /opt/english-practice/app/scripts/del_user.sh username
```

---

## 6. 故障排查

### 6.1 页面没样式、音频 404

**症状**：页面能打开但没 CSS，练习点播放没声音。

**原因**：nginx 的 `location /static/` 的 `alias` 路径写错了，少了一层 `app/`。

**检查**：
```bash
# 看 nginx 错误日志
sudo tail /var/log/nginx/english-practice.error.log
# 如果看到 open() "/opt/english-practice/static/css/style.css" failed (2: No such file or directory)
# 说明路径错了，应该是 /opt/english-practice/app/static/
```

**修复**：编辑 `/etc/nginx/conf.d/english-practice-app.loc`，把 `alias` 改成 `{{DEPLOY_DIR}}/static/`（即 `/opt/english-practice/app/static/`），然后 `sudo nginx -t && sudo systemctl reload nginx`。

### 6.2 录音上传 413

**症状**：学生提交练习时，长会话的录音上传失败，报 413 Request Entity Too Large。

**原因**：nginx 的 `client_max_body_size` 还是默认的 1m，而学生整场练习的录音可能到 10-20MB。

**修复**：编辑 `/etc/nginx/conf.d/english-practice-app.loc`，确保有 `client_max_body_size 20m;`，然后 `sudo nginx -t && sudo systemctl reload nginx`。

### 6.3 手机录不了音

**症状**：页面都正常，只有录不了音（点录音按钮没反应，或报权限错误）。

**原因**：浏览器只在「安全上下文」（https 或 localhost）里开放 `getUserMedia`。用手机访问 `http://192.168.x.x:5000` 这类局域网地址时 `navigator.mediaDevices` 直接就是 `undefined`。

**修复**：
1. 用 https 访问（自签证书或 Let's Encrypt）
2. 或者用 USB 连接手机，`adb reverse tcp:5000 tcp:5000`，然后手机访问 `http://localhost:5000`

### 6.4 服务起不来，报 MemoryMax

**症状**：`systemctl status english-practice.service` 显示 `status=OOM-killed`。

**原因**：洪峰时（全班同时登录）内存涨过 `MemoryMax`，被 systemd 杀掉。

**修复**：
1. 检查 `MemoryHigh` 和 `MemoryMax` 是否设得太紧（1GB 内存机器建议 384M/512M，4GB 机器可以放宽到 1G/2G）
2. 检查是否有 swap（`free -h` 看 Swap 行），没有的话加一个：
   ```bash
   sudo fallocate -l 2G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
3. 降低密码哈希成本（`PASSWORD_HASH_METHOD=scrypt:8192:8:1`）

### 6.5 登录后报 500

**症状**：登录成功但跳转到 `/practice` 时报 500 Internal Server Error。

**原因**：数据库表结构缺列（老代码升级后没跑迁移）。

**修复**：
```bash
sudo -u englishapp /opt/english-practice/venv/bin/python -c "
from scripts.init_db import migrate_users_rhythm_columns, migrate_words_voice_columns
import sqlite3
conn = sqlite3.connect('data/app.db')
migrate_users_rhythm_columns(conn)
migrate_words_voice_columns(conn)
conn.commit()
"
sudo systemctl restart english-practice.service
```

---

## 7. 安全加固

### 7.1 关闭调试模式

确保 `/etc/english-practice/env` 里 `FLASK_DEBUG=false`。调试模式会暴露堆栈信息，且性能差 10 倍。

### 7.2 限制注册

生产环境建议用邀请码模式（`REGISTER_MODE=invite`），避免被扫描器批量注册。第一周开课之后可以改成 `closed` 完全关闭注册。

### 7.3 nginx 限速

如果要防刷接口（比如暴力尝试邀请码），可以在 nginx 里加限速：

```nginx
# /etc/nginx/conf.d/english-practice.conf 的 http 块里
limit_req_zone $binary_remote_addr zone=register:10m rate=1r/s;

# location /register 里
limit_req zone=register burst=5 nodelay;
```

### 7.4 定期备份

```bash
# 每天凌晨 3 点备份数据库和录音
echo "0 3 * * * root /opt/english-practice/app/scripts/backup.sh" | sudo tee /etc/cron.d/english-practice-backup
```

`backup.sh` 内容参考 5.2 节。

---

## 8. 参考

- 本地开发：`README.md`
- 设计文档：`docs/design.md`
- 生产配置实例：`/etc/english-practice/`（主机上）
- nginx 配置实例：`/etc/nginx/conf.d/english-practice.conf`（主机上）
- systemd 单元实例：`/etc/systemd/system/english-practice.service`（主机上）
