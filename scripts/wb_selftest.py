#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：用临时目录构造场景，验证分类、保护与还原逻辑是否正确。

不会触碰任何真实数据。用法:
    python wb_selftest.py
退出码 0 = 全部通过。

重点覆盖历史上踩过的每一个坑：
  · 含记忆的目录必须进 B2，不能被当空壳删掉
  · 被「存活」自动化引用的目录必须受保护
  · 被「已删除」自动化引用的目录**不能**受保护（否则永远清不掉）
  · 已删除的自动化不能算进保护集合，但要在关联线索里可见（曾因此误报过）
  · dry-run 绝不落盘；隔离区可精确还原
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_common import (Cleaner, Quarantine, State, dirsize, dirsize_many,   # noqa: E402
                       list_orphan_workspaces, match_workspace, robust_remove,
                       send_to_trash, workdir_context)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def build_fixture(base):
    root = os.path.join(base, ".workbuddy")
    wb = os.path.join(base, "WorkBuddy")
    os.makedirs(root)
    os.makedirs(wb)

    live_ws = os.path.join(wb, "2026-01-01-00-00-01")
    auto_ws = os.path.join(wb, "2026-01-02-00-00-02")
    shell_ws = os.path.join(wb, "2026-01-03-00-00-03")
    mem_ws = os.path.join(wb, "2026-01-04-00-00-04")
    file_ws = os.path.join(wb, "2026-01-05-00-00-05")
    gait_ws = os.path.join(wb, "2026-01-06-00-00-06")
    odd_ws = os.path.join(wb, "MyOwnDir")
    for p in (live_ws, auto_ws, shell_ws, mem_ws, file_ws, gait_ws, odd_ws):
        os.makedirs(p)
    os.makedirs(os.path.join(shell_ws, ".workbuddy"))
    os.makedirs(os.path.join(gait_ws, ".workbuddy"))
    os.makedirs(os.path.join(mem_ws, ".workbuddy", "memory"))
    with open(os.path.join(mem_ws, ".workbuddy", "memory", "2026-01-04.md"), "w", encoding="utf-8") as f:
        f.write("这是工作区长期记忆，绝不能被当空壳删掉")
    os.makedirs(os.path.join(file_ws, "outputs"))
    with open(os.path.join(file_ws, "outputs", "report.md"), "w", encoding="utf-8") as f:
        f.write("用户成果文件")
    os.makedirs(os.path.join(file_ws, ".workbuddy", "memory"))
    with open(os.path.join(file_ws, ".workbuddy", "memory", "2026-01-05.md"), "w", encoding="utf-8") as f:
        f.write("既有文件也有记忆")

    db = os.path.join(root, "workbuddy.db")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE sessions (id TEXT, cwd TEXT, title TEXT, deleted_at INTEGER);
        CREATE TABLE automations (id TEXT, name TEXT, status TEXT, cwds TEXT,
                                  deleted_at INTEGER, last_run_at INTEGER, next_run_at INTEGER);
        CREATE TABLE workspace_trust_decisions (workspace_key TEXT);
    """)
    con.execute("INSERT INTO sessions VALUES ('live-1', ?, '存活会话', NULL)", (live_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-1', ?, '已删 A', 1)", (shell_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-2', ?, '已删 B', 1)", (mem_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-3', ?, '已删 C', 1)", (file_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-4', ?, '光缆台账（延庆段）', 1)", (file_ws,))
    con.execute("INSERT INTO sessions VALUES ('dead-5', ?, '光缆台账（密云段）', 1)", (file_ws,))
    con.execute("INSERT INTO automations VALUES ('a1','存活自动化','ACTIVE',?,NULL,1,2)",
                (json.dumps([auto_ws]),))
    con.execute("INSERT INTO automations VALUES ('a2','已废任务','ACTIVE',?,1,1,2)",
                (json.dumps([os.path.join(wb, "2026-01-09-00-00-09")]),))
    con.execute("INSERT INTO automations VALUES ('a3','已删自动化','PAUSED',?,1,1,2)",
                (json.dumps([gait_ws]),))
    con.commit()
    con.close()

    for sub in ("changes-index", "changes-detail", "artifact-index"):
        os.makedirs(os.path.join(root, sub))
    with open(os.path.join(root, "changes-index", "dead-1.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(root, "changes-index", "live-1.json"), "w") as f:
        f.write("{}")
    os.makedirs(os.path.join(root, "changes-detail", "dead-1"))
    os.makedirs(os.path.join(root, "changes-detail", "live-1"))

    paths = dict(live=live_ws, auto=auto_ws, shell=shell_ws, mem=mem_ws,
                 file=file_ws, gait=gait_ws, odd=odd_ws, root=root, wb=wb)
    return paths


def main():
    base = tempfile.mkdtemp(prefix="wb_selftest_")
    try:
        P = build_fixture(base)

        print("== 1) 状态加载与保护集合 ==")
        st = State(P["root"])
        check("已删会话 5 个", len(st.deleted) == 5, f"实际={len(st.deleted)}")
        check("存活会话 1 个", len(st.live_ids) == 1)
        check("存活自动化仅 1 条（已删的被排除）", len(st.automations) == 1,
              f"实际={len(st.automations)}")
        check("全部自动化读到 3 条（含已删，供关联线索用）", len(st.all_automations) == 3,
              f"实际={len(st.all_automations)}")
        check("受保护集合含存活工作区", st.is_protected(P["live"]))
        check("受保护集合含存活自动化的工作区", st.is_protected(P["auto"]))
        check("★ 受保护集合**不含**已删自动化的工作区（防误保护）",
              not st.is_protected(P["gait"]))

        print("\n== 2) 孤儿工作区分类 ==")
        b1, b2, prot = list_orphan_workspaces(P["wb"], st)
        n1 = {r["name"] for r in b1}
        n2 = {r["name"] for r in b2}
        np_ = {r["name"] for r in prot}
        check("真空壳含 shell", "2026-01-03-00-00-03" in n1, f"实际={sorted(n1)}")
        check("★ 已删自动化的空壳目录归入 B1（不被保护、可清理）",
              "2026-01-06-00-00-06" in n1)
        check("含记忆的进 B2（关键修复）", "2026-01-04-00-00-04" in n2, f"B2={sorted(n2)}")
        check("含文件的进 B2", "2026-01-05-00-00-05" in n2)
        check("含记忆目录的报告带 memory_files",
              any(r["name"] == "2026-01-04-00-00-04" and r["memory_files"] for r in b2))
        check("存活工作区被保护", "2026-01-01-00-00-01" in np_)
        check("自动化引用工作区被保护（关键修复）", "2026-01-02-00-00-02" in np_)
        check("已废自动化的目录不受保护（可清理）", "2026-01-09-00-00-09" not in np_)
        check("★ 非标准命名的空目录进 B2 而非 B1（可能是用户手建）",
              "MyOwnDir" in n2 and "MyOwnDir" not in n1, f"B1={sorted(n1)} B2={sorted(n2)}")
        check("非标准命名目录在报告里带 odd 标记",
              any(r["name"] == "MyOwnDir" and r.get("odd") for r in b2))

        print("\n== 3) 关联线索（v1.2 新增） ==")
        ctx = workdir_context(P["file"], st)
        check("关联线索：统计到 3 个已删会话", ctx["sessions_deleted"] == 3,
              f"实际={ctx['sessions_deleted']}")
        check("关联线索：标题可用于辨认项目",
              any("光缆台账" in t for t in ctx["titles"]), f"titles={ctx['titles']}")
        check("关联线索：存活会话数为 0", ctx["sessions_live"] == 0)
        ctx_g = workdir_context(P["gait"], st)
        check("★ 关联线索能看见「已删的自动化」（曾因此误报）",
              ctx_g["automations_deleted"] == 1 and ctx_g["automations_live"] == 0,
              f"实际 deleted={ctx_g['automations_deleted']} live={ctx_g['automations_live']}")
        ctx_none = workdir_context(os.path.join(base, "never-used-dir"), st)
        check("关联线索：无关联时全为 0",
              ctx_none["sessions_total"] == 0 and ctx_none["titles_total"] == 0
              and ctx_none["automations_deleted"] == 0)
        check("B2 记录里带 ctx 字段", all("ctx" in r for r in b2))

        print("\n== 4) --match 筛选（v1.2 新增） ==")
        _, b2m, _ = list_orphan_workspaces(P["wb"], st, compute_size=False, match="光缆")
        check("按会话标题命中", any(r["name"] == "2026-01-05-00-00-05" for r in b2m),
              f"实际={[r['name'] for r in b2m]}")
        b1m, _, _ = list_orphan_workspaces(P["wb"], st, compute_size=False, match="光缆")
        check("无关工作区被过滤掉", "2026-01-03-00-00-03" not in {r["name"] for r in b1m})
        check("不相关的工作区不匹配", not match_workspace(P["shell"], st, "光缆"))
        b1a, b2a, _ = list_orphan_workspaces(P["wb"], st, compute_size=False, match="已删自动化")
        check("可按自动化任务名匹配到对应工作区",
              any(r["name"] == "2026-01-06-00-00-06" for r in b1a + b2a),
              f"实际={[r['name'] for r in b1a + b2a]}")

        print("\n== 5) Tier A 只挑已删会话 ==")
        c = Cleaner(st, P["wb"], apply=False)
        c.tier_a()
        keys = {(r["tag"], os.path.basename(r["path"])) for r in c.record}
        check("dead-1 的缓存入选", ("changes-index", "dead-1.json") in keys)
        check("live-1 的缓存未入选", ("changes-index", "live-1.json") not in keys)

        print("\n== 6) B2 需显式确认 + keep 保留 ==")
        c2 = Cleaner(st, P["wb"], apply=False)
        c2.tier_b(want_b1=True, want_b2=True, keep_b2=["2026-01-04-00-00-04"])
        killed = {os.path.basename(r["path"]) for r in c2.record}
        check("被 keep 的 B2 未被处理", "2026-01-04-00-00-04" not in killed)
        check("其余 B2 被处理", "2026-01-05-00-00-05" in killed)

        print("\n== 7) dry-run 绝不落盘 ==")
        check("dry-run 后 shell 目录仍在", os.path.isdir(P["shell"]))
        check("dry-run 后 dead-1.json 仍在",
              os.path.isfile(os.path.join(P["root"], "changes-index", "dead-1.json")))

        print("\n== 8) --fast 不改变分类结果 ==")
        b1f, b2f, _ = list_orphan_workspaces(P["wb"], st, compute_size=False)
        check("--fast 的 B1 数量与完整扫描一致", len(b1f) == len(b1), f"{len(b1f)} vs {len(b1)}")
        check("--fast 的 B2 数量与完整扫描一致", len(b2f) == len(b2), f"{len(b2f)} vs {len(b2)}")

        print("\n== 9) 并行体积计算 ==")
        sizes = dirsize_many([P["file"], P["mem"], P["shell"]])
        check("dirsize_many 与串行结果一致",
              sizes[P["file"]] == dirsize(P["file"]) and sizes[P["mem"]] == dirsize(P["mem"]))
        check("dirsize_many 空输入返回空字典", dirsize_many([]) == {})

        print("\n== 10) 回收站往返 ==")
        victim = os.path.join(base, "trash_me.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("x")
        try:
            send_to_trash(victim)
            check("文件已离开原位", not os.path.exists(victim))
        except Exception as e:
            check("回收站可用", False, f"异常: {str(e)[:80]}")

        print("\n== 11) 隔离区往返（v1.2 新增） ==")
        q = Quarantine(os.path.join(base, "quarantine"))
        qv = os.path.join(base, "q_victim")
        os.makedirs(os.path.join(qv, "sub"))
        with open(os.path.join(qv, "sub", "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        entry = q.put(qv, tag="test", note="单元测试")
        check("隔离区：原路径已消失", not os.path.exists(qv))
        check("隔离区：条目进入清单", len(q.entries()) == 1)
        check("隔离区：内容可访问且保留结构",
              os.path.isfile(os.path.join(q.items_dir, entry["id"], "sub", "a.txt")))
        check("隔离区：记录了原始路径", entry["origPath"] == os.path.abspath(qv))
        done, fail = q.restore(ids=[entry["id"]])
        check("隔离区：还原成功", len(done) == 1 and not fail)
        check("隔离区：内容回到原位",
              os.path.isfile(os.path.join(qv, "sub", "a.txt")))
        check("隔离区：还原后清单清空", len(q.entries()) == 0)

        e2 = q.put(qv, tag="test2")
        done2 = q.purge(older_than_days=0)
        check("隔离区：purge 可清除条目",
              len(done2) == 1 and not os.path.exists(os.path.join(q.items_dir, e2["id"])))

        print("\n== 12) 带隔离区的清理器 ==")
        c3 = Cleaner(st, P["wb"], apply=True, quarantine=os.path.join(base, "quarantine2"))
        check("use_trash() 报告 quarantine 模式", c3.use_trash() == "quarantine")
        c3.tier_b(want_b1=True, want_b2=False)
        moved = [os.path.basename(r["path"]) for r in c3.record]
        check("B1 已被移入隔离区", os.path.isdir(os.path.join(base, "quarantine2", "items"))
              and len(c3.quar.entries()) == len(moved), f"moved={moved}")
        check("隔离区模式下 shell 目录原址已空", not os.path.isdir(P["shell"]))
        check("隔离区模式下含记忆目录仍存在（因未启用 B2）", os.path.isdir(P["mem"]))
        check("存活工作区仍存在", os.path.isdir(P["live"]))
        check("自动化工作区仍存在", os.path.isdir(P["auto"]))

        print("\n== 13) 稳健删除 ==")
        deep = os.path.join(base, "deep")
        os.makedirs(os.path.join(deep, "a", "b"))
        robust_remove(deep)
        check("robust_remove 能删除多级目录", not os.path.exists(deep))
        robust_remove(os.path.join(base, "not-exists-at-all"))
        check("robust_remove 对不存在的路径不报错", True)

    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 54)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
