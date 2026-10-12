---
name: workbuddy-task-cleanup
display_name: "WorkBuddy 任务垃圾清理"
display_name_en: "WorkBuddy Task Junk Cleanup"
description: "清理 WorkBuddy 自身数据目录中「已删除任务」遗留的孤儿文件。当用户说「清理 workbuddy 垃圾」「清理已删除任务的残留」「WorkBuddy 占空间太大」「清理会话残留 / 缓存 / 日志」「clean up WorkBuddy junk」时使用。基于 workbuddy.db 的 sessions.deleted_at 精确区分已删/存活会话，再按会话 id 与工作区路径定位孤儿，分级（A 内部缓存与转录 / B1 空壳工作区 / B2 含成果文件的孤儿工作区 / C 诊断日志与追踪）盘点并清理，全程先只读扫描、呈现报告、取得用户确认后才动手，绝不误删存活会话数据。"
description_zh: "清理 WorkBuddy 中已删除任务的遗留垃圾文件。依据 workbuddy.db 的 deleted_at 字段精确定位已删会话，分四级盘点（内部缓存与转录、空壳工作区、含成果文件的孤儿工作区、诊断日志与追踪），先只读扫描出报告，用户确认后再清理。"
description_en: "Clean up orphan files left behind by deleted tasks in WorkBuddy's own data directories. Uses sessions.deleted_at in workbuddy.db to separate deleted from live sessions, then locates orphans by session id and workspace path across four tiers (internal caches & transcripts, empty shell workspaces, orphan workspaces containing user artifacts, diagnostic logs & traces). Scan is read-only first; deletion only after explicit user confirmation."
agent_created: true
version: 1.0.1
tags: [workbuddy, cleanup, 清理, 垃圾清理, 磁盘空间, deleted-task, orphan, cache, logs, traces, 缓存, 日志, 残留, 会话, session, maintenance, 维护]
---

# WorkBuddy 任务垃圾清理

> **适合场景**：WorkBuddy 占用空间过大 · 删掉的对话还占磁盘 · 清理残留缓存/日志/追踪 · 定期维护

**执行手册**：严格按 Step 0 → Step 5 顺序执行。**Step 3 的用户确认是硬门槛，不可跳过。**

> **路径约定**：`<SKILL_DIR>` 指本 SKILL.md 所在目录。
> **Python**：用本机可用的 Python 3 即可 —— Windows 常见为托管路径 `%USERPROFILE%\.workbuddy\binaries\python\versions\<ver>\python.exe`，或直接 `python` / `py`；macOS / Linux 用 `python3`。两个脚本**仅依赖标准库**，Python 3.8+ 均可运行，无需安装任何第三方包。

---

## 核心原理（先理解，再动手）

「已删除任务」的权威判定依据是 **`~/.workbuddy/workbuddy.db` → `sessions` 表 → `deleted_at` 字段**：

- `deleted_at IS NULL` → **存活会话**，其数据一律不动
- `deleted_at` 有值 → **已删除任务**，它在磁盘上的残留即"垃圾"

不要凭目录时间、目录是否为空、或侧边栏列表来判断 —— 侧边栏只是快照，且可能不含隐藏的后台任务。完整的地图与坑位见 `references/orphan-map.md`。

四级分类：

| 级别 | 内容 | 风险 |
|---|---|---|
| **A** | 内部缓存 + 会话转录（`tasks/`、`artifact-index/`、`changes-index/`、`changes-detail/`、`file-history/`、`assistant-display/`、`projects/`） | 低，纯内部数据 |
| **B1** | 只剩 `.workbuddy` 的空壳孤儿工作区 `~/WorkBuddy/<ts>` | 低 |
| **B2** | **含用户成果文件**的孤儿工作区 | **高，须逐个确认** |
| **C** | 诊断：`traces/`、`logs/<日期>/`、`logs/sandbox/` | 中，删后失去历史排障线索 |

---

## Step 0 · 定位与前置检查

1. 确认数据根目录存在：
   ```bash
   ls -la "$HOME/.workbuddy/workbuddy.db" "$HOME/WorkBuddy" 2>/dev/null
   ```
2. **只读访问 DB**（脚本已内置 `mode=ro`）。**绝不要**写 DB。
3. 记下当前是哪一次会话在运行，避免误伤自己所在的工作区（脚本用 `live_cwds` 自动排除）。

## Step 1 · 只读盘点（不改动任何文件）

```bash
"<PYTHON>" "<SKILL_DIR>/scripts/wb_scan.py" --json ~/wb_cleanup_report.json
```

输出分四级的数量、体积，以及 **B2 每个目录的完整文件清单**、**存活会话工作区完整性**。这一步只读，可放心执行。

## Step 2 · 分类与呈现

把盘点结果整理成中文报告呈现给用户，至少包含：

- 会话总数 / 已删除数 / 存活数
- A / B1 / B2 / C 各级的数量与体积
- **C 级说明**：属诊断数据，删了会失去历史排障线索 —— 让用户知情
- **B2 逐个列出目录名 + 体积 + 文件清单**，并明确告知"这是你自己的成果文件，目前仅存于此"

## Step 3 · 取得确认（硬门槛 ⚠️）

**未得到用户明确选择前，绝不执行任何删除。**

- 用选择题让用户勾选清理范围（A / B1 / B2 / C，可多选）与删除方式（回收站 / 永久删除）。
- **B2 必须单独二次确认**：先逐个列文件，再问"仍要永久删除？"，因为这是用户的成果而不仅是缓存。
- 若用户选择永久删除，用**加粗**明确警告：
  > ⚠️ **此操作非常危险，可能导致不可逆的数据丢失！**
  并列出受影响的目录与文件。

## Step 4 · 执行清理

```bash
# 预览（默认 dry-run，不落盘）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C

# 执行
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C --apply

# 含 B2（必须额外 --include-b2）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C,B2 --include-b2 --apply
```

- 脚本自动跳过：存活会话工作区、`blobs/`、`settings.json`、`workbuddy.db`、`mcp.json`、当天日志与当天 sandbox。
- 若用户选"移入回收站"而非永久删除，脚本走 OS 回收站机制（本机 safe-delete 钩子天然如此）；若用户明确要永久删除，直接用脚本删除。
- 遇到 `[safe-delete]` 报错属正常（被占用/钩子拦截），记录下来稍后重跑补齐即可。

## Step 5 · 二次核验（必做）

```bash
"<PYTHON>" "<SKILL_DIR>/scripts/wb_scan.py"
```

**必须重跑一次盘点**，因为 safe-delete 钩子可能**延迟落盘**（首轮 `rmtree` 返回后目录可能还在）。确认：

- A / B1 / C（及 B2，若已授权）孤儿计数归 **0**
- **存活会话工作区缺失 = 0**（这是最重要的安全指标）

若仍有残留，重跑 Step 4 对应类别一次。

---

## 铁律

1. **先扫描、后呈现、再确认、末执行** —— 顺序不可颠倒。
2. **不动 DB**。只读打开，绝不写。
3. **不碰 `blobs/`** —— 存活任务也在引用。
4. **B2 必须逐个列文件 + 二次确认**，因为那是用户的成果文件，不是缓存。
5. **删完必须二次核验**，并报告"存活会话工作区缺失 0"。
6. 保留当天的 `logs/<今天>/` 与 `logs/sandbox/<今天>/`。
7. 若用户要求"永久删除"，先加粗警告并列出受影响文件路径，取得明确同意后才执行。

细节与体量参考见 `references/orphan-map.md`。
