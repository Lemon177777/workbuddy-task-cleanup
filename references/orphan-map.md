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
- **任何被自动化任务引用的目录**（见下节）。
- **任何含 `.workbuddy/memory/` 的工作区**（记忆优先，宁可留着）。
- 当天的 `logs/<今天>/` 与 `logs/sandbox/<今天>/`。

## ⚠️ 第二张权威表：`automations`（v1.1 新增，v1.0 的盲区）

自动化任务的工作目录**不存在于 `sessions` 表**，必须单独查：

```sql
SELECT id, name, status, cwds, deleted_at FROM automations;
```

- `cwds` 是 **JSON 数组**（如 `["C:\\Users\\...\\2026-10-03-18-10-05"]`），不是普通字符串 —— 直接按逗号切会解析失败。
- 必须过滤 `deleted_at IS NULL`，否则会把用户已删的旧任务也算进来（会造成误报）。
- `status` 为 `ACTIVE` 的任务若指向已消失目录，运行时会失败 —— 扫描报告应把它作为**体检项**列出并提示用户。

**为什么要紧**：一个工作区完全可能"没有任何存活会话引用"，却正是某个定时任务的运行目录。
只看 `sessions` 就删，会悄悄打断用户的自动化。检测到引用时一律跳过，并在报告里注明是哪个任务。

同理，`automation_runs.source_cwd` 记录了历史运行的来源目录，可用于交叉核对。

## ⚠️ 工作区记忆：`.workbuddy/memory/`（v1.1 新增，v1.0 的盲区）

工作区目录下的 `.workbuddy/memory/` 存着**项目长期记忆**：

```
<工作区>/.workbuddy/memory/
├── YYYY-MM-DD.md                        # 每日工作日志
└── automations/<automation-id>/memory.md # 自动化任务自己的记忆
```

v1.0 把"只有 `.workbuddy` 的目录"判定为空壳并直接删除 —— 若该目录只含记忆，
就会**连同记忆一起删掉**。判别规则必须改为：

- 有非 `.workbuddy` 文件 **或** `.workbuddy/memory/` 下有文件 → 归入 **B2**（需确认），并列出记忆文件
- 两者皆无 → 才是真·空壳 **B1**

## ⚠️ 三个已知坑

1. **safe-delete 钩子延迟落盘**：本机 `SAFE_DELETE_FAIL_CLOSED` 钩子会把删除转成回收站操作，
   且可能在 Python `rmtree()` 返回**之后**才真正落盘。首次清理后目录可能仍短暂存在 ——
   **必须二次核验**（重跑 `wb_scan.py`），不要仅凭首轮输出下结论。
2. **`logs/sandbox` 不能整目录删**：整目录会失败（`Some operations were aborted`）。
   必须按日期子目录（如 `20261009/`）逐个删，并保留当天。
3. **删除钩子会改写返回码**：部分环境（如本机的 SAFE_DELETE_FAIL_CLOSED 钩子）会**代劳删除**，
   但把 `SHFileOperationW` 的返回值改成非 0（实测 rc=2，而文件确实已进回收站）。
   → 因此**一律以实际效果为准**：调用后检查路径是否消失，而不是看返回值。
   同理，`shutil.rmtree` 也可能"报错但其实成功了"——删除后务必**二次核验**。

## 参考体量（2026-10-11 实测，80 个已删会话）

| 类别 | 体量 |
|---|---|
| A 内部缓存 + 转录 | ≈ 128 MB |
| B1 空壳工作区（36 个） | 16 KB |
| B2 含成果工作区（18 个） | 146.5 MB |
| C traces + logs | ≈ 2.75 GB |
| **合计** | **≈ 3.0 GB** |
