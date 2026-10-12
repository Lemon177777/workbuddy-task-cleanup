#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删任务残留 —— 分级清理器。

安全性设计（v1.1）:
  · 默认 dry-run，不加 --apply 绝不落盘
  · 默认走「回收站」(--to-trash)，要永久删除必须显式加 --no-trash
  · B2（含用户文件/工作区记忆）必须加 --include-b2，且支持 --keep-b2 选择性保留
  · 被存活会话 或 自动化任务(automations.cwds) 引用的目录 —— 一律保护，绝不删除
  · blobs / db / 配置 / 当天日志 —— 硬编码跳过

用法:
    python wb_clean.py --tiers A,B1,C                          # 预览
    python wb_clean.py --tiers A,B1,C --apply                  # 执行（移入回收站）
    python wb_clean.py --tiers A,B1,C --apply --no-trash       # 永久删除
    python wb_clean.py --tiers A,B1,C,B2 --include-b2 --apply \
        --keep-b2 "2026-09-14-22-35-03"                        # B2 保留指定目录
    python wb_clean.py --tiers C --older-than 30 --apply --log clean.json
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_common import Cleaner, State, human   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 已删任务残留分级清理")
    ap.add_argument("--tiers", required=True, help="类别，逗号分隔：A,B1,B2,C")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认仅预览）")
    ap.add_argument("--to-trash", dest="to_trash", action="store_true", default=True,
                    help="移入回收站（默认行为）")
    ap.add_argument("--no-trash", dest="to_trash", action="store_false",
                    help="永久删除，不经回收站。危险！")
    ap.add_argument("--include-b2", action="store_true",
                    help="确认清理 B2（含用户文件/工作区记忆）")
    ap.add_argument("--keep-b2", default="", metavar="NAMES",
                    help="B2 中要保留的目录名，逗号分隔")
    ap.add_argument("--older-than", type=int, default=None, metavar="DAYS",
                    help="只清理修改时间早于 N 天的项")
    ap.add_argument("--log", metavar="FILE", help="把删除清单写入 JSON 留痕")
    ap.add_argument("--workbuddy", default=os.path.expanduser("~/.workbuddy"))
    ap.add_argument("--workspaces", default=os.path.expanduser("~/WorkBuddy"))
    a = ap.parse_args()

    tiers = {t.strip().upper() for t in a.tiers.split(",") if t.strip()}
    bad = tiers - {"A", "B1", "B2", "C"}
    if bad:
        sys.exit(f"[x] 未知类别: {bad}")
    if "B2" in tiers and not a.include_b2:
        sys.exit("[x] B2 含用户成果文件或工作区记忆，必须同时加 --include-b2。")

    st = State(a.workbuddy)
    c = Cleaner(st, a.workspaces, apply=a.apply, to_trash=a.to_trash,
                older_than=a.older_than, log_path=a.log)

    mode = "预览（未落盘）" if not a.apply else ("移入回收站" if a.to_trash else "永久删除")
    print("=" * 70)
    print(f"WorkBuddy 残留清理  [{mode}]  类别={','.join(sorted(tiers))}")
    print(f"已删会话 {len(st.deleted)}  |  存活会话 {len(st.live_ids)}"
          f"  |  受保护工作目录 {len(st.protected_cwds)}")
    if a.older_than:
        print(f"仅清理修改时间早于 {a.older_than} 天的项")
    print("=" * 70)

    if "A" in tiers:
        c.tier_a()
    if "B1" in tiers or "B2" in tiers:
        c.tier_b(want_b1="B1" in tiers, want_b2="B2" in tiers,
                 keep_b2=a.keep_b2.split(",") if a.keep_b2 else ())
    if "C" in tiers:
        c.tier_c()

    total = 0
    print("\n结果:")
    for tag in sorted(c.stats):
        n, s = c.stats[tag]
        total += s
        verb = {"preview": "将处理", "trash": "已移入回收站", "delete": "已删除"}[
            "preview" if not a.apply else ("trash" if a.to_trash else "delete")]
        print(f"  {tag:26s} {verb} {n:4d} 项  {human(s)}")
    print(f"  {'合计':26s}          {human(total)}")

    if getattr(c, "protected_seen", None):
        print(f"\n[受保护] 已跳过 {len(c.protected_seen)} 个被会话/自动化引用的工作区：")
        for r in c.protected_seen:
            print(f"   {r['name']}   [{' / '.join(r['reasons'])}]")

    if st.broken_automations:
        print(f"\n[警告] {len(st.broken_automations)} 条自动化任务引用的目录已不存在：")
        for b in st.broken_automations:
            print(f"   [{b['status']}] {b['name']}  -> {b['cwd']}")
        print("   建议：确认这些任务是否还需要，或把工作目录改到现有位置后再启用。")

    if c.errors:
        print(f"\n跳过(被占用/钩子拦截) {len(c.errors)} 项:")
        for p, e in c.errors[:10]:
            print(f"   {p}  ->  {e}")
        print("   提示: 删除可能延迟落盘，稍后重跑一次即可补齐。")

    c.write_log()
    if a.log and a.apply:
        print(f"\n删除清单已写入: {a.log}")

    miss = [cwd for (_sid, cwd, _t) in st.live if cwd and not os.path.isdir(cwd)]
    print(f"\n存活会话工作区缺失: {len(miss)}")
    for m in miss:
        print(f"   MISSING  {m}")

    if a.apply:
        print("\n请再跑一次 wb_scan.py 做二次核验（删除可能延迟落盘，首轮未必全清）。")
    if c.errors or miss:
        sys.exit(2)


if __name__ == "__main__":
    main()
