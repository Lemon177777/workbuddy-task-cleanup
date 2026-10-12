#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删任务残留清理 —— 公共逻辑。

两个脚本（wb_scan.py / wb_clean.py）共用本模块。
仅使用标准库，Python 3.8+。
"""
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import time

# ---------------------------------------------------------------- 基础工具

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


def mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return time.time()


# ---------------------------------------------------------------- 回收站

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_void_p),          # 用 c_void_p 持有自建缓冲区，避免临时对象被回收
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


def _unique_dest(folder, name):
    dest = os.path.join(folder, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = os.path.join(folder, f"{stem} {i}{ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def send_to_trash(path):
    """跨平台移入回收站。失败抛 OSError。仅用标准库，不调用外部命令。

    注意：部分环境存在「删除钩子」，会代劳删除但返回被改写过的错误码。
    因此**以实际效果为准**（操作后路径是否消失），不单纯依赖返回值。
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return

    if os.name == "nt":
        FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
        buf = ctypes.create_unicode_buffer(path + "\0")   # 双 NUL 结尾；buf 须存活到调用结束
        op = _SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.cast(buf, ctypes.c_void_p).value
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 or not os.path.exists(path):
            return
        raise OSError(f"SHFileOperationW 失败 rc={rc}，且路径仍存在")

    if sys.platform == "darwin":
        trash = os.path.expanduser("~/.Trash")
        os.makedirs(trash, exist_ok=True)
        shutil.move(path, _unique_dest(trash, os.path.basename(path)))
        return

    # freedesktop 规范
    base = os.path.expanduser("~/.local/share/Trash")
    files_dir, info_dir = os.path.join(base, "files"), os.path.join(base, "info")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(info_dir, exist_ok=True)
    dest = _unique_dest(files_dir, os.path.basename(path))
    shutil.move(path, dest)
    with open(os.path.join(info_dir, os.path.basename(dest) + ".trashinfo"), "w",
              encoding="utf-8") as f:
        f.write(f"[Trash Info]\nPath={path}\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")


# ---------------------------------------------------------------- 状态加载

class State:
    """从 workbuddy.db 读取会话与自动化信息，给出所有保护集合。"""

    def __init__(self, root):
        self.root = root
        db = os.path.join(root, "workbuddy.db")
        if not os.path.isfile(db):
            raise SystemExit(f"[x] 找不到数据库: {db}")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            self.rows = list(con.execute("SELECT id,cwd,title,deleted_at FROM sessions"))
            self.automations = self._load_automations(con)
            self.trust_keys = self._load_trust(con)
        finally:
            con.close()

        self.deleted = {r[0] for r in self.rows if r[3] is not None}
        self.live_ids = {r[0] for r in self.rows if r[3] is None}
        self.live = [(r[0], r[1], r[2]) for r in self.rows if r[3] is None]
        self.live_cwds = {norm(r[1]) for r in self.live if r[1]}

        # ⚠️ P0：被自动化任务引用的目录 = 受保护，绝不删除
        self.automation_cwds = set()
        self.broken_automations = []       # 引用了已不存在目录的自动化
        for a in self.automations:
            for p in a["cwds"]:
                self.automation_cwds.add(norm(p))
                if not os.path.isdir(p):
                    self.broken_automations.append({"name": a["name"], "status": a["status"], "cwd": p})

        self.protected_cwds = self.live_cwds | self.automation_cwds

    @staticmethod
    def _load_automations(con):
        out = []
        try:
            cols = {c[1] for c in con.execute("PRAGMA table_info('automations')")}
            if not cols:
                return out
            sel = "id,name,status,cwds"
            if "deleted_at" in cols:
                sel += ",deleted_at"
            for row in con.execute(f"SELECT {sel} FROM automations"):
                d = dict(zip(sel.split(","), row))
                if d.get("deleted_at") is not None:
                    continue
                try:
                    cwds = json.loads(d.get("cwds") or "[]")
                except Exception:
                    cwds = [d["cwds"]] if d.get("cwds") else []
                if isinstance(cwds, str):
                    cwds = [cwds]
                out.append({"id": d["id"], "name": d["name"], "status": d["status"],
                            "cwds": [c for c in cwds if c]})
        except sqlite3.Error:
            pass
        return out

    @staticmethod
    def _load_trust(con):
        try:
            return {r[0] for r in con.execute("SELECT workspace_key FROM workspace_trust_decisions")}
        except sqlite3.Error:
            return set()

    def is_protected(self, path):
        return norm(path) in self.protected_cwds


# ---------------------------------------------------------------- 工作区检查

def workdir_memory_files(wd):
    """返回工作区内 .workbuddy/memory/ 下的所有文件（相对路径）。"""
    base = os.path.join(wd, ".workbuddy", "memory")
    if not os.path.isdir(base):
        return []
    out = []
    for r, _ds, fs in os.walk(base):
        for f in fs:
            out.append(os.path.relpath(os.path.join(r, f), wd))
    return sorted(out)


def list_orphan_workspaces(wb, state):
    """分类孤儿工作区。返回 (b1, b2, protected)。

    b1: 真·空壳（无用户文件、无工作区记忆）→ 可安全清理
    b2: 含用户文件 或 含工作区记忆 → 必须逐个确认
    protected: 被存活会话或自动化任务引用 → 绝不删除
    """
    b1, b2, protected = [], [], []
    if not os.path.isdir(wb):
        return b1, b2, protected

    for d in sorted(os.listdir(wb)):
        full = os.path.join(wb, d)
        if not os.path.isdir(full):
            continue
        if state.is_protected(full):
            protected.append({"name": d, "size": dirsize(full),
                              "reasons": _protect_reasons(full, state)})
            continue

        user_items = [i for i in os.listdir(full) if i != ".workbuddy"]
        mem = workdir_memory_files(full)
        sz = dirsize(full)
        rec = {"name": d, "abs": full, "size": sz,
               "has_files": bool(user_items), "memory_files": mem}

        if not user_items and not mem:
            b1.append(rec)
        else:
            files = []
            for r, _ds, fs in os.walk(full):
                if ".workbuddy" in r.split(os.sep):
                    continue
                for f in fs:
                    fp = os.path.join(r, f)
                    files.append({"rel": os.path.relpath(fp, full), "size": dirsize(fp)})
            files.sort(key=lambda x: -x["size"])
            rec["files"] = files
            b2.append(rec)
    return b1, b2, protected


def _protect_reasons(path, state):
    r = []
    if norm(path) in state.live_cwds:
        r.append("存活会话工作区")
    hits = [a["name"] for a in state.automations if norm(path) in {norm(c) for c in a["cwds"]}]
    if hits:
        r.append("被自动化任务引用：" + "、".join(hits))
    return r


# ---------------------------------------------------------------- 清理器

# 绝不触碰的顶层项
NEVER_TOUCH = {"blobs", "workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm",
               "settings.json", "mcp.json", "sessions", "connectors", "binaries",
               "skills", "plugins", "storage", "local_storage", "security", "projects-keep"}


class Cleaner:
    def __init__(self, state, wb, apply=False, to_trash=False, older_than=None, log_path=None):
        self.st = state
        self.wb = wb
        self.apply = apply
        self.to_trash = to_trash
        self.older_than = older_than           # 天；仅删修改时间更早的
        self.log_path = log_path
        self.today = time.strftime("%Y-%m-%d")
        self.today_compact = self.today.replace("-", "")
        self.stats = {}
        self.errors = []
        self.record = []                       # 删除留痕

    # ---- 内部：年龄过滤
    def _too_new(self, path):
        if not self.older_than:
            return False
        return (time.time() - mtime(path)) < self.older_than * 86400

    # ---- 内部：单次删除
    def _kill(self, path, tag, note=""):
        if not os.path.exists(path):
            return 0
        if self._too_new(path):
            self.stats.setdefault(tag + "(跳过:太新)", [0, 0])
            self.stats[tag + "(跳过:太新)"][0] += 1
            return 0

        sz = dirsize(path)
        mode = "preview" if not self.apply else ("trash" if self.to_trash else "delete")
        self.stats.setdefault(tag, [0, 0])
        self.stats[tag][0] += 1
        self.stats[tag][1] += sz
        self.record.append({"tag": tag, "path": path, "size": sz, "mode": mode, "note": note})
        if not self.apply:
            return sz

        try:
            if self.to_trash:
                send_to_trash(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return sz
        except Exception as e:
            self.errors.append((path, str(e)[:120]))
            return 0

    # ---- Tier A
    def tier_a(self):
        for sub, isdir in (("tasks", True), ("artifact-index", False), ("changes-index", False),
                           ("changes-detail", True), ("file-history", True), ("assistant-display", False)):
            if sub in NEVER_TOUCH:
                continue
            d = os.path.join(self.st.root, sub)
            if not os.path.isdir(d):
                continue
            for name in list(os.listdir(d)):
                key = name[:-5] if (not isdir and name.endswith(".json")) else name
                if key in self.st.deleted:
                    self._kill(os.path.join(d, name), sub)

        pd = os.path.join(self.st.root, "projects")
        if os.path.isdir(pd):
            for slug in list(os.listdir(pd)):
                sp = os.path.join(pd, slug)
                if not os.path.isdir(sp):
                    continue
                for f in list(os.listdir(sp)):
                    if f.split(".")[0] in self.st.deleted:
                        self._kill(os.path.join(sp, f), "projects-files")
                try:
                    if self.apply and os.path.isdir(sp) and not os.listdir(sp):
                        os.rmdir(sp)
                        self.stats.setdefault("projects-dirs", [0, 0])
                        self.stats["projects-dirs"][0] += 1
                except OSError:
                    pass

    # ---- Tier B
    def tier_b(self, want_b1=True, want_b2=False, keep_b2=()):
        b1, b2, protected = list_orphan_workspaces(self.wb, self.st)
        self.protected_seen = protected
        if want_b1:
            for r in b1:
                self._kill(r["abs"], "B1-shell")
        if want_b2:
            keep = {k.strip() for k in keep_b2 if k.strip()}
            for r in b2:
                if r["name"] in keep:
                    self.stats.setdefault("B2-保留", [0, 0])
                    self.stats["B2-保留"][0] += 1
                    continue
                note = ("含用户文件" if r["has_files"] else "") + \
                       ("/含工作区记忆" if r["memory_files"] else "")
                self._kill(r["abs"], "B2-user", note.strip("/"))

    # ---- Tier C
    def tier_c(self, keep_active=True):
        tr = os.path.join(self.st.root, "traces")
        if os.path.isdir(tr):
            for d in list(os.listdir(tr)):
                self._kill(os.path.join(tr, d), "traces")
        lg = os.path.join(self.st.root, "logs")
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

    # ---- 留痕
    def write_log(self):
        if not (self.apply and self.log_path):
            return
        payload = {
            "appliedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "trash" if self.to_trash else "delete",
            "olderThanDays": self.older_than,
            "totals": {k: {"count": v[0], "bytes": v[1]} for k, v in self.stats.items()},
            "deleted": self.record,
            "errors": [{"path": p, "error": e} for p, e in self.errors],
        }
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
