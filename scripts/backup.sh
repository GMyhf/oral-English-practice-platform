#!/usr/bin/env bash
# scripts/backup.sh —— 每天给数据库和朗读录音拍一份快照，旧快照按天数清掉。
#
# 用法（root 跑，cron 见 docs/deployment.md 7.4）：
#   sudo /opt/english-practice/app/scripts/backup.sh                 # 备份到 /var/backups/english-practice
#   sudo /opt/english-practice/app/scripts/backup.sh /mnt/nas/ep     # 换个目录
#
# 可以用环境变量覆盖的路径（默认值就是 docs/deployment.md 里的部署布局）：
#   APP_DIR   代码目录            /opt/english-practice/app
#   PY        虚拟环境里的 python /opt/english-practice/venv/bin/python
#   ENV_FILE  服务的环境变量文件  /etc/english-practice/env
#   KEEP_DAYS 快照保留天数        14
#
# 为什么这么写：
# - 数据库用 SQLite 的 backup API 拷，不用 cp：WAL 模式下还没合并的事务在 app.db-wal 里，
#   cp 主库会漏掉它们，甚至拷到写了一半的页。拷完再跑一次 integrity_check，坏的快照当场报错。
# - 先读 ENV_FILE：服务的 DATABASE_PATH / RECORDING_DIR 可能改过（挪到 /var/lib 之类），
#   不读的话这个脚本会去备份一个根本不在用的默认路径，而且还「成功」。
# - 快照里有密码哈希和学生的录音，备份目录只给 root 读（umask 077 + chmod 700）。
# - 任何一步失败都非零退出（set -e），cron 会把输出发给 root，不会静默地「以为有备份」。
set -euo pipefail
umask 077

APP_DIR=${APP_DIR:-/opt/english-practice/app}
PY=${PY:-/opt/english-practice/venv/bin/python}
ENV_FILE=${ENV_FILE:-/etc/english-practice/env}
KEEP_DAYS=${KEEP_DAYS:-14}
DEST=${1:-/var/backups/english-practice}

if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

mkdir -p "$DEST"
chmod 700 "$DEST"
stamp=$(date +%Y%m%d-%H%M%S)
db_snapshot="$DEST/app-$stamp.db"

cd "$APP_DIR"

# 路径解析交给 db.py（和应用用的是同一份逻辑），这里不再写第二遍
db_path=$("$PY" -c 'import os, sys; sys.path.insert(0, os.getcwd()); import db; print(db.DB_PATH)')
if [ ! -f "$db_path" ]; then
    echo "找不到数据库 $db_path，检查 DATABASE_PATH 或 APP_DIR" >&2
    exit 1
fi

# 以库文件的属主（服务账号 englishapp）身份去读库，而不是 root：
# WAL 模式下打开连接可能新建 app.db-wal / app.db-shm，root 建出来的这两个文件
# 服务账号写不了，恰好赶上服务启动就是一串 "attempt to write a readonly database"。
# 快照先落在属主可写的临时目录，再由 root 挪进只有 root 能读的备份目录。
owner=$(stat -c %U "$db_path")
run_as=()
if [ "$(id -u)" = 0 ] && [ "$owner" != root ]; then
    run_as=(runuser -u "$owner" --)
fi
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
chown "$owner" "$tmpdir" 2>/dev/null || true

${run_as[@]+"${run_as[@]}"} "$PY" - "$db_path" "$tmpdir/app.db" <<'PYEOF'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
    status = dst.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    src.close()
    dst.close()
if status != "ok":
    sys.exit("快照完整性检查没过：%s" % status)
PYEOF
mv "$tmpdir/app.db" "$db_snapshot"
chown "$(id -u):$(id -g)" "$db_snapshot"
echo "数据库 $db_path → $db_snapshot"

rec_dir=${RECORDING_DIR:-data/recordings}
case "$rec_dir" in
    /*) ;;
    *) rec_dir="$APP_DIR/$rec_dir" ;;
esac
if [ -d "$rec_dir" ]; then
    tar -czf "$DEST/recordings-$stamp.tar.gz" -C "$(dirname "$rec_dir")" "$(basename "$rec_dir")"
    echo "录音 $rec_dir → $DEST/recordings-$stamp.tar.gz"
else
    echo "录音目录 $rec_dir 不存在（还没人上传过录音），跳过"
fi

find "$DEST" -maxdepth 1 -type f \( -name 'app-*.db' -o -name 'recordings-*.tar.gz' \) \
     -mtime +"$KEEP_DAYS" -print -delete
