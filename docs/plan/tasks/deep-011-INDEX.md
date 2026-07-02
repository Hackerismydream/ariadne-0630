# deep-011 拆解索引 — 5 个独立小 PR（给 codex，每个边界死框）

> deep-011 原始诊断见 `deep-011-real-backend-usability.md`。那份文档诊断准确、治本、克制，
> 但 scope 大（A-E 五件事）。本索引把它拆成 **5 个独立可交付的小 PR**，每个边界清晰、验收可判定。
> **codex 执行强但死板**——所以每份子任务文档里架构决策已由 Claude 裁定，**不留开放题**，
> codex 照做即可，遇到文档没覆盖的先停下问，不自行发挥。

## 架构裁定（Claude 已决，codex 不要重新决策）

deep-011 原文留了 5 个 Review Question 给 Claude，已裁定如下，各子任务按此执行：

1. **`IssueStatus.FAILED` 现在就加**（不靠 taskrun 派生）。失败是一等状态，派生会让前端/API 到处写派生逻辑。加枚举 + schema CHECK 迁移一次到位。→ PR-A
2. **只对真实 backend（codex/claude-code）detach，dry-run 保持同步**。dry-run 瞬时完成，detach 反而复杂化测试。→ PR-B
3. **后台执行放独立 `ariadne daemon-start` 进程**，不用 FastAPI BackgroundTasks（避免和请求生命周期纠缠、进程重启丢任务）。API 只负责 enqueue，daemon 独立 claim 执行——这就是现有 daemon 的本职，不发明新执行器。→ PR-B
4. **heartbeat 间隔 10s**。→ PR-C
5. **UI 创建的任务默认 timeout 300s + 可覆盖**，CLI 保留 600s。→ PR-C

## 5 个 PR + 依赖顺序

| PR | 内容 | 依赖 | 为什么独立 |
|----|------|------|-----------|
| **PR-A** | 正确性：failed member 不能标 issue done + 加 `IssueStatus.FAILED` | 无 | 纯 bug 修复，不碰执行模型，必须先行 |
| **PR-B** | `POST /api/issues` 真实 backend detach + daemon 独立执行 | 无（可与 A 并行） | 执行模型改动，独立闭环 |
| **PR-C** | 执行中 heartbeat 持久化 + lease 心跳 + timeout 300s | B（detach 后才有"执行中"可观测） | 可观测性，独立验收 |
| **PR-D** | 前端失败/重试链展示（attempt/failure_reason/no-diff 解释） | A（要有 FAILED 状态）+ C（要有 heartbeat 事件） | 纯前端，独立 |
| **PR-E** | 用户取消：`POST /api/issues/{id}/cancel` + kill 进程组 | B（要有独立执行进程可 kill） | 新能力，独立 |

**执行顺序建议**：A 和 B 可并行（都不依赖对方）→ C（依赖 B）→ D（依赖 A+C）→ E（依赖 B）。
**最优先 PR-A**——它修的是 false `done` 这个最伤的正确性 bug，且完全独立。

## 全程铁律（每个 PR 都适用）
- 每个 PR 先写**能复现 incident 的失败测试**，再修，测试转绿。
- `ruff` 零告警；现有测试（当前 215）不回归；前端 `npm run build` 通过。
- 复用 `run_intent`/daemon/orchestrator，API 层零业务逻辑，不重复编排。
- 不引入 Redis/WebSocket/worker 系统/多租户（守 local-first）。
- 真实 backend 执行必须隔离 worktree，绝不写主仓库。
- 一个 PR 一个聚焦 commit 序列，用 Ask Matt 工作流。
- 碰到子任务文档没覆盖的架构选择 → **停下写进报告问 Claude，不自行决定**。

## 各 PR 详细文档
- `deep-011-A-correctness-failed-not-done.md`
- `deep-011-B-detach-real-backend.md`
- `deep-011-C-heartbeat-observability.md`
- `deep-011-D-frontend-failure-presentation.md`
- `deep-011-E-user-cancellation.md`
