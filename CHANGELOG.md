# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.1.0] — 2026-10-12

> 一次以「安全」为主题的升级：补上两个可能造成数据损失的盲区，并把默认行为从「永久删除」改成「移入回收站」。

### 🛡️ 新增 · 三重保护

清理前先建立三个保护集合，**任一命中即绝不删除**：

- **自动化任务的工作目录** —— `automations.cwds`（JSON 数组，过滤 `deleted_at IS NULL`）。
  ⚠️ **v1.0 的盲区**：这类目录与 `sessions` 表毫无关系，一个工作区完全可能「没有任何存活会话引用」，
  却正是某个定时任务的运行目录 —— 删掉会**悄悄打断用户的自动化**。
- **工作区记忆** —— `<工作区>/.workbuddy/memory/` 下的任何文件。
  ⚠️ **v1.0 的第二个盲区**：v1.0 把「只剩 `.workbuddy` 的目录」当空壳直接删，
  若该目录只含记忆，就会**连项目长期记忆一起删掉**。

被保护的工作区会在报告中单独列出并注明原因；同时会体检 `ACTIVE` 但目录已消失的自动化任务。

### 🔒 安全默认值变更

- **默认移入回收站**（`--to-trash`），删除可反悔；永久删除需显式加 `--no-trash`。
  三平台各自适配：Windows 走 `SHFileOperationW`，macOS 走 `~/.Trash`，Linux 走 freedesktop 规范。
- 删除判定**以实际效果为准**（操作后路径是否消失），不再单纯依赖返回值 ——
  部分环境的删除钩子会代劳删除、却把返回值改写成非 0（实测 rc=2 而文件确实已进回收站）。

### 🧪 新增 · 自检

- `scripts/wb_selftest.py`：在系统临时目录构造一套假数据，验证分类与保护逻辑，**完全不触碰真实数据**。
  共 23 项断言，覆盖历史上踩过的每一个坑（含记忆目录的归类、自动化目录的保护、
  已废弃自动化不受保护、`--keep-b2` 生效、dry-run 绝不落盘等）。

### 🧰 重构

- 抽出 `scripts/wb_common.py`：数据库读取、保护集合计算、回收站实现、清理器统一收敛。
  `wb_clean.py` 由 8.4 KB 精简至 5.4 KB，两个 CLI 只负责参数解析与输出。
- `NEVER_TOUCH` 显式列出更多不可触碰的顶层项（`connectors/`、`skills/`、`plugins/`、`storage/`、
  `local_storage/`、`security/` 等）。

### ➕ 新增参数

| 参数 | 说明 |
| --- | --- |
| `--keep-b2 NAMES` | B2 中要**保留**的目录名，逗号分隔 |
| `--older-than DAYS` | 只清理修改时间早于 N 天的项 |
| `--log FILE` | 把删除清单写入 JSON 留痕 |
| `wb_scan.py --check` | 有残留或异常时退出码 `1`，便于挂定时任务 |

### 🔧 修正

- **B1 判定收紧**：从「只剩 `.workbuddy`」改为「**既无用户文件、也无工作区记忆**」。
  含记忆的目录一律升级为 B2，需人工确认 —— **记忆优先，宁可留着**。

---

## [1.0.1] — 2026-10-11

### 修复

- **清理不彻底的误判**：本机 safe-delete 钩子可能把删除转成回收站操作，并在 `rmtree()` 返回**之后**才真正落盘。
  原先仅凭首轮输出判断"已清干净"会漏判。现已在流程中加入 **Step 5 二次核验**（重跑 `wb_scan.py`），
  并在 `wb_clean.py` 结束时主动提示重跑。
- **`logs/sandbox` 整目录删除失败**：整目录删除会报 `Some operations were aborted`，
  现改为**按日期子目录**逐个删除，并保留当天。

### 新增

- 清理结束后自动输出 **存活会话工作区缺失** 校验结果（最重要安全指标）。
- `wb_scan.py` 支持 `--json` 导出机器可读报告。
- 明确列出**永久豁免名单**：`blobs/`、`workbuddy.db`、`settings.json`、`mcp.json`、`sessions/*.json`、
  存活会话工作区、当天日志。

### 变更

- 体量统计口径统一为「四级分类」，便于对照。

---

## [1.0.0] — 2026-10-10

### 首个版本

- 基于 `~/.workbuddy/workbuddy.db` 的 `sessions.deleted_at` 字段判定「已删除任务」，
  取代按目录时间 / 是否为空 / 侧边栏列表等不可靠的启发式判断。
- 四级分级清理：**A** 内部缓存与转录 · **B1** 空壳孤儿工作区 · **B2** 含成果文件的孤儿工作区 · **C** 诊断日志与追踪。
- `wb_scan.py`：只读盘点，绝不修改任何文件。
- `wb_clean.py`：分级清理，**默认 dry-run**；B2 需额外 `--include-b2` 双重授权。
- 全流程只读打开数据库（`mode=ro`），不写入任何字节。
- 两个脚本仅依赖 Python 标准库，Python 3.8+ 跨平台可用。

---

[1.1.0]: https://github.com/Lemon177777/workbuddy-task-cleanup/releases/tag/v1.1.0
[1.0.1]: https://github.com/Lemon177777/workbuddy-task-cleanup/releases/tag/v1.0.1
[1.0.0]: https://github.com/Lemon177777/workbuddy-task-cleanup/releases/tag/v1.0.0
