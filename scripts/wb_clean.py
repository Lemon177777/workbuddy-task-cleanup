#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删任务残留 —— 分级清理器。

默认 **dry-run**（只打印将删内容，不落盘）。真正删除必须显式加 --apply。
B2（含用户成果文件的孤儿工作区）额外要求 --include-b2，双重确认。

用法:
    python wb_clean.py --tiers A,B1,C                # 预览
    python wb_clean.py --tiers A,B1,C --apply        # 执行
    python wb_clean.py --tiers A,B1,C,B2 --include-b2 --apply

绝不触碰: blobs/、sessions/*.json、settings.json、workbuddy.db、mcp.json
以及任何存活会话的工作区目录；logs 保留当天。
纯标准库，无第三方依赖。
"""
import argparse
import os
import shutil
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


class Cleaner:
    def __init__(self, root, wb, apply):
        self.root, self.wb, self.apply = root, wb, apply
        con = sqlite3.connect(f"file:{os.path.join(root, 'workbuddy.db')}?mode=ro", uri=True)
        try:
            rows = list(con.execute("SELECT id,cwd,deleted_at FROM sessions"))
        finally:
            con.close()
        self.deleted = {r[0] for r in rows if r[2] is not None}
        self.live_ids = {r[0] for r in rows if r[2] is None}
        self.live_cwds = {norm(r[1]) for r in rows if r[2] is None and r[1]}
        self.today = time.strftime("%Y-%m-%d")
        self.today_compact = self.today.replace("-", "")
        self.stats = {}
        self.errors = []

    def _kill(self, path, tag):
        if not os.path.exists(path):
            return 0
        sz = dirsize(path)
        if not self.apply:
            self.stats.setdefault(tag, [0, 0])
            self.stats[tag][0] += 1
            self.stats[tag][1] += sz
            return sz
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            self.stats.setdefault(tag, [0, 0])
            self.stats[tag][0] += 1
            self.stats[tag][1] += sz
            return sz
        except Exception as e:  # 被占用/钩子拦截
            self.errors.append((path, str(e)[:90]))
            return 0

    def tier_a(self):
        for sub, isdir in TIER_A:
            d = os.path.join(self.root, sub)
            if not os.path.isdir(d):
                continue
            for name in list(os.listdir(d)):
                key = name[:-5] if (not isdir and name.endswith(".json")) else name
                if key in self.deleted:
                    self._kill(os.path.join(d, name), sub)
        pd = os.path.join(self.root, "projects")
        if os.path.isdir(pd):
            for slug in list(os.listdir(pd)):
                sp = os.path.join(pd, slug)
                if not os.path.isdir(sp):
                    continue
                for f in list(os.listdir(sp)):
                    if f.split(".")[0] in self.deleted:
                        self._kill(os.path.join(sp, f), "projects-files")
                try:
                    if self.apply and os.path.isdir(sp) and not os.listdir(sp):
                        os.rmdir(sp)
                        self.stats.setdefault("projects-dirs", [0, 0])
                        self.stats["projects-dirs"][0] += 1
                except OSError:
                    pass

    def tier_b1(self):
        if not os.path.isdir(self.wb):
            return
        for d in list(os.listdir(self.wb)):
            full = os.path.join(self.wb, d)
            if not os.path.isdir(full) or norm(full) in self.live_cwds:
                continue
            if not [i for i in os.listdir(full) if i != ".workbuddy"]:
                self._kill(full, "B1-shell-workspace")

    def tier_b2(self):
        if not os.path.isdir(self.wb):
            return
        for d in sorted(os.listdir(self.wb)):
            full = os.path.join(self.wb, d)
            if not os.path.isdir(full) or norm(full) in self.live_cwds:
                continue
            if [i for i in os.listdir(full) if i != ".workbuddy"]:
                self._kill(full, "B2-user-workspace")

    def tier_c(self):
        tr = os.path.join(self.root, "traces")
        if os.path.isdir(tr):
            for d in list(os.listdir(tr)):
                self._kill(os.path.join(tr, d), "traces")
        lg = os.path.join(self.root, "logs")
        if os.path.isdir(lg):
            for d in list(os.listdir(lg)):
                full = os.path.join(lg, d)
                if d == "sandbox" and os.path.isdir(full):
                    for sd in list(os.listdir(full)):
                        sf = os.path.join(full, sd)
                        if os.path.isdir(sf) and sd != self.today_compact:
                            self._kill(sf, "logs-sandbox")
                elif os.path.isdir(full) and is_dated_dir(d) and d != self.today:
                    self._kill(full, "logs-dated")

    def verify(self):
        miss = []
        if os.path.isdir(self.wb):
            pass
        con = sqlite3.connect(f"file:{os.path.join(self.root, 'workbuddy.db')}?mode=ro", uri=True)
        try:
            rows = list(con.execute("SELECT cwd FROM sessions WHERE deleted_at IS NULL AND cwd<>''"))
        finally:
            con.close()
        for (cwd,) in rows:
            if not os.path.isdir(cwd):
                miss.append(cwd)
        return miss


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 已删任务残留分级清理")
    ap.add_argument("--tiers", required=True,
                    help="要清理的类别，逗号分隔，可选 A,B1,B2,C")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认仅预览）")
    ap.add_argument("--include-b2", action="store_true",
                    help="确认删除 B2（含用户成果文件的孤儿工作区）")
    ap.add_argument("--workbuddy", default=os.path.expanduser("~/.workbuddy"))
    ap.add_argument("--workspaces", default=os.path.expanduser("~/WorkBuddy"))
    a = ap.parse_args()

    tiers = {t.strip().upper() for t in a.tiers.split(",") if t.strip()}
    bad = tiers - {"A", "B1", "B2", "C"}
    if bad:
        sys.exit(f"[x] 未知类别: {bad}")
    if "B2" in tiers and not a.include_b2:
        sys.exit("[x] B2 含用户成果文件，必须同时加 --include-b2 才允许清理。")

    c = Cleaner(a.workbuddy, a.workspaces, a.apply)
    mode = "执行删除" if a.apply else "预览（未落盘）"
    print("=" * 68)
    print(f"WorkBuddy 残留清理  [{mode}]  类别={','.join(sorted(tiers))}")
    print(f"已删会话 {len(c.deleted)}  |  存活会话 {len(c.live_ids)}")
    print("=" * 68)

    if "A" in tiers:
        c.tier_a()
    if "B1" in tiers:
        c.tier_b1()
    if "B2" in tiers:
        c.tier_b2()
    if "C" in tiers:
        c.tier_c()

    total = 0
    print("\n结果:")
    for tag in sorted(c.stats):
        n, s = c.stats[tag]
        total += s
        print(f"  {tag:22s} {'将删' if not a.apply else '已删'} {n:4d} 项  {human(s)}")
    print(f"  {'合计':22s}            {human(total)}")

    if c.errors:
        print(f"\n跳过(被占用/钩子拦截) {len(c.errors)} 项:")
        for p, e in c.errors[:10]:
            print(f"   {p}  -> {e}")
        print("   提示: safe-delete 钩子可能延迟落盘或被占用，稍后重跑一次即可补齐。")

    miss = c.verify()
    print(f"\n存活会话工作区缺失: {len(miss)}")
    for m in miss:
        print(f"   MISSING  {m}")

    if a.apply:
        print("\n⚠️ 请再跑一次 wb_scan.py 做二次核验（钩子可能延迟落盘，首轮结果未必全清）。")


if __name__ == "__main__":
    main()
