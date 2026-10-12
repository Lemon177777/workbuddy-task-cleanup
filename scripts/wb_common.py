#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 已删任务残留清理 —— 公共逻辑（v1.2）。

wb_scan.py / wb_clean.py / wb_restore.py / wb_selftest.py 共用本模块。
仅使用标准库，Python 3.8+。

设计要点：
  · 三重保护集合（存活会话 cwd / 自动化任务 cwd / 工作区记忆）
  · cwd → 会话·自动化 反向索引，用于回答「这个工作区是谁的」
  · 并行体积计算（dirsize_many），扫描大目录时明显更快
  · 自带隔离区（Quarantine），可精确还原，跨平台行为一致
"""
import ctypes
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

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


def dirsize_many(paths, workers=None):
    """并行计算多个路径的体积。IO 密集，线程池即可见效。

    返回 {原始路径字符串: 字节数}。路径数 < 2 时直接串行，避免线程开销。
    """
    paths = list(paths)
    if not paths:
        return {}
    if len(paths) < 2:
        return {paths[0]: dirsize(paths[0])}
    n = workers or min(8, max(2, os.cpu_count() or 4))
    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            return dict(zip(paths, ex.map(dirsize, paths)))
    except Exception:
        return {p: dirsize(p) for p in paths}


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def is_dated_dir(name):
    return len(name) == 10 and name[4] == "-" and name[7] == "-"


_WS_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")


def is_workspace_dir(name):
    """是否是 WorkBuddy 自动生成的工作区目录名（YYYY-MM-DD-HH-MM-SS）。

    不符合该格式的目录可能来自用户手建，清理时必须更保守 —— 宁可让人多看一眼。
    """
    return bool(_WS_NAME_RE.match(name))


def mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return time.time()


def win_long(p):
    r"""给 Windows 下过长的路径加 \\?\ 前缀，绕开 MAX_PATH 限制。

    孤儿工作区的路径很容易超 260 字符：
      ~/WorkBuddy/<时间戳>/.workbuddy/memory/automations/<uuid>/memory.md
    """
    if os.name != "nt":
        return p
    ap = os.path.abspath(p)
    if len(ap) < 248 or ap.startswith("\\\\?\\"):
        return ap
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def robust_remove(path):
    r"""删除文件或目录；Windows 长路径失败时自动加长路径前缀重试。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        return
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        if os.name == "nt":
            lp = win_long(path)
            if lp != path:
                if os.path.isdir(lp):
                    shutil.rmtree(lp)
                elif os.path.exists(lp):
                    os.remove(lp)
                return
        raise


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
    """跨平台移入系统回收站。失败抛 OSError。仅用标准库，不调用外部命令。

    注意：部分环境存在「删除钩子」，会代劳删除但返回被改写过的错误码。
    因此**以实际效果为准**（操作后路径是否消失），不单纯依赖返回值。
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return

    if os.name == "nt":
        FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
        src = win_long(path)
        buf = ctypes.create_unicode_buffer(src + "\0")   # 双 NUL 结尾；buf 须存活到调用结束
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
    """从 workbuddy.db 读取会话与自动化信息，给出所有保护集合与反向索引。"""

    def __init__(self, root):
        self.root = root
        db = os.path.join(root, "workbuddy.db")
        if not os.path.isfile(db):
            raise SystemExit(f"[x] 找不到数据库: {db}")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            self.rows = list(con.execute("SELECT id,cwd,title,deleted_at FROM sessions"))
            self.all_automations = self._load_automations(con)
        finally:
            con.close()

        self.deleted = {r[0] for r in self.rows if r[3] is not None}
        self.live_ids = {r[0] for r in self.rows if r[3] is None}
        self.live = [(r[0], r[1], r[2]) for r in self.rows if r[3] is None]
        self.live_cwds = {norm(r[1]) for r in self.live if r[1]}

        # 存活自动化（已删除的旧任务不算，否则会误报 / 误保护）
        self.automations = [a for a in self.all_automations if not a["deleted"]]

        # ⚠️ P0：被自动化任务引用的目录 = 受保护，绝不删除
        self.automation_cwds = set()
        self.broken_automations = []       # 引用了已不存在目录的、仍存活的自动化
        for a in self.automations:
            for p in a["cwds"]:
                self.automation_cwds.add(norm(p))
                if not os.path.isdir(p):
                    self.broken_automations.append({"name": a["name"], "status": a["status"], "cwd": p})

        self.protected_cwds = self.live_cwds | self.automation_cwds

        # ---- 反向索引：cwd → 谁用过它（含已删除的，仅供报告展示） ----
        self.sessions_by_cwd = {}
        for sid, cwd, title, del_at in self.rows:
            if cwd:
                self.sessions_by_cwd.setdefault(norm(cwd), []).append(
                    {"id": sid, "title": title or "", "deleted": del_at is not None})

        self.automations_by_cwd = {}
        for a in self.all_automations:
            for c in a["cwds"]:
                self.automations_by_cwd.setdefault(norm(c), []).append(
                    {"name": a["name"], "status": a["status"], "deleted": a["deleted"]})

    @staticmethod
    def _load_automations(con):
        """读取**全部**自动化任务（含已删除），用 deleted 标记区分。

        ⚠️ 已删除的任务不参与保护集合，但会在「关联线索」里展示，
        帮助用户判断某个孤儿工作区到底是干什么用的。
        """
        out = []
        try:
            cols = {c[1] for c in con.execute("PRAGMA table_info('automations')")}
            if not cols:
                return out
            sel = "id,name,status,cwds"
            for extra in ("deleted_at", "last_run_at", "next_run_at", "valid_until"):
                if extra in cols:
                    sel += "," + extra
            for row in con.execute(f"SELECT {sel} FROM automations"):
                d = dict(zip(sel.split(","), row))
                try:
                    cwds = json.loads(d.get("cwds") or "[]")
                except Exception:
                    cwds = [d["cwds"]] if d.get("cwds") else []
                if isinstance(cwds, str):
                    cwds = [cwds]
                out.append({
                    "id": d["id"], "name": d["name"], "status": d["status"],
                    "cwds": [c for c in cwds if c],
                    "deleted": d.get("deleted_at") is not None,
                    "last_run_at": d.get("last_run_at"),
                    "next_run_at": d.get("next_run_at"),
                    "valid_until": d.get("valid_until"),
                })
        except sqlite3.Error:
            pass
        return out

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


def workdir_context(path, state):
    """回答「这个工作区是谁的」—— B2 逐个确认时最需要的上下文。

    返回关联的会话与自动化任务统计。含**已删除**的条目，
    因为它们恰恰能说明这个孤儿工作区曾经属于哪个项目。
    """
    key = norm(path)
    sess = state.sessions_by_cwd.get(key, [])
    auto = state.automations_by_cwd.get(key, [])
    del_sess = [s for s in sess if s["deleted"]]
    live_sess = [s for s in sess if not s["deleted"]]
    titles = [s["title"] for s in del_sess if s["title"]]
    live_titles = [s["title"] for s in live_sess if s["title"]]
    return {
        "sessions_total": len(sess),
        "sessions_deleted": len(del_sess),
        "sessions_live": len(live_sess),
        "titles": titles[:6],
        "titles_total": len(titles),
        "live_titles": live_titles[:3],
        "automations": auto,
        "automations_deleted": sum(1 for a in auto if a["deleted"]),
        "automations_live": sum(1 for a in auto if not a["deleted"]),
    }


def match_workspace(path, state, pattern):
    """按工作区名、关联会话标题、关联自动化任务名做子串匹配（大小写不敏感）。"""
    if not pattern:
        return True
    low = pattern.lower()
    if low in os.path.basename(path).lower():
        return True
    for s in state.sessions_by_cwd.get(norm(path), []):
        if low in (s["title"] or "").lower():
            return True
    for a in state.automations_by_cwd.get(norm(path), []):
        if low in (a["name"] or "").lower():
            return True
    return False


def _protect_reasons(path, state):
    key = norm(path)
    r = []
    if key in state.live_cwds:
        r.append("存活会话工作区")
    hits = [a["name"] for a in state.automations_by_cwd.get(key, []) if not a["deleted"]]
    if hits:
        r.append("被自动化任务引用：" + "、".join(hits))
    return r


def list_orphan_workspaces(wb, state, compute_size=True, match=None):
    """分类孤儿工作区。返回 (b1, b2, protected)。

    b1: 真·空壳（无用户文件、无工作区记忆）→ 可安全清理
    b2: 含用户文件 或 含工作区记忆 → 必须逐个确认（附关联线索 ctx）
    protected: 被存活会话或自动化任务引用 → 绝不删除

    compute_size=False 时跳过体积计算（--fast），数量统计不受影响。
    """
    b1, b2, protected = [], [], []
    if not os.path.isdir(wb):
        return b1, b2, protected

    raw = []
    for d in sorted(os.listdir(wb)):
        full = os.path.join(wb, d)
        if not os.path.isdir(full):
            continue
        if state.is_protected(full):
            raw.append(("protected", full, d, None))
            continue
        if match and not match_workspace(full, state, match):
            continue
        user_items = [i for i in os.listdir(full) if i != ".workbuddy"]
        mem = workdir_memory_files(full)
        if not user_items and not mem and is_workspace_dir(d):
            raw.append(("b1", full, d, None))
        else:
            # 含内容 -> B2；非标准命名（可能是用户手建）即使为空也归 B2，多要一次人工确认
            raw.append(("b2", full, d, {"has_files": bool(user_items), "memory_files": mem,
                                        "odd": not (user_items or mem)}))

    sizes = dirsize_many([r[1] for r in raw]) if compute_size else {}

    for kind, full, d, extra in raw:
        sz = sizes.get(full, 0)
        if kind == "protected":
            protected.append({"name": d, "size": sz, "reasons": _protect_reasons(full, state)})
        elif kind == "b1":
            b1.append({"name": d, "abs": full, "size": sz, "has_files": False, "memory_files": []})
        else:
            files = []
            for r, _ds, fs in os.walk(full):
                if ".workbuddy" in r.split(os.sep):
                    continue
                for f in fs:
                    fp = os.path.join(r, f)
                    files.append({"rel": os.path.relpath(fp, full), "size": dirsize(fp)})
            files.sort(key=lambda x: -x["size"])
            b2.append({"name": d, "abs": full, "size": sz,
                       "has_files": extra["has_files"], "memory_files": extra["memory_files"],
                       "files": files, "ctx": workdir_context(full, state),
                       "odd": extra.get("odd", False)})
    return b1, b2, protected


# ---------------------------------------------------------------- 隔离区

class Quarantine:
    """自建隔离区：把待清理内容 move 进来，可精确还原到原路径。

    相比系统回收站的优势：
      · 跨平台行为完全一致，不依赖 Shell API / freedesktop 实现
      · 能精确记录「从哪来」，还原时不必猜
      · 可以按时间批量清理到期项
    """

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.items_dir = os.path.join(self.root, "items")
        self.manifest_path = os.path.join(self.root, "manifest.json")

    def _load(self):
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict) and isinstance(d.get("entries"), list):
                    return d
        except Exception:
            pass
        return {"version": 1, "entries": []}

    def _save(self, data):
        os.makedirs(self.root, exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)

    def put(self, path, tag="", note=""):
        """把 path 移入隔离区。返回记录条目。"""
        os.makedirs(self.items_dir, exist_ok=True)
        sz = dirsize(path)
        base = time.strftime("%Y%m%d-%H%M%S") + "-" + os.path.basename(path.rstrip("\\/"))[:48]
        eid, i = base, 1
        while os.path.exists(os.path.join(self.items_dir, eid)):
            eid = f"{base}#{i}"
            i += 1
        shutil.move(path, os.path.join(self.items_dir, eid))
        entry = {"id": eid, "origPath": path, "size": sz, "tag": tag, "note": note,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        data = self._load()
        data["entries"].append(entry)
        self._save(data)
        return entry

    def entries(self):
        return self._load()["entries"]

    def total_size(self):
        return sum(e.get("size", 0) for e in self.entries())

    def restore(self, ids=None, on_conflict="rename"):
        """还原到原路径。ids=None 表示全部。返回 (成功列表, 失败列表)。

        被还原的条目会从清单中移除；失败或跳过的会保留，便于稍后重试。
        """
        data = self._load()
        done, fail, keep = [], [], []
        for e in data["entries"]:
            if ids and e["id"] not in ids:
                keep.append(e)
                continue
            src = os.path.join(self.items_dir, e["id"])
            dst = e["origPath"]
            try:
                if not os.path.exists(src):
                    fail.append((e, "隔离区内容已不存在"))
                    continue
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if os.path.exists(dst):
                    if on_conflict == "skip":
                        fail.append((e, "目标已存在，按 --on-conflict skip 跳过"))
                        keep.append(e)
                        continue
                    dst = _unique_dest(parent or ".", os.path.basename(dst))
                shutil.move(src, dst)
                done.append(e)
            except Exception as ex:
                fail.append((e, str(ex)[:100]))
                keep.append(e)
        data["entries"] = keep
        self._save(data)
        return done, fail

    def purge(self, older_than_days=None):
        """彻底删除隔离区内容（不可恢复）。返回被清除的条目列表。"""
        data = self._load()
        now = time.time()
        done, keep = [], []
        for e in data["entries"]:
            if older_than_days is not None:
                try:
                    t = time.mktime(time.strptime(e["at"], "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    t = now
                if (now - t) < older_than_days * 86400:
                    keep.append(e)
                    continue
            try:
                p = os.path.join(self.items_dir, e["id"])
                if os.path.exists(p):
                    robust_remove(p)
                done.append(e)
            except Exception:
                keep.append(e)
        data["entries"] = keep
        self._save(data)
        return done


# ---------------------------------------------------------------- 清理器

# 绝不触碰的顶层项
NEVER_TOUCH = {"blobs", "workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm",
               "settings.json", "mcp.json", "sessions", "connectors", "binaries",
               "skills", "plugins", "storage", "local_storage", "security", "projects-keep"}

TIER_A_SUBS = (("tasks", True), ("artifact-index", False), ("changes-index", False),
               ("changes-detail", True), ("file-history", True), ("assistant-display", False))


class Cleaner:
    def __init__(self, state, wb, apply=False, to_trash=True, older_than=None, log_path=None,
                 quarantine=None, match=None, compute_size=True):
        self.st = state
        self.wb = wb
        self.apply = apply
        self.to_trash = to_trash
        self.older_than = older_than
        self.log_path = log_path
        self.quar = Quarantine(quarantine) if quarantine else None
        self.match = match
        self.compute_size = compute_size
        self.today = time.strftime("%Y-%m-%d")
        self.today_compact = self.today.replace("-", "")
        self.stats = {}
        self.errors = []
        self.record = []
        self.protected_seen = []

    def use_trash(self):
        """实际使用的删除方式：quarantine > trash > delete。"""
        if self.quar:
            return "quarantine"
        return "trash" if self.to_trash else "delete"

    def _too_new(self, path):
        if not self.older_than:
            return False
        return (time.time() - mtime(path)) < self.older_than * 86400

    def _kill(self, path, tag, note=""):
        if not os.path.exists(path):
            return 0
        if self._too_new(path):
            self.stats.setdefault(tag + "(跳过:太新)", [0, 0])
            self.stats[tag + "(跳过:太新)"][0] += 1
            return 0

        sz = dirsize(path)
        mode = "preview" if not self.apply else self.use_trash()
        self.stats.setdefault(tag, [0, 0])
        self.stats[tag][0] += 1
        self.stats[tag][1] += sz
        self.record.append({"tag": tag, "path": path, "size": sz, "mode": mode, "note": note})
        if not self.apply:
            return sz

        try:
            if self.quar:
                self.quar.put(path, tag=tag, note=note)
            elif self.to_trash:
                send_to_trash(path)
            else:
                robust_remove(path)
            return sz
        except Exception as e:  # 被占用 / 钩子拦截 / 长路径失败
            self.errors.append((path, str(e)[:120]))
            return 0

    def tier_a(self):
        for sub, isdir in TIER_A_SUBS:
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

    def tier_b(self, want_b1=True, want_b2=False, keep_b2=()):
        b1, b2, protected = list_orphan_workspaces(
            self.wb, self.st, compute_size=self.compute_size, match=self.match)
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

    def tier_c(self):
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

    def write_log(self):
        if not (self.apply and self.log_path):
            return
        payload = {
            "schemaVersion": 1,
            "appliedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.use_trash(),
            "olderThanDays": self.older_than,
            "match": self.match,
            "totals": {k: {"count": v[0], "bytes": v[1]} for k, v in self.stats.items()},
            "deleted": self.record,
            "errors": [{"path": p, "error": e} for p, e in self.errors],
        }
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
