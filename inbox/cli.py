"""命令行入口。"""
from __future__ import annotations

import argparse
import sys

from .engine import Inbox, InboxError


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="inbox", description="准直器送样的检具收件工具")
    parser.add_argument("--origin", default="http://127.0.0.1:8765",
                        help="来源服务地址（默认 http://127.0.0.1:8765）")
    parser.add_argument("--store", default="inbox-state.json",
                        help="质检侧本地状态文件（默认 inbox-state.json）")
    parser.add_argument("command",
                        choices=["sync", "collect", "export", "status", "run"],
                        help="sync 同步来源；collect 配额内取件；export 买方导出；"
                             "status 按交样单查看；run 依次执行 sync+collect+export")
    args = parser.parse_args(argv)

    inbox = Inbox(args.origin, args.store)
    try:
        if args.command == "sync":
            inbox.sync()
            print("已同步来源快照。")
        elif args.command == "collect":
            inbox.collect()
            print("取件完成。")
        elif args.command == "export":
            inbox.export()
            print("导出完成。")
        elif args.command == "status":
            print(inbox.status())
        else:
            print(inbox.run())
    except InboxError as exc:
        print(f"收件工具：{exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
