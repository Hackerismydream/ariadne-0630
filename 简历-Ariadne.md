# Ariadne：本地多 Agent 任务编排平台

**核心技术：** Python、FastAPI、Pydantic、SQLite、Typer、Multi-Agent Orchestration、Task State Machine、Harness Integration、Trace/Observability

**项目描述：**
参考 multica（38k★ 开源 managed agents 平台）的架构理念，开发本地多 Agent 任务编排平台，将 Codex / Claude Code 等 coding agent harness 作为可插拔执行器，通过 Squad 机制实现 leader 委派 + 成员独立执行的任务编排。重点解决多 agent 协作中职责混淆（leader 既 routing 又执行）、任务失败无分类无重试、harness 切换耦合死、执行结果不可观测的问题。

**核心职责与贡献：**

**1. Squad 编排架构设计：** 参照 multica 的 Squad briefing protocol（`squad_briefing.go`），设计 leader-member 分离的编排模型——leader 只做委派决策输出 `DelegationDecision`、不执行实际工作，member 独立 claim 子任务执行，回调在成员完成后重新唤醒 leader 评估下一步。支持多步委派（leader 委派给 coder → 执行完成 → 再委派给 tester → 执行完成 → 标记 done），leader 在 re-evaluation 时接收已完成成员的结果作为决策上下文，8 个 orchestrator 测试覆盖多步委派/无委派/非法委派/事件回调等路径。

**2. 任务状态机与控制平面：** 参照 multica 的 task lifecycle（`queued → claimed → running → completed/failed/cancelled`）和 failure 分类机制，用 SQLite 实现 durable 任务队列，支持 claim/dispatch/complete 状态转移、`attempt`/`max_attempts` 重试链、`failure_reason` 五类失败分类（`agent_error`/`timeout`/`runtime_offline`/`runtime_recovery`/`manual`）。daemon 持久轮询带 heartbeat 和 stale task 抢占回收，原子 claim 通过 `BEGIN IMMEDIATE` + threading.Lock 保证并发安全，重试链保留 handoff_prompt 确保 agent 拿到完整上下文。

**3. 多 Harness 可插拔执行层：** 设计 `ExecutionBackend` 协议抽象，实现 CodexBackend 和 ClaudeBackend 两种 coding agent harness 的统一接入——支持 command template 渲染（`shlex.quote` 防注入）、安全 gate（`ENABLE_EXTERNAL_EXECUTION` + `confirm_execution` 双确认）、git worktree 隔离执行（避免污染目标 repo）、Popen 流式进度上报（逐行读取 stdout）、git diff 捕获和超时控制。ClaudeBackend 额外解析 `--output-format json` 结构化输出，提取 `result`/`session_id`/`num_turns`/`cost_usd` 到 metadata。两种 backend 在真实任务上验证：Codex 完成 power 函数实现（41.8s，2 文件 +7 行）、Claude Code 完成 sqrt 函数实现含除零处理（22.4s，2 文件 +10 行），切换零代码改动。

**4. 结构化委派机制：** 对比 multica 的 `@mention` markdown 路由（prompt 驱动，不可测试），用 Pydantic 模型实现结构化委派——leader 输出包含 `target_agent_id`/`backend`/`skill_refs`/`reason` 的 `DelegationDecision`，经过 roster 验证后创建子任务。委派决策可序列化、可回放、可测试。LLM 决策支持 re-evaluation 上下文（传入已完成成员结果）、3 次连续失败自动降级到确定性决策，无 API key 时走确定性 fallback。

**5. 可观测性 — Trace 全链路追踪：** 每个 task 生成 `trace_id`，retry 和 delegation 子任务继承同一 `trace_id`，贯穿 claim→delegate→execute→complete 全生命周期。`activity_log` 表记录每个状态转移事件（created/claimed/started/completed/failed/delegated/retried），`ariadne task-timeline` CLI 和 dashboard 可按 trace_id 查看完整时间线，7 个 trace 测试覆盖 ID 生成/继承/活动日志/时间线排序。

**6. 评测闭环与 Web Dashboard：** 设计两层评测——LLM-as-judge 评估（1-5 分，无 key 时确定性回退）和 benchmark harness（批量任务收集 success rate/duration/retry/failure breakdown）。FastAPI 控制面提供 REST API（issues/tasks/timeline/agents）和单页 HTML dashboard，支持自动刷新、任务点击查看时间线、agent 状态实时展示。

**测试：** 145 个测试覆盖状态机/原子 claim/retry/failure 分类/squad briefing/多步委派/事件回调/LLM decide/trace 全链路/backend 安全 gate/worktree 隔离/JSON 解析/流式进度/E2E squad loop/API 端点，ruff 全部通过。

**GitHub：** https://github.com/Hackerismydream/ariadne-0630
