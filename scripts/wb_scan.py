#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删任务残留 —— 只读盘点（绝不修改任何文件）。

用法:
    python wb_scan.py                       # 人类可读报告
    python wb_scan.py --json out.json       # 同时落 JSON
    python wb_scan.py --fast                # 跳过体积计算，只数数量（快很多）
    python wb_scan.py --match 光缆           # 只看关联到「光缆」的工作区
    python wb_scan.py --check               # 有残留/异常时退出码 1（供定时任务）
    python wb_scan.py --inspect <目录>       # 体检单个目录：这是谁的、能不能清

判定依据: ~/.workbuddy/workbuddy.db 的 sessions.deleted_at
  deleted_at IS NULL -> 存活会话      deleted_at 有值 -> 已删除任务

三重保护（v1.1 起）:
  · 被 sessions 引用的工作区          -> 保护
  · 被 automations.cwds 引用的工作区  -> 保护  ⚠️ 删掉会打断定时任务
  · 含 .workbuddy/memory/ 的工作区    -> 升级为 B2，需人工确认（不再当空壳删）

v1.2 新增:
  · B2 报告附带「工作区关联线索」（曾关联哪些已删会话/自动化）—— 逐个确认时的上下文
  · --fast 并行/跳过体积、--match 按标题筛选、--inspect 单目录体检
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_common import (State, dirsize, dirsize_many, human, is_dated_dir,   # noqa: E402
                       list_orphan_workspaces, match_workspace, workdir_context,
                       workdir_memory_files)

TIER_A = [("tasks", True), ("artifact-index", False), ("changes-index", False),
          ("changes-detail", True), ("file-history", True), ("assistant-display", False)]


def _ctx_lines(ctx, indent="        "):
    """把关联线索渲染成若干行文本。"""
    out = []
    if ctx["sessions_deleted"]:
        head = f"{indent}曾关联 {ctx['sessions_deleted']} 个已删会话"
        if ctx["titles"]:
            shown = " / ".join(ctx["titles"][:4])
            more = f" 等 {ctx['titles_total']} 个" if ctx["titles_total"] > 4 else ""
            out.append(f"{head}：{shown}{more}")
        else:
            out.append(head + "（无标题）")
    elif ctx["sessions_live"]:
        out.append(f"{indent}关联 {ctx['sessions_live']} 个存活会话")
    else:
        out.append(f"{indent}未关联任何会话（可能是临时工作区）")

    has_auto = ctx["automations_deleted"] + ctx["automations_live"]
    if has_auto:
        names = []
        for a in ctx["automations"][:3]:
            flag = "已删" if a["deleted"] else a["status"]
            names.append(f"{a['name']}（{flag}）")
        more = f" 等 {has_auto} 条" if has_auto > 3 else ""
        out.append(f"{indent}曾被 {has_auto} 条自动化引用：{' / '.join(names)}{more}")
    return out


def scan(root, wb, compute_size=True, match=None):
    st = State(root)
    today = time.strftime("%Y-%m-%d")
    today_compact = today.replace("-", "")

    rep = {
        "schemaVersion": 2,
        "root": root, "workspaces": wb, "scannedAt": today,
        "match": match,
        "sessions": {"total": len(st.rows), "deleted": len(st.deleted), "live": len(st.live_ids)},
        "automations": {"total": len(st.automations),
                        "deleted": len(st.all_automations) - len(st.automations),
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

    # ---- Tier A ----
    a_targets = []
    for sub, isdir in TIER_A:
        d = os.path.join(root, sub)
        items = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                key = name[:-5] if (not isdir and name.endswith(".json")) else name
                if key in st.deleted:
                    items.append(os.path.join(d, name))
        a_targets.extend(items)
        rep["tier_a"][sub] = {"count": len(items), "size": 0, "items": sorted(os.path.basename(i) for i in items)}

    pd = os.path.join(root, "projects")
    proj_files, proj_dirs = [], []
    if os.path.isdir(pd):
        for slug in os.listdir(pd):
            sp = os.path.join(pd, slug)
            if not os.path.isdir(sp):
                continue
            dl, has_live = [], False
            for f in os.listdir(sp):
                key = f.split(".")[0]
                if key in st.deleted:
                    dl.append(os.path.join(sp, f))
                elif key in st.live_ids:
                    has_live = True
            if dl:
                a_targets.extend(dl)
                proj_files.extend(f"{slug}/{os.path.basename(p)}" for p in dl)
                if not has_live:
                    proj_dirs.append(slug)

    if compute_size and a_targets:
        sz = dirsize_many(a_targets)
        per_sub = {}
        for p, n in sz.items():
            base = os.path.basename(os.path.dirname(p))
            if os.path.dirname(p) == pd:
                base = "projects"
            per_sub[base] = per_sub.get(base, 0) + n
        for k, v in per_sub.items():
            if k in rep["tier_a"]:
                rep["tier_a"][k]["size"] = v
        rep["tier_a"]["projects"] = {"count": len(proj_files), "size": per_sub.get("projects", 0),
                                     "emptied_slug_dirs": sorted(proj_dirs)}
    else:
        rep["tier_a"]["projects"] = {"count": len(proj_files), "size": 0,
                                     "emptied_slug_dirs": sorted(proj_dirs)}

    # ---- Tier B（含保护判定与关联线索） ----
    b1, b2, protected = list_orphan_workspaces(wb, st, compute_size=compute_size, match=match)
    rep["tier_b1"]["dirs"] = [r["name"] for r in b1]
    rep["tier_b1"]["count"] = len(b1)
    rep["tier_b1"]["size"] = sum(r["size"] for r in b1)

    rep["tier_b2"]["dirs"] = [{"name": r["name"], "size": r["size"],
                               "has_files": r["has_files"], "memory_files": r["memory_files"],
                               "ctx": r["ctx"], "files": r.get("files", [])} for r in b2]
    rep["tier_b2"]["count"] = len(b2)
    rep["tier_b2"]["size"] = sum(r["size"] for r in b2)

    rep["protected_workspaces"] = [{"name": r["name"], "size": r["size"], "reasons": r["reasons"]}
                                   for r in protected]

    # ---- Tier C ----
    tr = os.path.join(root, "traces")
    if os.path.isdir(tr):
        items = [os.path.join(tr, i) for i in os.listdir(tr)]
        rep["tier_c"]["traces"] = {"count": len(items),
                                   "size": (sum(dirsize_many(items).values()) if (compute_size and items) else 0)}
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
                        rep["tier_c"]["logs_sandbox"]["size"] += dirsize(sf) if compute_size else 0
            elif os.path.isdir(full) and is_dated_dir(d) and d != today:
                rep["tier_c"]["logs_dated"]["dirs"].append(d)
                rep["tier_c"]["logs_dated"]["count"] += 1
                rep["tier_c"]["logs_dated"]["size"] += dirsize(full) if compute_size else 0

    # ---- 完整性 ----
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
    rep["_sizesKnown"] = bool(compute_size)
    return rep


def print_report(rep):
    s = rep["sessions"]
    known = rep.get("_sizesKnown", True)
    hb = human if known else (lambda _n: "—")
    unit = "" if known else "   （--fast：未统计体积）"
    print("=" * 72)
    print(f"WorkBuddy 已删任务残留盘点   root={rep['root']}{unit}")
    print(f"会话总数 {s['total']}  |  已删除 {s['deleted']}  |  存活 {s['live']}")
    if rep.get("match"):
        print(f"筛选条件 --match：{rep['match']}")
    print("=" * 72)

    au = rep["automations"]
    print(f"\n[自动化] 存活 {au['total']} 条 / 已删 {au['deleted']} 条"
          f"  保护 {len(au['protected_cwds'])} 个工作目录")
    if au["broken"]:
        print(f"    [警告] {len(au['broken'])} 条存活任务引用的目录已不存在（运行会失败）：")
        for b in au["broken"]:
            print(f"       [{b['status']}] {b['name']}")
            print(f"            缺失目录: {b['cwd']}")

    print("\n[A] 按已删会话 id 命名的缓存（纯内部数据，可直接清理）")
    ta = 0
    for sub, v in rep["tier_a"].items():
        ta += v["size"]
        print(f"    {sub:18s} 孤儿 {v['count']:4d} 个   {hb(v['size'])}")
    print(f"    {'小计':18s} {'':>11s}   {hb(ta)}")

    b1 = rep["tier_b1"]
    print(f"\n[B1] 真·空壳孤儿工作区（无文件、无记忆）  {b1['count']} 个  {hb(b1['size'])}")
    for d in b1["dirs"]:
        print(f"    {d}")

    b2 = rep["tier_b2"]
    print(f"\n[B2] 含用户文件/工作区记忆的孤儿工作区  {b2['count']} 个  {hb(b2['size'])}  [须逐个确认]")
    for it in b2["dirs"]:
        flags = []
        if it["has_files"]:
            flags.append("用户文件")
        if it["memory_files"]:
            flags.append(f"工作区记忆x{len(it['memory_files'])}")
        if it.get("odd"):
            flags.append("非标准命名⚠")
        print(f"    ### {it['name']}   [{hb(it['size'])}]  {'+'.join(flags) or '空目录'}")
        for line in _ctx_lines(it.get("ctx") or {}):
            print(line)
        for f in it["files"][:12]:
            print(f"        {hb(f['size']):>9s}  {f['rel']}")
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
    print(f"\n[C] 诊断日志 / 追踪  合计 {hb(ctot)}")
    print(f"    traces/            {c['traces']['count']:4d} 项   {hb(c['traces']['size'])}")
    print(f"    logs/<日期>/       {c['logs_dated']['count']:4d} 天   {hb(c['logs_dated']['size'])}")
    print(f"    logs/sandbox/      {c['logs_sandbox']['count']:4d} 天   {hb(c['logs_sandbox']['size'])}")

    li = rep["live_integrity"]
    print(f"\n[OK] 存活会话 {li['count']} 个，工作区缺失 {len(li['missing'])} 个")
    for m in li["missing"]:
        print(f"    MISSING  {m}")

    print("\n提醒: 先向用户呈现本报告并取得确认，再执行清理；B2 必须逐个列明文件。")


def inspect(path, root):
    """体检单个目录 —— 回答「这是谁的、能不能清」。"""
    st = State(root)
    ap = os.path.abspath(path)
    ctx = workdir_context(ap, st)
    exists = os.path.isdir(ap)

    print("=" * 72)
    print(f"目录体检   {ap}")
    print("=" * 72)
    print(f"存在      : {'是' if exists else '否'}")
    if exists:
        print(f"体积      : {human(dirsize(ap))}")
        mem = workdir_memory_files(ap)
        print(f"工作区记忆: {len(mem)} 个文件")
        for m in mem[:8]:
            print(f"             {m}")
            if len(mem) > 8:
                print(f"             ... 另有 {len(mem) - 8} 个")
                break

    print(f"\n关联会话  : {ctx['sessions_total']} 个"
          f"（已删 {ctx['sessions_deleted']} / 存活 {ctx['sessions_live']}）")
    for t in ctx["titles"]:
        print(f"             · [已删] {t}")
    if ctx["titles_total"] > len(ctx["titles"]):
        print(f"             ... 另有 {ctx['titles_total'] - len(ctx['titles'])} 个已删标题未显示")
    for t in ctx.get("live_titles", []):
        print(f"             · [存活] {t}")

    auto = ctx["automations"]
    print(f"\n关联自动化: {len(auto)} 条"
          f"（存活 {ctx['automations_live']} / 已删 {ctx['automations_deleted']}）")
    for a in auto:
        flag = "存活" if not a["deleted"] else "已删"
        print(f"             · [{a['status']}/{flag}] {a['name']}")

    protected = st.is_protected(ap)
    print(f"\n保护判定  : {'★ 受保护 —— 绝不删除' if protected else '未受保护'}")
    if protected:
        from wb_common import _protect_reasons
        print(f"             原因：{' / '.join(_protect_reasons(ap, st))}")
    elif exists:
        b1, b2, _p = list_orphan_workspaces(os.path.dirname(ap), st, compute_size=False, match=None)
        names_b1 = {r["name"] for r in b1}
        names_b2 = {r["name"] for r in b2}
        base = os.path.basename(ap.rstrip("\\/"))
        if base in names_b1:
            print("             分类：B1 真·空壳 —— 可安全清理")
        elif base in names_b2:
            print("             分类：B2 含用户文件或记忆 —— 须人工确认")
        else:
            print("             分类：不在扫描范围内（可能不是 ~/WorkBuddy 下的工作区）")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(
        description="WorkBuddy 已删任务残留只读盘点",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python wb_scan.py --fast\n"
               "  python wb_scan.py --match 光缆 --json out.json\n"
               "  python wb_scan.py --inspect ~/WorkBuddy/2026-09-14-22-35-03\n")
    ap.add_argument("--workbuddy", default=os.path.expanduser("~/.workbuddy"))
    ap.add_argument("--workspaces", default=os.path.expanduser("~/WorkBuddy"))
    ap.add_argument("--json", metavar="OUT", help="同时输出 JSON 报告")
    ap.add_argument("--fast", action="store_true", help="跳过体积计算，只数数量（快很多）")
    ap.add_argument("--match", metavar="关键词", help="只看关联到该关键词的工作区（匹配目录名/会话标题/自动化名）")
    ap.add_argument("--inspect", metavar="PATH", help="体检单个目录：这是谁的、能不能清")
    ap.add_argument("--check", action="store_true",
                    help="有残留或异常时退出码 1（供定时任务判断）")
    a = ap.parse_args()

    if a.inspect:
        inspect(a.inspect, a.workbuddy)
        return

    rep = scan(a.workbuddy, a.workspaces, compute_size=not a.fast, match=a.match)
    print_report(rep)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入: {a.json}")
    if a.check and rep["_dirty"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
