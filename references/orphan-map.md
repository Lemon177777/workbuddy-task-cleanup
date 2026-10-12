# WorkBuddy 数据目录 · 已删任务残留地图

## 权威判定依据

`~/.workbuddy/workbuddy.db`（SQLite）→ `sessions` 表：

| 字段 | 含义 |
|---|---|
| `id` | 会话 / 任务 id（uuid 或雪花 id） |
| `cwd` | 该会话的工作区目录 |
| `title` | 标题 |
| `deleted_at` | **NULL = 存活；有值 = 已删除** |

```python
con = sqlite3.connect("file:.../workbuddy.db?mode=ro", uri=True)
deleted = {r[0] for r in con.execute("SELECT id FROM sessions WHERE deleted_at IS NOT NULL")}
live_cwds = {norm(r[0]) for r in con.execute("SELECT cwd FROM sessions WHERE deleted_at IS NULL AND cwd<>''")}
```

> **务必只读打开**（`mode=ro`）。WorkBuddy 正在运行时 DB 有 WAL，写锁会冲突。

## 孤儿位置（按会话 id 命名）

| 路径 | 内容 | 说明 |
|---|---|---|
| `~/.workbuddy/tasks/<id>/` | TaskCreate 任务清单 | 小 |
| `~/.workbuddy/artifact-index/<id>.json` | 产物索引 | 小 |
| `~/.workbuddy/changes-index/<id>.json` | 改动索引 | 小 |
| `~/.workbuddy/changes-detail/<id>/` | 改动明细 | **可达数十 MB** |
| `~/.workbuddy/file-history/<id>/` | 文件历史版本 | 数 MB |
| `~/.workbuddy/assistant-display/<id>.json` | 展示态快照 | 极小 |
| `~/.workbuddy/projects/<cwd-slug>/<id>.jsonl` | **会话完整转录** | 与 `.file-rollback.ndjson`、`.meta.json` 同组；可达上百 MB |

判别规则：取文件名（`.json` 去后缀 / 目录名 / `projects` 下取 `.` 前部分）→ 若在 `deleted` 集合内即为孤儿。

## 孤儿位置（按 cwd 命名）

`~/WorkBuddy/<YYYY-MM-DD-HH-MM-SS>/` —— 每次新建对话自动生成的 playground 工作区。
若其绝对路径（`normcase(normpath(...))`）不在 `live_cwds` 中，即为孤儿。

> ⚠️ **这里可能装着用户自己的成果文件**（报告、源码、图片）。删前必须逐个列清单。

## 诊断类（非严格"已删任务"，但体量大）

| 路径 | 说明 |
|---|---|
| `~/.workbuddy/traces/<pid>/trace_*.json` | 运行追踪，可数 GB |
| `~/.workbuddy/logs/<日期>/*.log` | 按天日志 |
| `~/.workbuddy/logs/sandbox/<YYYYMMDD>/` | 沙箱日志，常 1 GB+ |

## 🚫 绝不触碰

- `~/.workbuddy/blobs/` —— 内容寻址存储，**存活任务也在引用**，删除会破坏数据。
- `~/.workbuddy/workbuddy.db`（及 `-wal`/`-shm`）、`settings.json`、`mcp.json`、`sessions/*.json`。
- 任何存活会话的 `cwd` 目录。
- 当天的 `logs/<今天>/` 与 `logs/sandbox/<今天>/`。

## ⚠️ 两个已知坑

1. **safe-delete 钩子延迟落盘**：本机 `SAFE_DELETE_FAIL_CLOSED` 钩子会把删除转成回收站操作，
   且可能在 Python `rmtree()` 返回**之后**才真正落盘。首次清理后目录可能仍短暂存在 ——
   **必须二次核验**（重跑 `wb_scan.py`），不要仅凭首轮输出下结论。
2. **`logs/sandbox` 不能整目录删**：整目录会失败（`Some operations were aborted`）。
   必须按日期子目录（如 `20261009/`）逐个删，并保留当天。

## 参考体量（2026-10-11 实测，80 个已删会话）

| 类别 | 体量 |
|---|---|
| A 内部缓存 + 转录 | ≈ 128 MB |
| B1 空壳工作区（36 个） | 16 KB |
| B2 含成果工作区（18 个） | 146.5 MB |
| C traces + logs | ≈ 2.75 GB |
| **合计** | **≈ 3.0 GB** |
