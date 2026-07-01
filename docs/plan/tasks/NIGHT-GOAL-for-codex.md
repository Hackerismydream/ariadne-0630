# 通宵自主目标书 — deep-010 前端 + 联调 + 自修 + runtime 优化（给 codex，无人值守）

> 用户睡觉，你无人值守自主执行。**这不是开放探索——是带收敛条件和护栏的自主循环。**
> 先读 `AGENTS.md`、`frontend/DESIGN.md`、`docs/plan/tasks/deep-010-nextjs-frontend.md`、
> `docs/plan/tasks/deep-009-api-issue-convergence.md`，再开工。全程用 Ask Matt 工作流。

## GOAL（醒来时应达成的状态）

一个**能跑通的 terminal 风格前端 + 稳定后端**的完整闭环：
1. `frontend/` Next.js 前端按 DESIGN.md 落地（3 页面：列表 / New Task / 详情实时进度+diff）。
2. **前后端联调通过**：`api-serve`(:8000) + `npm run dev`(:3000)，浏览器能走完「建任务→看实时 transcript→看 diff」，dry-run 全绿。
3. 联调中暴露的 bug **你自己修掉**（前端或后端，谁的问题修谁）。
4. runtime（daemon/backends/orchestrator/runner）的**具体缺陷**顺带修（有界，见护栏）。
5. 全程测试保持绿、ruff 干净，每个逻辑单元一个干净 commit。

## 执行顺序（分阶段，每阶段自查后再进下一段）

**阶段 1：前端落地**（deep-010 主体）
- 按 deep-010 PROMPT + DESIGN.md 建 Next.js 前端，只在 `frontend/`。
- 组件用 isolated preview harness 单独调（StatusBadge/AsciiProgress/Transcript/ShellPrompt/Pane）。
- 用 **Chrome DevTools MCP / Computer Use 截图自查**，对照 DESIGN.md 逐项核验（phosphor 绿/无圆角/扫描线/闪烁光标/ASCII 进度条/状态码双通道/打字机 transcript）。视觉不符就继续调，符合才进下一段。

**阶段 2：前后端联调**
- 起后端 `uv run ariadne api-serve` + 前端 `npm run dev`。
- 用 DevTools MCP / Computer Use 实际操作：建任务(dry-run) → 看列表刷新 → 进详情 → 看 SSE transcript 流式追加 → 看 diff。
- 记录每个断点（CORS？SSE 断流？字段对不上？状态码映射错？），逐个修。**修到闭环真能走通为止。**

**阶段 3：自查修 bug**
- 联调暴露的问题，前后端谁的问题修谁。
- 每修一个：先加一个能复现的测试（后端 pytest / 前端能自动化的部分），再修，确认测试转绿。**先复现再修，不靠"看起来对"。**

**阶段 4：runtime 有界优化**（见护栏——只修具体缺陷，不重构没坏的）

## 护栏（HARD LIMITS — 违反即停）

### 🔥 真实 backend 授权（用户明确要求：放开烧 token 把产品做出来）
- **鼓励用真实 codex/claude backend 深度联调**，跑通真实端到端：真实建 issue → 真实 agent 执行 → 真实 diff → 前端真实展示。dry-run 只用于快速回归，**产品是否真能用要靠真实 backend 证明**。
- 用户明确授权无人值守烧 token，token 不是约束。目标是产品真正跑起来，不是省钱。
- **但真实执行必须隔离**（这条与省钱无关，是保护主仓库）：真实 backend 一律在**隔离 git worktree** 跑（deep-006 默认隔离已保证），**绝不** `--write-workspace` 写主仓库。一夜无监督执行不能污染主工作区。
- 真实 backend 跑出的**性能/加速比数字如实记录进 NIGHT-REPORT.md**（标注真实环境），这正好补上 M4 的真实 benchmark——但只记真实跑出来的，不估算、不编造。

### 🚧 runtime 优化是"有界"的，不是开放重构
- **只修**：测试红的、联调暴露的、明确的 bug（如竞态、资源泄漏、错误吞掉、状态机漏转移）。
- **不做**：主动重构没坏的代码、换架构、加新功能、引入新依赖、"我觉得更优雅"的改写。
- 每个 runtime 改动必须有一个**失败测试先证明问题存在**，再修。没有可复现的问题 = 不改。
- 碰到需要改模块边界 / 跨层 / 引入 DDD 新构件 / 本目标书没覆盖的情况 → **停下，写进 `NIGHT-REPORT.md` 待人决策，不自行扩大范围。**

### 🧱 边界（AGENTS.md）
- 前端只在 `frontend/`，只通过 HTTP 调后端，不 import Python、不读后端文件。
- 后端不塞前端资源。API 层零业务逻辑（复用 runner，不重写）。
- 不引入 multica 的 SaaS 架构（Redis/WS/event bus/多节点/认证）。
- models 按需充血但不引入过度仪式（值对象总线/CQRS）。

### ✅ 每个 commit 的门槛
- `uv run ruff check src/ariadne/` 零告警。
- `uv run pytest -q` 全绿，且**不删/不改弱现有测试来凑绿**（改测试=改行为，要在报告里说明理由）。
- 前端 `npm run build` 通过。
- 一个逻辑单元一个 commit，message 说清「做了什么 + 落哪层」，不夹带 AI 署名。

## 收敛条件（什么时候算"做完了"停下）
达成 GOAL 的 1-5，或**连续 2 轮找不到新的可复现问题**时，停止，写 `NIGHT-REPORT.md`：
- 完成了什么（前端页面、联调结果、修了哪些 bug、runtime 改了什么）
- 每个 bug：怎么复现的、根因、怎么修的、测试证明
- **卡住/不确定/需要人决策的**（尤其边界问题、真实 backend 的表现）
- 哪些数字来自真实 backend、哪些是 dry-run（如实标注；真实的就记真实值，不编造/不估算）
- 当前测试数 + ruff 状态 + 前端 build 状态

## 绝不做（醒来不想看到的）
- 真实 backend 执行绝不写主仓库（必须隔离 worktree）。
- 不为凑绿删测试、改弱断言。
- 不主动重构没坏的 runtime。
- 不编造真实性能数字（dry-run 就标 dry-run）。
- 不碰 git 历史、不 force push、不动 main 以外的既有分支。
- 不越过 GOAL 范围加功能（"顺便做个 X"= 停下记进报告）。
