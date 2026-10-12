# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[1.0.1]: https://github.com/Lemon177777/workbuddy-task-cleanup/releases/tag/v1.0.1
[1.0.0]: https://github.com/Lemon177777/workbuddy-task-cleanup/releases/tag/v1.0.0
