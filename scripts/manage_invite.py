# scripts/manage_invite.py
"""邀请码管理：发放、查看、吊销，以及「谁用哪个码注册的」追溯。

用法（项目根目录）：
    python scripts/manage_invite.py list                 # 当前能用的码
    python scripts/manage_invite.py list --all           # 连已吊销的一起看
    python scripts/manage_invite.py add                  # 生成一个随机码并打印
    python scripts/manage_invite.py add pku2026 --note "发在课程群里"
    python scripts/manage_invite.py add --max-uses 1     # 一次性码（一码一号）
    python scripts/manage_invite.py revoke pku2026       # 软吊销：码立刻失效，使用记录留着
    python scripts/manage_invite.py users pku2026        # 这个码注册过谁
    python scripts/manage_invite.py purge pku2026        # 确认不用追溯了再硬删

线上（部署机）要以服务账号跑，否则写不进 /opt/english-practice/app/data/app.db：
    sudo -u englishapp /opt/english-practice/venv/bin/python \
        /opt/english-practice/app/scripts/manage_invite.py add

增删换码**不需要重启服务**（每个注册请求现查库）；只有开关 REGISTER_MODE 要重启。
这个脚本第一次跑还会顺手把两张表建出来 —— 已上线的库就靠它补表，
所以「补表 → 发码 → 改开关重启」是这个功能上线的固定顺序。
"""
import argparse
import os
import sqlite3
import sys

# Windows 控制台默认 GBK，和 init_db.py 一样先改掉，否则打印备注里的中文会炸
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config                                          # noqa: E402
from db import resolve_db_path                         # noqa: E402
from services import registration                      # noqa: E402


def _hint_sqlite_error(exc):
    """打不开库最常见的原因就是「用了 rocky 而不是 englishapp」，直接说清楚。"""
    sys.exit(
        "数据库操作失败：%s\n"
        "  路径：%s\n"
        "  部署机上要用服务账号跑（rocky 对它没写权限）：\n"
        "    sudo -u englishapp /opt/english-practice/venv/bin/python "
        "/opt/english-practice/app/scripts/manage_invite.py ..." % (exc, resolve_db_path()))


def _mode_warning():
    """把「码发了但闸门没开」这种配置不一致当面说出来，别等上线当天才发现。"""
    mode = registration.mode()
    if mode == "open":
        print("\n!! 注意：REGISTER_MODE=open，现在谁都能注册，这些码根本不会被检查。"
              "要让闸门生效：/etc/english-practice/env 里写 REGISTER_MODE=invite，再重启服务。")
    elif mode == "closed":
        print("\n!! 注意：REGISTER_MODE=closed，注册口整个关着，码先发着也注册不上。"
              "开放注册时改成 invite 并重启服务。")


def _row_line(row, width=20):
    if row["revoked_at"]:
        status = "已吊销"
    elif row["max_uses"] is None:
        status = "不限次数（已用 %d 次）" % row["use_count"]
    else:
        left = row["max_uses"] - row["use_count"]
        status = "已用 %d/%d%s" % (row["use_count"], row["max_uses"],
                                   "，剩 %d" % left if left > 0 else "，用完")
    return "%-*s  %-24s  id=%-4s  %s" % (
        width, row["code"], status, row["id"], row["note"] or "")


def cmd_list(args):
    rows = registration.list_codes(include_revoked=args.all)
    print("数据库：%s    REGISTER_MODE=%s\n" % (resolve_db_path(), registration.mode()))
    if not rows:
        print("库里没有任何邀请码。")
        if registration.mode() == "invite":
            print("这种状态下注册页会显示「老师还没有发放邀请码，注册暂未开放」—— 有意为之，"
                  "宁可关着也不要敞口。要发码：python scripts/manage_invite.py add")
        return
    for row in rows:
        print(_row_line(row))
    _mode_warning()


def cmd_add(args):
    codes = args.code or [None]        # 不给码就是「生成一个」
    for code in codes:
        row = registration.add(code=code, note=args.note, max_uses=args.max_uses,
                               length=args.length)
        limit = "不限次数" if row["max_uses"] is None else "可用 %d 次" % row["max_uses"]
        print("邀请码 %s    %s    备注：%s" % (row["code"], limit, row["note"] or "无"))
    _mode_warning()


def cmd_revoke(args):
    n = registration.revoke(args.code)
    if n:
        print("已吊销 %s。使用记录还在，`users %s` 还能查是谁用它注册过；"
              "确认不要追溯了再 `purge`。" % (args.code, args.code))
    else:
        print("没找到还能用的 %s（可能已经吊销或删掉了）" % args.code)


def cmd_purge(args):
    print("已从库里删除 %s" % args.code if registration.purge(args.code)
          else "库里没有 %s" % args.code)


def cmd_users(args):
    row = registration.get(args.code)
    if not row:
        sys.exit("库里没有这个码：%s" % args.code)
    rows = registration.users_of(args.code)
    print("%s（%s）注册过 %d 个账号：" % (
        row["code"], "已吊销" if row["revoked_at"] else "有效", len(rows)))
    for user in rows:
        print("  id=%-5s %-20s %-20s %s" % (user["id"], user["username"],
                                            user["nickname"] or "", user["created_at"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="注册邀请码管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出现有邀请码")
    p_list.add_argument("--all", action="store_true", help="连已吊销的一起列")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="添加（或随机生成）邀请码")
    p_add.add_argument("code", nargs="*", help="留空则自动生成随机码")
    p_add.add_argument("--note", help="这批码发给谁，纯备注")
    p_add.add_argument("--max-uses", type=int, default=None,
                       help="可用次数；不给就是不限次数，给 1 就是一码一号")
    p_add.add_argument("--length", type=int, default=None,
                       help="随机码长度，默认 config.REGISTER_CODE_LENGTH=%d"
                            % config.REGISTER_CODE_LENGTH)
    p_add.set_defaults(func=cmd_add)

    p_rev = sub.add_parser("revoke", help="吊销一个码（立刻注册不上，记录保留）")
    p_rev.add_argument("code")
    p_rev.set_defaults(func=cmd_revoke)

    p_purge = sub.add_parser("purge", help="从库里彻底删掉一个码（连带使用记录）")
    p_purge.add_argument("code")
    p_purge.set_defaults(func=cmd_purge)

    p_users = sub.add_parser("users", help="查这个码注册过哪些账号")
    p_users.add_argument("code")
    p_users.set_defaults(func=cmd_users)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except sqlite3.Error as exc:
        _hint_sqlite_error(exc)
    except ValueError as exc:            # add 的参数不合法：只说一句，别甩 traceback
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
