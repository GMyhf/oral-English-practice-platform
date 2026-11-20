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

### 2.3 配置环境变量

```bash
# 复制模板（/etc/english-practice 目录要先建）
sudo mkdir -p /etc/english-practice
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


这个文件里的**任何一项**改完都要 `sudo systemctl restart english-practice` 才生效：
systemd 只在启动服务时读一次 EnvironmentFile，`config.py` 也只在 import 时读一次。
（发码、换码、吊销邀请码不用重启，那些在数据库里，不在这个文件里。）

#### 以服务账号、带上同一份环境变量跑运维脚本

`sudo -u englishapp python ...` **读不到** systemd 的 EnvironmentFile。只要你在 env 里改过
`DATABASE_PATH`（比如挪到 /var/lib），这样跑的 `init_db.py`、`manage_invite.py` 就会去操作默认路径上的
另一个库——而且 `db.connect()` 会静默新建一个空库，码发出去了，线上却注册不了。
`manage_invite.py` 根据 `REGISTER_MODE` 给的提示也会对不上服务的实际值。

所以下文所有运维命令都用这个辅助函数（放进管理员的 `~/.bashrc`）：

```bash
# 用法：ep scripts/init_db.py / ep scripts/manage_invite.py list
ep() {
    sudo -u englishapp bash -c 'set -a; . /etc/english-practice/env; set +a
        cd /opt/english-practice/app && exec /opt/english-practice/venv/bin/python "$@"' _ "$@"
}
```

### 2.4 初始化数据库

```bash
# 以 englishapp 身份建库 + 扫描音频（约 11000 个文件，1-2 分钟）
ep scripts/init_db.py
```

`init_db.py` 会创建数据库（默认 `data/app.db`，SQLite）并扫描 `static/audio/` 下的音频文件登记到数据库。
重复执行是安全的：表用 `CREATE TABLE IF NOT EXISTS` 建，老库缺的列会用 `ALTER TABLE` 补上。

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

# 测试并重载 —— 两个 443 server 块都引用了证书文件，证书没就位时 nginx -t 必然报错。
# 第一次部署先做完第 3 节（至少放好自签证书），再回来跑这两条。
sudo nginx -t
sudo systemctl reload nginx
```

**关键配置**：
- `client_max_body_size 20m`：录音是每题一条、单独 POST 到 `/api/recording` 的，这个值是单条录音的上限，要和 env 里的 `MAX_UPLOAD_MB` 一致；nginx 默认的 1m 会把稍长的录音挡成 413
- `location /static/` 的 `alias` 必须是 `/opt/english-practice/app/static/`（注意有 `app/` 这一层），少一层就全站 404
- `proxy_set_header X-Forwarded-Proto $scheme`：目前应用没装 ProxyFix，Flask 并不读这个头；留着是为了以后要用时不必再改 nginx。麦克风权限只取决于浏览器地址栏是不是 https（或 localhost），和这个头无关

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
# 生成自签证书（有效期 800 天）。SAN 里自动写入 localhost、本机主机名和本机所有网卡的 IPv4；
# 公网 IP 不在本机网卡上（云主机常见）时用 --ip 补上，可以重复。脚本目前不支持写 IPv6 和域名。
cd /opt/english-practice/app
ep scripts/serve_https.py --no-run --ip 10.0.0.1

# 证书在 data/https/ 下，文件名是 dev-cert.pem / dev-key.pem，复制到 nginx 期望的位置
sudo cp data/https/dev-cert.pem /etc/pki/tls/certs/english-practice.crt
sudo cp data/https/dev-key.pem /etc/pki/tls/private/english-practice.key
sudo chmod 600 /etc/pki/tls/private/english-practice.key
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

签发方式决定了能不能自动续期，二选一：

**方式 A：http-01（80 端口能从公网访问时用，推荐）**。nginx 模板的 80 端口 server 块已经留好了
`/.well-known/acme-challenge/` → `/var/www/certbot`，排在 301 跳转之前。

```bash
sudo mkdir -p /var/www/certbot
sudo restorecon -Rv /var/www/certbot        # 仅 SELinux 系统
sudo certbot certonly --webroot -w /var/www/certbot -d example.com \
     --deploy-hook "systemctl reload nginx"
```

`--deploy-hook` 会被记进 `/etc/letsencrypt/renewal/example.com.conf`，以后每次续期成功都自动 reload nginx。
然后打开 certbot 自带的续期定时器（名字因发行版而异），并演练一次：

```bash
sudo systemctl enable --now certbot-renew.timer   # Rocky / CentOS / Fedora
sudo systemctl enable --now certbot.timer         # Ubuntu / Debian（apt 装的通常已默认开启）
sudo certbot renew --dry-run
```

**方式 B：dns-01 手动（服务器没有公网入站时）**

```bash
sudo certbot certonly --manual --preferred-challenges dns -d example.com
# 按提示在 DNS 服务商处添加 TXT 记录（_acme-challenge.example.com），等待生效后继续
```

⚠️ 这样签出来的证书**不会自动续期**：`certbot renew` 在定时器里是非交互运行的，
没法替你去 DNS 服务商那里改 TXT 记录，会直接失败。证书 90 天过期，所以要么每次到期前
（建议第 60 天）手动重跑上面这条命令再 `sudo systemctl reload nginx`，并在日历上设提醒；
要么换成你的 DNS 服务商对应的 certbot DNS 插件（带 API 凭据，能自动改记录）。

签发完成后，证书在 `/etc/letsencrypt/live/example.com/`，nginx 配置里的 `ssl_certificate` 和 `ssl_certificate_key` 指向这里。

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
ep scripts/kdf_bench.py
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

# 先备份（见 5.2），再补表结构：不带参数的 init_db.py 是幂等的，
# 会建出新增的表、给老表补列，并重新登记音频。账号和成绩不动。
sudo /opt/english-practice/app/scripts/backup.sh
ep scripts/init_db.py

# 重启服务
sudo systemctl restart english-practice.service
```

以前这里是一段手写的 `python -c` 迁移代码，它没设 `row_factory`，一跑就报
`TypeError: tuple indices must be integers or slices, not str`，还把库路径写死成 `data/app.db`、
补不出新加的表。直接跑 `init_db.py` 就够了。

**注意**：不要跑 `scripts/init_db.py --reset`，那会删掉所有账号、练习记录和邀请码。

### 5.2 备份数据库

```bash
# 数据库快照 + 录音打包，存到 /var/backups/english-practice（只有 root 可读），保留 14 天
sudo /opt/english-practice/app/scripts/backup.sh

# 存到别处、保留更久
sudo KEEP_DAYS=30 /opt/english-practice/app/scripts/backup.sh /mnt/nas/english-practice
```

`scripts/backup.sh` 做的事：读 `/etc/english-practice/env` 拿到真实的库和录音路径；
以服务账号身份用 SQLite backup API 拍快照（WAL 模式下不能直接 `cp` 主库），拍完跑一次
`integrity_check`；把录音目录打成 tar.gz；删掉超过保留天数的旧快照。任何一步失败都非零退出。

录音每天是整份打包，录音多了以后备份目录会按「录音总量 × 保留天数」增长，留意磁盘。

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
# 发一个不限次数的班用码（随机生成）
ep scripts/manage_invite.py add

# 指定码名，附备注
ep scripts/manage_invite.py add pku2026 --note "群里发"

# 一码一号：只能注册一个账号，发给某一个人
ep scripts/manage_invite.py add --max-uses 1 --note "张三"

# 查看现有的码、各用掉了几个
ep scripts/manage_invite.py list

# 查某个码注册过谁
ep scripts/manage_invite.py users pku2026

# 码泄露了：立刻失效，记录留着
ep scripts/manage_invite.py revoke pku2026
```

发码 / 换码 / 吊销都不用重启服务；只有改 `REGISTER_MODE` 要重启。

### 5.5 清理测试数据

仓库里**暂时没有**批量清理测试号和删除单个账号的脚本（这里以前引用的
`scripts/cleanup_testdata.py`、`scripts/del_user.sh` 并不在仓库里）。
真要删号，先 `sudo /opt/english-practice/app/scripts/backup.sh` 拍快照，再手工处理：
账号在 `users` 表，名下的会话、逐题作答、注册记录分别在 `practice_sessions`、`practice_records`、
`invite_uses`，录音文件在录音目录里以 `u<用户id>_` 开头。

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

**症状**：练习中某一题的录音上传失败，报 413 Request Entity Too Large。

**原因**：录音是每题一条单独上传的。nginx 的 `client_max_body_size` 还是默认的 1m 时，稍长一点的录音就会被挡；
如果 nginx 放行了，应用自己还有一道 `MAX_UPLOAD_MB` 上限。两边返回的 413 页面长得不一样，看响应体能分出是谁挡的。

**修复**：编辑 `/etc/nginx/conf.d/english-practice-app.loc`，确保有 `client_max_body_size 20m;`，然后 `sudo nginx -t && sudo systemctl reload nginx`。
要放宽就和 env 里的 `MAX_UPLOAD_MB` 一起改。

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

**修复**：重跑一遍不带参数的 `init_db.py`，它会补齐缺的表和列（见 5.1）：
```bash
sudo /opt/english-practice/app/scripts/backup.sh
ep scripts/init_db.py
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

部署模板里**没有**这段配置，需要时自己加。两处要改：

```nginx
# 1) /etc/nginx/conf.d/english-practice.conf 的最上面、所有 server 块之外。
#    conf.d/*.conf 是在 http 层被 include 的，所以写在这里就是 http 层。
limit_req_zone $binary_remote_addr zone=register:10m rate=1r/s;
```

```nginx
# 2) /etc/nginx/conf.d/english-practice-app.loc 里，location / 之前。
#    精确匹配的 location 不会继承 location / 里的 proxy_* 设置，要整段重复一遍。
location = /register {
    limit_req zone=register burst=5 nodelay;
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection       "";
    proxy_set_header Host             $host;
    proxy_set_header X-Real-IP        $remote_addr;
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

改完 `sudo nginx -t && sudo systemctl reload nginx`。

### 7.4 定期备份

```bash
# 每天凌晨 3 点备份数据库和录音（脚本说明见 5.2）
echo "0 3 * * * root /opt/english-practice/app/scripts/backup.sh" | sudo tee /etc/cron.d/english-practice-backup

# 装好后先手动跑一次，确认真的产出了文件
sudo /opt/english-practice/app/scripts/backup.sh && sudo ls -l /var/backups/english-practice
```

脚本失败时 cron 会把输出发到 root 的本地邮箱；机器上没配邮件的话，隔段时间看一眼备份目录里的日期。

---

## 8. 两种运行模式（`APP_MODE`）

同一个代码库支持两种跑法，由 `/etc/english-practice/env` 里的 `APP_MODE` 决定，
改完必须 `sudo systemctl restart english-practice` 才生效（这个值在进程启动时读一次，
和 `REGISTER_MODE` 一样）。

| | `server`（默认） | `local` |
|---|---|---|
| 登录 | 要，注册还要邀请码 | 不要，`/login` `/register` 只会给一张说明页 |
| 成绩（会话 + 逐题） | `data/app.db` | 这台设备浏览器的 IndexedDB（库名 `ep_local_db`） |
| 朗读录音 | `data/recordings/` | 同一个 IndexedDB 里的 Blob |
| 设置（题量、三段时长） | `users` 表 | 这台设备的 localStorage（键 `ep_settings`） |
| 出题数据（词库、近似音题库） | 从库里读 | **同样从库里读**，只是不写 |
| 老师的可见范围 | 全班进度 | 什么都看不到 |

### 8.1 切换

```bash
sudo vi /etc/english-practice/env          # APP_MODE=local
sudo systemctl restart english-practice
```

两种模式下 `scripts/init_db.py` 都不能省：`local` 不写库，但词库和近似音题库是出题的数据源，
库里没有词就直接抽不到题。数据库、`data/recordings/`、nginx 的静态直出配置都不用动 ——
`local` 模式只是不再往它们里写。

值写错（`LOCAL`、`LocalMode`、多一个空格）不会把登录体系悄悄关掉：
会退回 `server` 并往 journald 里打一行中文警告。

```bash
sudo journalctl -u english-practice -b --no-pager | grep APP_MODE
```

### 8.2 切完怎么确认（在服务器本机上也验得出）

```bash
# 页面外壳里带着模式标记（base.html 的 <body data-mode="...">）；local 下不该出现 /login /register 的链接
curl -s http://127.0.0.1:8000/ | grep -o 'data-mode="[a-z]*"'
# /api/me 会如实报告模式
curl -s http://127.0.0.1:8000/api/me
# local 模式下三个写接口一律 409，而且不会落库
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/practice/session \
     -H 'Content-Type: application/json' -d '{"practice_type":"vowel","params":{}}'
```

看到 `409` 才算切到位。这两个性质有回归测试兜着：`tests/test_app_modes.py`
（含「用别人的服务器会话号在本地模式机器上读不出任何内容」这条 —— 本地模式的结果页
是个空壳，后端一行库都不查）。

浏览器那一半（IndexedDB 真的存下了、录音真的能回放）只能在设备上验，最短路径走一遍：

1. 打开首页，右上角是「本地模式 · 只存这台设备」，点任意练习不需要登录。
2. **看读译**做 2 题、每题都录音 → 结束后的结果页能逐题听到自己的朗读。
   （近似音只练听辨、不录音，要验录音得选看读译或听读写。）
   这一条同时盯住一个很容易复发的坑：录音明明存进了 IndexedDB，结果页却说「没有录到音」——
   那是 `records.recording_path` 少了 `local:` 前缀，读的一侧（`scripts/local-result.js`）
   全靠这个前缀认路，见 `scripts/local-store.js` 的 `saveRecords`。
3. 「我的数据」显示 1 场 / 2 条作答、录音 2 条；点「导出（含录音）」拿到的文件不该只有几百字节，
   页面上那句「N 条录音已含在文件里」得真的对得上（这一场没录音时不该冒出「0 条录音」）。
4. 回到服务器上复查「真的一个字都没写」（在 `/opt/english-practice/app` 下，用服务账号）：

```bash
sudo -u englishapp /opt/english-practice/venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/app.db')
print('sessions', conn.execute('SELECT count(*) FROM practice_sessions').fetchone()[0],
      'records',   conn.execute('SELECT count(*) FROM practice_records').fetchone()[0])
"
ls data/recordings/ | wc -l
```

练完一轮前后这两个数都不该变。变了就说明这台机器其实还在 `server` 模式（检查 `APP_MODE` 有没有拼对、
服务有没有重启）。

### 8.3 备份责任从服务器转到设备

`local` 模式把「数据只在服务器上」这个前提换掉了，备份这件事随之变成设备使用者的责任：

- **浏览器随时可能清掉这些记录**：用户清除浏览数据、换浏览器、换电脑、用无痕窗口，
  记录就没了，服务器上没有第二份可以找回。
- 「我的数据」页（`/account`）上有三件事可以做：
  1. **申请持久化存储** —— 调 `navigator.storage.persist()`，让浏览器在磁盘紧张时不自动驱逐。
     多数桌面浏览器有交互就批，手机 Chrome 会弹权限，**被拒不算故障**，页面会转而提醒导出。
  2. **导出（含录音）/ 只导出记录** —— 单个 JSON 文件，录音以 base64 内嵌。
     录音合计超过 40MB 时自动降级为「只导记录」，并在页面上说明为什么。
  3. **导入备份文件** —— 是**追加**不是覆盖：每一场都拿本机新的自增编号落库，
     原有的记录一律不动，导入后能在列表里看到来源痕迹（`imported_from`）。
- 机房公用机的常规做法：一学期开学导一次，换机时在新机器上导入。

### 8.4 从 `local` 切回 `server` 时

设备上 IndexedDB 里的记录**不会自动上传**，也不会被删。切回之后「我的数据」页不再出现，
那些记录在这个模式下看不见也导不出来。所以：

1. 想让这些成绩进服务器，就在还是 `local` 的时候导出文件，再在 `server` 模式下
   由老师决定是否人工登记 —— 目前没有「把备份灌进 `practice_sessions`」的工具，
   因为备份里没有用户身份，硬塞只会污染班级数据。
2. 还想让使用者自己看到它们，就别切回去：`local` 模式下随时可以再导出。

---

## 9. 参考

- 本地开发：`README.md`
- 设计文档：`docs/design.md`
- 运行模式的实现：`config.py` 的 `APP_MODE`、`app.py` 的 `is_local()`、
  `scripts/practice-store.js`（分叉点）、`scripts/local-store.js`（IndexedDB）
- 生产配置实例：`/etc/english-practice/`（主机上）
- nginx 配置实例：`/etc/nginx/conf.d/english-practice.conf`（主机上）
- systemd 单元实例：`/etc/systemd/system/english-practice.service`（主机上）
