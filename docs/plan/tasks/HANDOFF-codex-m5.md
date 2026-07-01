# M5 Handoff — 叙事定稿（给 codex）

> 前置：M0-M3 已在 origin/main 落地并通过核验（register_backend、session/MCP、
> Skill 表+WAL、隔离优先执行，测试 146→194）。本轮做 M5：把这些成果沉淀为
> 可讲的叙事。**先读 deep-005 的 M5 条目和「执行铁律」再动手。**

## 你能独立做的（M5 主体）

### 1. 给 M0-M3 补 ADR
在 `docs/adr/` 按现有编号往后排（当前到 0009），每个决策一篇：
- **开放注册的执行层**：为什么从硬编码 `_BACKENDS` dict 改成 `register_backend()`；为什么**不**做第三方 entry-point 发现（避免为不存在的用户过早抽象）。
- **session 续跑 + MCP 注入**：为什么 durable teammate 需要 resume；MCP config 的 agent 级 > env 级优先规则。
- **Skill 作为 capability package**：从「标签」到「内容 materialize + verification 闭环」；为什么 verification 失败是「信号」不是「硬门禁」。
- **确认 gate → 隔离优先（最重要，最能讲）**：记录这个 UX 认知迭代——「最初用双重确认做安全，后来意识到这是把设计者的焦虑转嫁给用户，重构成隔离优先：安全来自架构而非弹窗」。带上 `worktree_audit` 的审计设计。

### 2. 架构图 / 架构文档更新
- 反映真实现状：注册化执行层、skill 表、worktree 隔离执行流、WAL。
- 架构文档描述**代码真实状态**，不描述计划终点。

### 3. README 能力描述
- 基于 M0-M3 的真实代码补新能力段落。只写已实现的，别写未有的。
- 叙事继续去 multica 化（主语是问题本身），multica 只在 provenance 段出现。

## 你不能做、必须留给人的（硬边界）

1. **benchmark 真实数字——禁止编造。**
   简历/README 的 benchmark 表目前标着「dry-run 模拟」，这是诚实的，**保持原样**。
   真实 codex/claude 加速比需要真实环境（API key + 真实 CLI）跑 `benchmark-compare --backend codex` 才能得到——那是 M0 剩下的人工步骤，还没跑。
   **在真实数据产出前，不许把任何数字写成真实性能声明，不许估算，不许「预计」。** 保持模拟标注即可。这是 deep-005 第 2 条铁律。

2. **M4 的 3 分钟人工验收——不是你的任务。**
   你是源码作者，天然违反「不看源码」前提，你跑的验收不算数。这一步由项目所有者本人做，你不要代跑、不要报告「已验收」。

## 完成标准
- ADR 齐全，能对着 README + 架构图讲 20 分钟，每个取舍有 ADR 支撑。
- 简历无失实数字（模拟的标模拟，真实的等真实环境跑完才填）。
- `uv run ruff check src/ariadne/` + `uv run pytest -q` 全绿，194 测试不回归。
- 每个 ADR / 文档改动一个清晰 commit，做完贴 `pytest` 输出。

## 做完后回报什么
不要只说「M5 完成」。列出：新增了哪几个 ADR（编号+标题）、README/架构图改了哪些段、哪些数字**仍是模拟待补**（明确标出等真实环境）。
