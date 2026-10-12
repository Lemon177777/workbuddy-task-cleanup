---
name: workbuddy-task-cleanup
display_name: "WorkBuddy 任务垃圾清理"
display_name_en: "WorkBuddy Task Junk Cleanup"
description: "清理 WorkBuddy 自身数据目录中「已删除任务」遗留的孤儿文件。当用户说「清理 workbuddy 垃圾」「清理已删除任务的残留」「WorkBuddy 占空间太大」「清理会话残留 / 缓存 / 日志」「clean up WorkBuddy junk」时使用。基于 workbuddy.db 的 sessions.deleted_at 精确区分已删/存活会话，再按会话 id 与工作区路径定位孤儿，分级（A 内部缓存与转录 / B1 真空壳工作区 / B2 含成果文件或工作区记忆的孤儿工作区 / C 诊断日志与追踪）盘点并清理。内置三重保护：存活会话工作区、自动化任务引用的工作目录、含工作区记忆的目录，一律不删；非标准命名目录保守归入 B2 待确认。默认移入回收站或自建隔离区（可用 wb_restore.py 精确还原），全程先只读扫描、呈现报告、取得用户确认后才动手。"
description_zh: "清理 WorkBuddy 中已删除任务的遗留垃圾文件。依据 workbuddy.db 的 sessions.deleted_at 精确定位已删会话，并保护被自动化任务引用的工作目录与含工作区记忆的目录。分四级盘点（内部缓存与转录、真空壳工作区、含成果文件或记忆的孤儿工作区、诊断日志与追踪），报告附「关联线索」帮助辨认工作区归属，默认移入回收站或隔离区（可还原），用户确认后再清理。"
description_en: "Clean up orphan files left behind by deleted tasks in WorkBuddy's own data directories. Uses sessions.deleted_at in workbuddy.db to separate deleted from live sessions, and protects directories referenced by automations plus any workspace containing .workbuddy/memory. Four tiers: internal caches & transcripts, true empty shells, orphan workspaces holding user artifacts or memory, diagnostic logs & traces. Reports carry a 'context' block (which deleted sessions/automations a workspace belonged to). Trash or self-owned quarantine by default, with wb_restore.py for precise rollback; deletion only after explicit user confirmation."
agent_created: true
version: 1.2.0
tags: [workbuddy, cleanup, 清理, 垃圾清理, 磁盘空间, deleted-task, orphan, cache, logs, traces, 缓存, 日志, 残留, 会话, session, maintenance, 维护, quarantine, restore, 还原, 隔离区]
---

# WorkBuddy 任务垃圾清理

> **适合场景**：WorkBuddy 占用空间过大 · 删掉的对话还占磁盘 · 清理残留缓存/日志/追踪 · 定期维护

**执行手册**：严格按 Step 0 → Step 5 顺序执行。**Step 3 的用户确认是硬门槛，不可跳过。**

> **路径约定**：`<SKILL_DIR>` 指本 SKILL.md 所在目录。
> **Python**：用本机可用的 Python 3 即可 —— Windows 常见为托管路径 `%USERPROFILE%\.workbuddy\binaries\python\versions\<ver>\python.exe`，或直接 `python` / `py`；macOS / Linux 用 `python3`。四个脚本**仅依赖标准库**，Python 3.8+ 均可运行，无需安装任何第三方包。

---

## v1.2 新增能力

| 能力 | 用法 | 解决什么 |
|---|---|---|
| **工作区关联线索** | `wb_scan.py`（自动带出） | B2 报告列出「曾关联哪些已删会话 / 哪条自动化」，逐个确认时一眼认出这是什么项目 |
| **单目录体检** | `wb_scan.py --inspect <目录>` | 回答「这目录是谁的、能不能清」，不必跑全量扫描 |
| **按标题筛选** | `--match 关键词`（scan / clean） | 精确打击，如 `--match 光缆 --tiers B2` |
| **快速扫描** | `--fast`（scan / clean） | 跳过体积统计，只数数量，大目录下明显更快 |
| **自建隔离区** | `wb_clean.py --quarantine --apply` | 移入自己的隔离区（记录原始路径），可精确还原 |
| **隔离区管理** | `wb_restore.py list / restore / purge` | 查看、还原、按天批量清除 |

> 扫描提速：体积计算已改为**线程池并行**（`dirsize_many`）。`--fast` 进一步跳过体积，只统计数量。

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
| **B1** | 真·空壳孤儿工作区（无用户文件、**无工作区记忆**、且目录名符合时间戳格式） | 低 |
| **B2** | **含用户成果文件 或 含 `.workbuddy/memory/` 记忆 或 非标准命名**的孤儿工作区 | **高，须逐个确认** |
| **C** | 诊断：`traces/`、`logs/<日期>/`、`logs/sandbox/` | 中，删后失去历史排障线索 |
| **🔒** | **受保护**：被存活会话 或 **自动化任务**引用的工作区 | **绝不删除** |

## 四重保护（v1.1 起，务必理解）

清理前必须建立保护集合，任一命中即**绝不删除**：

1. **存活会话的 cwd** —— `sessions.deleted_at IS NULL` 的工作目录。
2. **自动化任务的工作目录** —— `automations.cwds` 字段（JSON 数组）。
   ⚠️ **这是 v1.0 的盲区**：自动化任务的工作目录与 `sessions` 毫无关系，
   一个工作区可能"没有任何存活会话引用"、却正是某个定时任务的运行目录。
   删掉它 = 悄悄打断用户的定时任务。查询时**必须**过滤 `automations.deleted_at IS NULL`。
3. **工作区记忆** —— `<工作区>/.workbuddy/memory/` 下的任何文件。
   ⚠️ **这是 v1.0 的第二个盲区**：v1.0 把"只有 `.workbuddy` 的目录"当空壳直接删，
   但 `.workbuddy/memory/` 里可能存着该项目的长期记忆与自动化任务记忆。
   含记忆的工作区必须升级为 B2、列出记忆文件、由用户确认。
4. **非标准命名的目录**（v1.2 新增）—— `~/WorkBuddy/` 下名字不符合 `YYYY-MM-DD-HH-MM-SS` 的目录，
   即使完全为空也**不判为 B1**，而是归入 B2 待人工确认 —— 它可能是用户手建的。
   宁可让人多看一眼，也不要自动删掉用户自己命名的东西。

> ⚠️ **已删除的自动化任务不参与保护集合**，但会出现在「关联线索」里。
> 若不区分 `deleted_at`，会把用户早已删掉的旧任务也当成"还在用"，导致**误报**（v1.1 起已修正，自检里有专门断言）。

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
# 常规盘点（体积用线程池并行计算）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_scan.py" --json ~/wb_cleanup_report.json

# 想更快：跳过体积统计
"<PYTHON>" "<SKILL_DIR>/scripts/wb_scan.py" --fast

# 只关心某条线索（匹配 目录名 / 会话标题 / 自动化名）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_scan.py" --match 光缆

# 体检单个目录：这是谁的、能不能清
"<PYTHON>" "<SKILL_DIR>/scripts/wb_scan.py" --inspect "~/WorkBuddy/2026-09-14-22-35-03"
```

输出分四级的数量、体积，**B2 每个目录的完整文件清单 + 关联线索**、**受保护工作区**、**存活会话工作区完整性**、**失效自动化体检**。这一步只读，可放心执行。

`--check` 参数在「有残留或异常」时返回退出码 1，便于挂定时任务。

清理脚本自身可先自检（用临时目录造数据，不动真实数据）：
```bash
"<PYTHON>" "<SKILL_DIR>/scripts/wb_selftest.py"     # 应输出「通过 54 项，失败 0 项」
```

## Step 2 · 分类与呈现

把盘点结果整理成中文报告呈现给用户，至少包含：

- 会话总数 / 已删除数 / 存活数；自动化任务存活/已删条数
- A / B1 / B2 / C 各级的数量与体积
- **C 级说明**：属诊断数据，删了会失去历史排障线索 —— 让用户知情
- **B2 逐个列出**：目录名 + 体积 + 文件清单 + **关联线索**（曾关联哪些已删会话/自动化），
  并明确告知"这是你自己的成果文件或项目记忆，目前仅存于此"
- **B1 若只按名字看不出是什么**，可用 `--inspect` 补一条线索再呈现

## Step 3 · 取得确认（硬门槛 ⚠️）

**未得到用户明确选择前，绝不执行任何删除。**

- 用选择题让用户勾选清理范围（A / B1 / B2 / C，可多选）与删除方式（回收站 / 隔离区 / 永久删除）。
- **B2 必须单独二次确认**：先逐个列文件与关联线索，再问"仍要永久删除？"，
  因为这是用户的成果、记忆，或用户自己命名的目录。
- 若用户选择永久删除，用**加粗**明确警告：
  > ⚠️ **此操作非常危险，可能导致不可逆的数据丢失！**
  并列出受影响的目录与文件。

## Step 4 · 执行清理

```bash
# 预览（默认 dry-run，不落盘）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C

# 执行 —— 默认移入系统回收站，可反悔
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C --apply

# 移入自建隔离区（推荐，可精确还原）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C --apply --quarantine

# 永久删除（必须显式声明，危险）
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C --apply --no-trash

# 含 B2：必须加 --include-b2；可用 --keep-b2 选择性保留
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers A,B1,C,B2 --include-b2 --apply \
    --keep-b2 "2026-09-14-22-35-03"

# 精确打击 + 留痕
"<PYTHON>" "<SKILL_DIR>/scripts/wb_clean.py" --tiers B2 --include-b2 --match 光缆 \
    --older-than 30 --apply --log clean.json
```

| 参数 | 说明 |
|---|---|
| `--tiers` | 类别，逗号分隔：`A,B1,B2,C` |
| `--apply` | 真正执行；缺省为 dry-run |
| `--to-trash` / `--no-trash` | 移入系统回收站（**默认**）/ 永久删除 |
| `--quarantine [DIR]` | 移入**自建隔离区**（默认 `~/.workbuddy/.wb-quarantine`），可用 `wb_restore.py` 精确还原 |
| `--include-b2` | 清理 B2 的必要开关 |
| `--keep-b2 NAMES` | B2 中要保留的目录名，逗号分隔 |
| `--match KEYWORD` | 只处理关联到该关键词的工作区（目录名/会话标题/自动化名） |
| `--older-than DAYS` | 只清理修改时间早于 N 天的项 |
| `--fast` | 跳过体积统计，更快 |
| `--log FILE` | 把删除清单写入 JSON 留痕 |

- 脚本自动跳过：存活会话工作区、**自动化任务工作目录**、`blobs/`、`settings.json`、`workbuddy.db`、`mcp.json`、当天日志与当天 sandbox。
- 遇到 `[safe-delete]` 报错属正常（被占用/钩子拦截），记录下来稍后重跑补齐即可。
- **注意**：部分环境存在「删除钩子」，会代劳删除但返回被改写的错误码。本脚本**以实际效果为准**（操作后路径是否消失），不单纯依赖返回值。

## Step 4b · 撤销 / 还原（用了隔离区才需要）

```bash
"<PYTHON>" "<SKILL_DIR>/scripts/wb_restore.py" list                      # 查看隔离区
"<PYTHON>" "<SKILL_DIR>/scripts/wb_restore.py" restore --all             # 全部还原到原路径
"<PYTHON>" "<SKILL_DIR>/scripts/wb_restore.py" restore --id <ID>         # 还原指定条目
"<PYTHON>" "<SKILL_DIR>/scripts/wb_restore.py" purge --older-than 30     # 彻底清除到期项
```

隔离区会记录每一次的**原始路径**，所以还原是精确的，不需要用户自己去找。
`purge` 是唯一不可逆的操作，务必先展示清单再让用户确认。

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
4. **不删任何被自动化任务引用的工作目录** —— 删掉等于悄悄打断用户的定时任务。
5. **不把含 `.workbuddy/memory/` 的工作区当空壳** —— 那是项目长期记忆。
6. **不把非标准命名的目录当空壳** —— 一律归 B2，多要一次人工确认。
7. **B2 必须逐个列文件 + 关联线索 + 二次确认**，因为那是用户的成果、记忆或自建目录。
8. **默认走回收站或隔离区**；用户明确要求永久删除时才加 `--no-trash`，并先加粗警告、列出受影响路径。
9. **删完必须二次核验**，并报告"存活会话工作区缺失 0"。
10. 保留当天的 `logs/<今天>/` 与 `logs/sandbox/<今天>/`。
11. **改动脚本后先跑 `wb_selftest.py`**，54 项全绿再交付。

细节与体量参考见 `references/orphan-map.md`。
