#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删除任务残留 —— 只读盘点（绝不修改任何文件）。

用法:
    python wb_scan.py                       # 人类可读报告
    python wb_scan.py --json out.json       # 同时落一份 JSON

判定依据: ~/.workbuddy/workbuddy.db 的 sessions 表 deleted_at
  - deleted_at IS NULL  -> 存活会话
  - deleted_at 有值     -> 已删除任务（其磁盘残留即"垃圾"）

纯标准库，无第三方依赖。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

TIER_A = [
    ("tasks", True),
    ("artifact-index", False),
    ("changes-index", False),
    ("changes-detail", True),
    ("file-history", True),
    ("assistant-display", False),
]


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def dirsize(p):
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    t = 0
    for r, _ds, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return t


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def is_dated_dir(name):
    return len(name) == 10 and name[4] == "-" and name[7] == "-"


def load_sessions(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = list(con.execute("SELECT id,cwd,title,deleted_at FROM sessions"))
    finally:
        con.close()
    deleted = {r[0] for r in rows if r[3] is not None}
    live = [(r[0], r[1], r[2]) for r in rows if r[3] is None]
    live_cwds = {norm(r[1]) for r in live if r[1]}
    return deleted, live, live_cwds, rows


def scan(root, wb):
    db = os.path.join(root, "workbuddy.db")
    if not os.path.isfile(db):
        sys.exit(f"[x] 找不到数据库: {db}")
    deleted, live, live_cwds, rows = load_sessions(db)
    today = time.strftime("%Y-%m-%d")
    today_compact = today.replace("-", "")

    rep = {
        "root": root,
        "workspaces": wb,
        "scannedAt": today,
        "sessions": {"total": len(rows), "deleted": len(deleted), "live": len(live)},
        "tier_a": {},
        "tier_b1": {"count": 0, "size": 0, "dirs": []},
        "tier_b2": {"count": 0, "size": 0, "dirs": []},
        "tier_c": {
            "traces": {"count": 0, "size": 0},
            "logs_dated": {"count": 0, "size": 0, "dirs": []},
            "logs_sandbox": {"count": 0, "size": 0, "dirs": []},
        },
        "live_integrity": {"count": len(live), "missing": []},
    }

    # ---- Tier A: 按已删会话 id 命名的缓存 ----
    for sub, isdir in TIER_A:
        d = os.path.join(root, sub)
        items = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                key = name[:-5] if (not isdir and name.endswith(".json")) else name
                if key in deleted:
                    items.append({"name": name, "size": dirsize(os.path.join(d, name))})
        rep["tier_a"][sub] = {
            "count": len(items),
            "size": sum(i["size"] for i in items),
            "items": sorted(i["name"] for i in items),
        }

    # ---- Tier A: projects/<cwd-slug>/<sessionId>.* ----
    pd = os.path.join(root, "projects")
    proj_files, proj_dirs, proj_size = [], [], 0
    if os.path.isdir(pd):
        for slug in os.listdir(pd):
            sp = os.path.join(pd, slug)
            if not os.path.isdir(sp):
                continue
            dl = []
            has_live = False
            for f in os.listdir(sp):
                key = f.split(".")[0]
                if key in deleted:
                    dl.append(f)
                    proj_size += dirsize(os.path.join(sp, f))
                elif key in {r[0] for r in live}:
                    has_live = True
            if dl:
                proj_files.extend(f"{slug}/{f}" for f in dl)
                if not has_live:
                    proj_dirs.append(slug)
    rep["tier_a"]["projects"] = {
        "count": len(proj_files),
        "size": proj_size,
        "emptied_slug_dirs": sorted(proj_dirs),
    }

    # ---- Tier B: 孤儿工作区 ~/WorkBuddy/<ts> ----
    if os.path.isdir(wb):
        for d in sorted(os.listdir(wb)):
            full = os.path.join(wb, d)
            if not os.path.isdir(full):
                continue
            if norm(full) in live_cwds:
                continue
            user_items = [i for i in os.listdir(full) if i != ".workbuddy"]
            sz = dirsize(full)
            if user_items:
                files = []
                for r, _ds, fs in os.walk(full):
                    if ".workbuddy" in r.split(os.sep):
                        continue
                    for f in fs:
                        fp = os.path.join(r, f)
                        files.append({"rel": os.path.relpath(fp, full), "size": dirsize(fp)})
                files.sort(key=lambda x: -x["size"])
                rep["tier_b2"]["dirs"].append({"name": d, "size": sz, "files": files})
                rep["tier_b2"]["count"] += 1
                rep["tier_b2"]["size"] += sz
            else:
                rep["tier_b1"]["dirs"].append(d)
                rep["tier_b1"]["count"] += 1
                rep["tier_b1"]["size"] += sz

    # ---- Tier C: 诊断 ----
    tr = os.path.join(root, "traces")
    if os.path.isdir(tr):
        items = os.listdir(tr)
        rep["tier_c"]["traces"] = {
            "count": len(items),
            "size": sum(dirsize(os.path.join(tr, i)) for i in items),
        }
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

    # ---- 存活会话工作区完整性 ----
    for sid, cwd, _title in live:
        if cwd and not os.path.isdir(cwd):
            rep["live_integrity"]["missing"].append(cwd)

    return rep


def print_report(rep):
    s = rep["sessions"]
    print("=" * 68)
    print(f"WorkBuddy 已删任务残留盘点   root={rep['root']}")
    print(f"会话总数 {s['total']}  |  已删除 {s['deleted']}  |  存活 {s['live']}")
    print("=" * 68)

    print("\n[A] 按已删会话 id 命名的缓存（纯内部数据，可直接清理）")
    ta_total = 0
    for sub, v in rep["tier_a"].items():
        ta_total += v["size"]
        print(f"    {sub:18s} 孤儿 {v['count']:4d} 个   {human(v['size'])}")
    print(f"    {'小计':18s} {'':>11s}   {human(ta_total)}")

    b1 = rep["tier_b1"]
    print(f"\n[B1] 空壳孤儿工作区（只剩 .workbuddy）  {b1['count']} 个  {human(b1['size'])}")
    for d in b1["dirs"]:
        print(f"    {d}")

    b2 = rep["tier_b2"]
    print(f"\n[B2] 含用户文件的孤儿工作区  {b2['count']} 个  {human(b2['size'])}   ⚠️ 删前须逐个确认")
    for it in b2["dirs"]:
        print(f"    ### {it['name']}   [{human(it['size'])}]")
        for f in it["files"][:15]:
            print(f"        {human(f['size']):>9s}  {f['rel']}")
        if len(it["files"]) > 15:
            print(f"        ... 另有 {len(it['files']) - 15} 个文件")

    c = rep["tier_c"]
    ctot = c["traces"]["size"] + c["logs_dated"]["size"] + c["logs_sandbox"]["size"]
    print(f"\n[C] 诊断日志 / 追踪  合计 {human(ctot)}")
    print(f"    traces/            {c['traces']['count']:4d} 项   {human(c['traces']['size'])}")
    print(f"    logs/<日期>/       {c['logs_dated']['count']:4d} 天   {human(c['logs_dated']['size'])}")
    print(f"    logs/sandbox/      {c['logs_sandbox']['count']:4d} 天   {human(c['logs_sandbox']['size'])}")

    li = rep["live_integrity"]
    print(f"\n[✓] 存活会话 {li['count']} 个，工作区缺失 {len(li['missing'])} 个")
    for m in li["missing"]:
        print(f"    MISSING  {m}")

    print("\n提醒: 清理前务必先向用户呈现本报告并取得确认。B2 必须逐个列明文件。")


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 已删任务残留只读盘点")
    ap.add_argument("--workbuddy", default=os.path.expanduser("~/.workbuddy"),
                    help="WorkBuddy 数据目录 (默认 ~/.workbuddy)")
    ap.add_argument("--workspaces", default=os.path.expanduser("~/WorkBuddy"),
                    help="Workspace 根目录 (默认 ~/WorkBuddy)")
    ap.add_argument("--json", metavar="OUT", help="同时输出 JSON 报告到指定路径")
    a = ap.parse_args()

    rep = scan(a.workbuddy, a.workspaces)
    print_report(rep)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入: {a.json}")


if __name__ == "__main__":
    main()
