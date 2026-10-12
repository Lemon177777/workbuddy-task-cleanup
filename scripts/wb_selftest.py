#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：用临时目录构造场景，验证分类与保护逻辑是否正确。

不会触碰任何真实数据。用法:
    python wb_selftest.py
退出码 0 = 全部通过。
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_common import (Cleaner, State, list_orphan_workspaces,  # noqa: E402
                       send_to_trash)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def build_fixture(base):
    root = os.path.join(base, ".workbuddy")
    wb = os.path.join(base, "WorkBuddy")
    os.makedirs(root)
    os.makedirs(wb)

    live_ws = os.path.join(wb, "2026-01-01-live")
    auto_ws = os.path.join(wb, "2026-01-02-auto")
    shell_ws = os.path.join(wb, "2026-01-03-shell")
    mem_ws = os.path.join(wb, "2026-01-04-memonly")
    file_ws = os.path.join(wb, "2026-01-05-hasfile")
    for p in (live_ws, auto_ws, shell_ws, mem_ws, file_ws):
        os.makedirs(p)
    os.makedirs(os.path.join(shell_ws, ".workbuddy"))
    os.makedirs(os.path.join(mem_ws, ".workbuddy", "memory"))
    with open(os.path.join(mem_ws, ".workbuddy", "memory", "2026-01-04.md"), "w", encoding="utf-8") as f:
        f.write("这是工作区长期记忆，绝不能被当空壳删掉")
    os.makedirs(os.path.join(file_ws, "outputs"))
    with open(os.path.join(file_ws, "outputs", "report.md"), "w", encoding="utf-8") as f:
        f.write("用户成果文件")

    db = os.path.join(root, "workbuddy.db")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE sessions (id TEXT, cwd TEXT, title TEXT, deleted_at INTEGER);
        CREATE TABLE automations (id TEXT, name TEXT, status TEXT, cwds TEXT, deleted_at INTEGER);
        CREATE TABLE workspace_trust_decisions (workspace_key TEXT);
    """)
    con.execute("INSERT INTO sessions VALUES ('live-1', ?, '存活', NULL)", (live_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-1', ?, '已删', 1)", (shell_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-2', ?, '已删', 1)", (mem_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-3', ?, '已删', 1)", (file_ws,))
    import json as _json
    con.execute("INSERT INTO automations VALUES ('a1','测试自动化','ACTIVE',?,NULL)",
                (_json.dumps([auto_ws]),))
    con.execute("INSERT INTO automations VALUES ('a2','已废任务','ACTIVE',?,1)",
                (_json.dumps([os.path.join(wb, "2026-01-09-gone")]),))
    con.commit()
    con.close()

    # Tier A 素材：一个已删会话的缓存 + 一个存活会话的缓存
    for sub in ("changes-index", "changes-detail", "artifact-index"):
        os.makedirs(os.path.join(root, sub))
    with open(os.path.join(root, "changes-index", "dead-1.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(root, "changes-index", "live-1.json"), "w") as f:
        f.write("{}")
    os.makedirs(os.path.join(root, "changes-detail", "dead-1"))
    os.makedirs(os.path.join(root, "changes-detail", "live-1"))
    return root, wb


def main():
    base = tempfile.mkdtemp(prefix="wb_selftest_")
    try:
        root, wb = build_fixture(base)
        print("== 1) 状态加载与保护集合 ==")
        st = State(root)
        check("已删会话 3 个", len(st.deleted) == 3)
        check("存活会话 1 个", len(st.live_ids) == 1)
        check("有效自动化仅 1 条（已删的被过滤）", len(st.automations) == 1,
              f"实际={len(st.automations)}")
        check("已删除的自动化被忽略", all(a["id"] != "a2" for a in st.automations))
        check("受保护集合含存活工作区",
              os.path.normcase(os.path.join(wb, "2026-01-01-live")) in st.protected_cwds)

        print("\n== 2) 孤儿工作区分类 ==")
        b1, b2, prot = list_orphan_workspaces(wb, st)
        n1 = {r["name"] for r in b1}
        n2 = {r["name"] for r in b2}
        np_ = {r["name"] for r in prot}
        check("真空壳只含 shell", n1 == {"2026-01-03-shell"}, f"实际={n1}")
        check("含记忆的进 B2（关键修复）", "2026-01-04-memonly" in n2, f"B2={n2}")
        check("含文件的进 B2", "2026-01-05-hasfile" in n2)
        check("含记忆目录的报告带 memory_files",
              any(r["name"] == "2026-01-04-memonly" and r["memory_files"] for r in b2))
        check("存活工作区被保护", "2026-01-01-live" in np_)
        check("自动化引用工作区被保护（关键修复）", "2026-01-02-auto" in np_)
        check("已废自动化的目录不受保护（可清理）",
              "2026-01-09-gone" not in np_)

        print("\n== 3) Tier A 只挑已删会话 ==")
        c = Cleaner(st, wb, apply=False)
        c.tier_a()
        keys = {(r["tag"], os.path.basename(r["path"])) for r in c.record}
        check("dead-1 的缓存入选", ("changes-index", "dead-1.json") in keys)
        check("live-1 的缓存未入选", ("changes-index", "live-1.json") not in keys)

        print("\n== 4) B2 需显式确认 + keep 保留 ==")
        c2 = Cleaner(st, wb, apply=False)
        c2.tier_b(want_b1=True, want_b2=True, keep_b2=["2026-01-04-memonly"])
        killed = {os.path.basename(r["path"]) for r in c2.record}
        check("被 keep 的 B2 未被处理", "2026-01-04-memonly" not in killed)
        check("其余 B2 被处理", "2026-01-05-hasfile" in killed)

        print("\n== 5) dry-run 绝不落盘 ==")
        check("dry-run 后 shell 目录仍在", os.path.isdir(os.path.join(wb, "2026-01-03-shell")))
        check("dry-run 后 dead-1.json 仍在",
              os.path.isfile(os.path.join(root, "changes-index", "dead-1.json")))

        print("\n== 6) 回收站往返 ==")
        victim = os.path.join(base, "trash_me.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("x")
        try:
            send_to_trash(victim)
            check("文件已离开原位", not os.path.exists(victim))
        except Exception as e:
            check("回收站可用", False, f"异常: {str(e)[:80]}")

        print("\n== 7) 实际删除（真跑一次）==")
        c3 = Cleaner(st, wb, apply=True, to_trash=False)
        c3.tier_b(want_b1=True, want_b2=False)
        check("shell 目录已删除", not os.path.isdir(os.path.join(wb, "2026-01-03-shell")))
        check("含记忆目录仍存在（因未启用 B2）",
              os.path.isdir(os.path.join(wb, "2026-01-04-memonly")))
        check("存活工作区仍存在", os.path.isdir(os.path.join(wb, "2026-01-01-live")))
        check("自动化工作区仍存在", os.path.isdir(os.path.join(wb, "2026-01-02-auto")))

    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
