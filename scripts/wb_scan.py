#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删任务残留 —— 只读盘点（绝不修改任何文件）。

用法:
    python wb_scan.py                       # 人类可读报告
    python wb_scan.py --json out.json       # 同时落 JSON
    python wb_scan.py --check               # 有残留/异常时退出码 1（供定时任务）

判定依据: ~/.workbuddy/workbuddy.db 的 sessions.deleted_at
  deleted_at IS NULL -> 存活会话      deleted_at 有值 -> 已删除任务

三重保护（v1.1 新增）:
  · 被 sessions 引用的工作区          -> 保护
  · 被 automations.cwds 引用的工作区  -> 保护  ⚠️ 关键：删掉会打断定时任务
  · 含 .workbuddy/memory/ 的工作区    -> 升级为 B2，需人工确认（不再当空壳删）
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_common import (State, dirsize, human, is_dated_dir,   # noqa: E402
                       list_orphan_workspaces)

TIER_A = [("tasks", True), ("artifact-index", False), ("changes-index", False),
          ("changes-detail", True), ("file-history", True), ("assistant-display", False)]


def scan(root, wb):
    st = State(root)
    today = time.strftime("%Y-%m-%d")
    today_compact = today.replace("-", "")

    rep = {
        "root": root, "workspaces": wb, "scannedAt": today,
        "sessions": {"total": len(st.rows), "deleted": len(st.deleted), "live": len(st.live_ids)},
        "automations": {"total": len(st.automations),
                        "protected_cwds": sorted(st.automation_cwds),
                        "broken": st.broken_automations},
        "tier_a": {}, "tier_b1": {"count": 0, "size": 0, "dirs": []},
        "tier_b2": {"count": 0, "size": 0, "dirs": []},
        "protected_workspaces": [],
        "tier_c": {"traces": {"count": 0, "size": 0},
                   "logs_dated": {"count": 0, "size": 0, "dirs": []},
                   "logs_sandbox": {"count": 0, "size": 0, "dirs": []}},
        "live_integrity": {"count": len(st.live), "missing": []},
    }

    # ---- Tier A
    for sub, isdir in TIER_A:
        d = os.path.join(root, sub)
        items = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                key = name[:-5] if (not isdir and name.endswith(".json")) else name
                if key in st.deleted:
                    items.append({"name": name, "size": dirsize(os.path.join(d, name))})
        rep["tier_a"][sub] = {"count": len(items), "size": sum(i["size"] for i in items),
                              "items": sorted(i["name"] for i in items)}

    pd = os.path.join(root, "projects")
    proj_files, proj_dirs, proj_size = [], [], 0
    if os.path.isdir(pd):
        for slug in os.listdir(pd):
            sp = os.path.join(pd, slug)
            if not os.path.isdir(sp):
                continue
            dl, has_live = [], False
            for f in os.listdir(sp):
                key = f.split(".")[0]
                if key in st.deleted:
                    dl.append(f)
                    proj_size += dirsize(os.path.join(sp, f))
                elif key in st.live_ids:
                    has_live = True
            if dl:
                proj_files.extend(f"{slug}/{f}" for f in dl)
                if not has_live:
                    proj_dirs.append(slug)
    rep["tier_a"]["projects"] = {"count": len(proj_files), "size": proj_size,
                                 "emptied_slug_dirs": sorted(proj_dirs)}

    # ---- Tier B（含保护判定）
    b1, b2, protected = list_orphan_workspaces(wb, st)
    rep["tier_b1"]["dirs"] = [r["name"] for r in b1]
    rep["tier_b1"]["count"] = len(b1)
    rep["tier_b1"]["size"] = sum(r["size"] for r in b1)

    rep["tier_b2"]["dirs"] = [{"name": r["name"], "size": r["size"],
                               "has_files": r["has_files"], "memory_files": r["memory_files"],
                               "files": r.get("files", [])} for r in b2]
    rep["tier_b2"]["count"] = len(b2)
    rep["tier_b2"]["size"] = sum(r["size"] for r in b2)

    rep["protected_workspaces"] = [{"name": r["name"], "size": r["size"], "reasons": r["reasons"]}
                                   for r in protected]

    # ---- Tier C
    tr = os.path.join(root, "traces")
    if os.path.isdir(tr):
        items = os.listdir(tr)
        rep["tier_c"]["traces"] = {"count": len(items),
                                   "size": sum(dirsize(os.path.join(tr, i)) for i in items)}
    lg = os.path.join(root, "logs")
    if os.path.isdir(lg):
        for d in sorted(os.listdir(lg)):
            full = os.path.join(lg, d)
            if d == "sandbox" and os.path.isdir(full):
                for sd in sorted(os.listdir(full)):
                    sf = os.path.join(full, sd)
                    if os.path.isdir(sf) and sd != today_compact:
                        rep["tier_c"]["logs_sandbox"]["dirs"].append(sd)
                        rep["tier_c"]["logs_sandbox"]["count"] += 1
                        rep["tier_c"]["logs_sandbox"]["size"] += dirsize(sf)
            elif os.path.isdir(full) and is_dated_dir(d) and d != today:
                rep["tier_c"]["logs_dated"]["dirs"].append(d)
                rep["tier_c"]["logs_dated"]["count"] += 1
                rep["tier_c"]["logs_dated"]["size"] += dirsize(full)

    # ---- 完整性
    for _sid, cwd, _t in st.live:
        if cwd and not os.path.isdir(cwd):
            rep["live_integrity"]["missing"].append(cwd)

    rep["_dirty"] = bool(
        sum(v["count"] for v in rep["tier_a"].values())
        + rep["tier_b1"]["count"] + rep["tier_b2"]["count"]
        + rep["tier_c"]["traces"]["count"] + rep["tier_c"]["logs_dated"]["count"]
        + rep["tier_c"]["logs_sandbox"]["count"]
        + len(rep["live_integrity"]["missing"])
        + len(st.broken_automations)
    )
    return rep


def print_report(rep):
    s = rep["sessions"]
    print("=" * 70)
    print(f"WorkBuddy 已删任务残留盘点   root={rep['root']}")
    print(f"会话总数 {s['total']}  |  已删除 {s['deleted']}  |  存活 {s['live']}")
    print("=" * 70)

    au = rep["automations"]
    print(f"\n[自动化] 共 {au['total']} 条任务，保护 {len(au['protected_cwds'])} 个工作目录")
    if au["broken"]:
        print(f"    [警告] {len(au['broken'])} 条任务引用的目录已不存在（运行会失败）：")
        for b in au["broken"]:
            print(f"       [{b['status']}] {b['name']}")
            print(f"            缺失目录: {b['cwd']}")

    print("\n[A] 按已删会话 id 命名的缓存（纯内部数据，可直接清理）")
    ta = 0
    for sub, v in rep["tier_a"].items():
        ta += v["size"]
        print(f"    {sub:18s} 孤儿 {v['count']:4d} 个   {human(v['size'])}")
    print(f"    {'小计':18s} {'':>11s}   {human(ta)}")

    b1 = rep["tier_b1"]
    print(f"\n[B1] 真·空壳孤儿工作区（无文件、无记忆）  {b1['count']} 个  {human(b1['size'])}")
    for d in b1["dirs"]:
        print(f"    {d}")

    b2 = rep["tier_b2"]
    print(f"\n[B2] 含用户文件/工作区记忆的孤儿工作区  {b2['count']} 个  {human(b2['size'])}  [须逐个确认]")
    for it in b2["dirs"]:
        flags = []
        if it["has_files"]:
            flags.append("用户文件")
        if it["memory_files"]:
            flags.append(f"工作区记忆x{len(it['memory_files'])}")
        print(f"    ### {it['name']}   [{human(it['size'])}]  {'+'.join(flags)}")
        for f in it["files"][:12]:
            print(f"        {human(f['size']):>9s}  {f['rel']}")
        if len(it["files"]) > 12:
            print(f"        ... 另有 {len(it['files']) - 12} 个文件")
        for m in it["memory_files"]:
            print(f"        [记忆]    {m}")

    pw = rep["protected_workspaces"]
    if pw:
        print(f"\n[受保护] 绝不删除的孤儿工作区  {len(pw)} 个")
        for it in pw:
            print(f"    {it['name']}   [{' / '.join(it['reasons'])}]")

    c = rep["tier_c"]
    ctot = c["traces"]["size"] + c["logs_dated"]["size"] + c["logs_sandbox"]["size"]
    print(f"\n[C] 诊断日志 / 追踪  合计 {human(ctot)}")
    print(f"    traces/            {c['traces']['count']:4d} 项   {human(c['traces']['size'])}")
    print(f"    logs/<日期>/       {c['logs_dated']['count']:4d} 天   {human(c['logs_dated']['size'])}")
    print(f"    logs/sandbox/      {c['logs_sandbox']['count']:4d} 天   {human(c['logs_sandbox']['size'])}")

    li = rep["live_integrity"]
    print(f"\n[OK] 存活会话 {li['count']} 个，工作区缺失 {len(li['missing'])} 个")
    for m in li["missing"]:
        print(f"    MISSING  {m}")

    print("\n提醒: 先向用户呈现本报告并取得确认，再执行清理；B2 必须逐个列明文件。")


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 已删任务残留只读盘点")
    ap.add_argument("--workbuddy", default=os.path.expanduser("~/.workbuddy"))
    ap.add_argument("--workspaces", default=os.path.expanduser("~/WorkBuddy"))
    ap.add_argument("--json", metavar="OUT", help="同时输出 JSON 报告")
    ap.add_argument("--check", action="store_true",
                    help="有残留或异常时退出码 1（供定时任务判断）")
    a = ap.parse_args()

    rep = scan(a.workbuddy, a.workspaces)
    print_report(rep)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入: {a.json}")
    if a.check and rep["_dirty"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
