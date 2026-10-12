#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隔离区管理 —— 查看 / 还原 / 彻底清除。

配合 `wb_clean.py --quarantine <DIR> --apply` 使用：
清理时不直接删，而是 move 进隔离区并记录原始路径，之后可精确还原。

用法:
    python wb_restore.py list                          # 列出隔离区内容
    python wb_restore.py restore --id <ID>             # 还原指定条目（可重复 --id）
    python wb_restore.py restore --all                 # 全部还原
    python wb_restore.py restore --all --on-conflict skip   # 目标已存在时跳过而非改名
    python wb_restore.py purge --older-than 30 --yes   # 彻底删除 30 天前的条目

默认隔离区位置：~/.workbuddy/.wb-quarantine
纯标准库，Python 3.8+。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_common import Quarantine, human   # noqa: E402

DEFAULT_Q = os.path.expanduser("~/.workbuddy/.wb-quarantine")


def cmd_list(q, args):
    entries = q.entries()
    if not entries:
        print(f"隔离区为空: {q.root}")
        return 0
    print("=" * 78)
    print(f"隔离区  {q.root}")
    print(f"共 {len(entries)} 项，占用 {human(q.total_size())}")
    print("=" * 78)
    for e in sorted(entries, key=lambda x: x.get("at", ""), reverse=True):
        print(f"\n  ID   {e['id']}")
        print(f"  原址 {e['origPath']}")
        print(f"  体积 {human(e.get('size', 0))}   删除于 {e.get('at', '—')}"
              f"   类别 {e.get('tag', '—')}" + (f"   ({e['note']})" if e.get("note") else ""))
        p = os.path.join(q.items_dir, e["id"])
        if not os.path.exists(p):
            print("  [!] 隔离区内容已不存在，该条目可清理")
    print("\n还原: python wb_restore.py restore --id <ID>")
    return 0


def cmd_restore(q, args):
    entries = q.entries()
    if not entries:
        print("隔离区为空，无需还原。")
        return 0
    ids = list(args.id)
    if not args.all and not ids:
        print("[x] 请指定 --id（可重复）或 --all。用 list 查看现有条目。")
        return 2
    target = None if args.all else ids
    if args.all:
        print(f"将还原全部 {len(entries)} 项：")
        for e in entries:
            print(f"   {e['id']}  ->  {e['origPath']}")
    else:
        known = {e["id"] for e in entries}
        unknown = [i for i in ids if i not in known]
        if unknown:
            print(f"[x] 找不到这些 ID: {unknown}")
            return 2
        for e in entries:
            if e["id"] in ids:
                print(f"   {e['id']}  ->  {e['origPath']}")

    if not args.yes:
        try:
            ans = input("\n确认还原？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消。")
            return 0

    done, fail = q.restore(ids=target, on_conflict=args.on_conflict)
    print("\n结果:")
    for e in done:
        print(f"  [OK]   {e['origPath']}   ({human(e.get('size', 0))})")
    for e, why in fail:
        print(f"  [跳过] {e['origPath']}   -> {why}")
    print(f"\n已还原 {len(done)} 项，跳过/失败 {len(fail)} 项。")
    return 0 if not fail else 1


def cmd_purge(q, args):
    entries = q.entries()
    if not entries:
        print("隔离区为空。")
        return 0
    scope = entries
    if args.older_than is not None:
        import time
        now = time.time()
        scope = []
        for e in entries:
            try:
                t = time.mktime(time.strptime(e["at"], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                t = now
            if (now - t) >= args.older_than * 86400:
                scope.append(e)
        print(f"命中 {len(scope)} 项（删除于 {args.older_than} 天前）")
    else:
        print(f"将彻底删除**全部** {len(scope)} 项（不可恢复）")

    total = sum(e.get("size", 0) for e in scope)
    for e in scope:
        print(f"   {e['id']}   {human(e.get('size', 0))}   {e['origPath']}")

    if not scope:
        return 0
    print(f"\n合计将释放 {human(total)}")
    if not args.yes:
        try:
            ans = input("\n⚠️  此操作不可恢复，确认彻底删除？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消。")
            return 0

    if args.older_than is not None:
        done = q.purge(older_than_days=args.older_than)
    else:
        done = q.purge(older_than_days=None)
    print(f"\n已彻底删除 {len(done)} 项，释放 {human(sum(e.get('size', 0) for e in done))}。")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="WorkBuddy 清理隔离区管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python wb_restore.py list\n"
               "  python wb_restore.py restore --id 20261012-231500-2026-09-14-22-35-03\n"
               "  python wb_restore.py purge --older-than 30 --yes\n")
    ap.add_argument("action", choices=["list", "restore", "purge"], help="要执行的动作")
    ap.add_argument("--quarantine", default=DEFAULT_Q, help=f"隔离区目录（默认 {DEFAULT_Q}）")
    ap.add_argument("--id", action="append", default=[], help="restore：指定条目标识，可重复")
    ap.add_argument("--all", action="store_true", help="restore：还原全部条目")
    ap.add_argument("--on-conflict", choices=["rename", "skip"], default="rename",
                    help="restore：目标已存在时改名保存（默认）或跳过")
    ap.add_argument("--older-than", type=int, default=None, metavar="DAYS",
                    help="purge：只清除删除于 N 天前的条目")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（供脚本调用）")
    a = ap.parse_args()

    q = Quarantine(a.quarantine)
    if a.action == "list":
        return cmd_list(q, a)
    if a.action == "restore":
        return cmd_restore(q, a)
    return cmd_purge(q, a)


if __name__ == "__main__":
    sys.exit(main())
