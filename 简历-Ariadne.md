# Ariadne：本地多 Agent 任务编排平台

**核心技术：** Python、Pydantic、SQLite、Typer、Multi-Agent Orchestration、Task State Machine、Harness Integration、Subprocess Execution

**项目描述：**
参考 multica（38k★ 开源 managed agents 平台）的架构理念，开发本地多 Agent 任务编排平台，将 Codex / Claude Code 等 coding agent harness 作为可插拔执行器，通过 Squad 机制实现 leader 委派 + 成员独立执行的任务编排。重点解决多 agent 协作中职责混淆（leader 既 routing 又执行）、任务失败无分类无重试、harness 切换耦合死的问题。

**核心职责与贡献：**

**1. Squad 编排架构设计：** 参照 multica 的 Squad briefing protocol（`squad_briefing.go`），设计 leader-member 分离的编排模型——leader 只做委派决策输出 `DelegationDecision`、不执行实际工作，member 独立 claim 子任务执行，回调在成员完成后重新唤醒 leader 评估下一步。将编排逻辑实现为约 200 行的调度器，支持委派→执行→再评估的完整路径，leader 决策与成员执行完全解耦可独立测试，8 个 orchestrator 测试覆盖委派/无委派/非法委派/事件回调/确定性决策等路径。

**2. 任务状态机与控制平面：** 参照 multica 的 task lifecycle（`queued → claimed → running → completed/failed/cancelled`）和 failure 分类机制，用 SQLite 实现 durable 任务队列，支持 claim/dispatch/complete 状态转移、`attempt`/`max_attempts` 重试链、`failure_reason` 五类失败分类（`agent_error`/`timeout`/`runtime_offline`/`runtime_recovery`/`manual`）。daemon 持久轮询带 heartbeat 和 stale task 抢占回收，覆盖 21 个状态机测试用例，原子 claim 通过 `BEGIN IMMEDIATE` + threading.Lock 保证并发安全，非法状态转移 0 通过。

**3. 多 Harness 可插拔执行层：** 设计 `ExecutionBackend` 协议抽象，实现 CodexBackend 和 ClaudeBackend 两种 coding agent harness 的统一接入——支持 command template 渲染、model/effort 参数注入、安全 gate（`ENABLE_EXTERNAL_EXECUTION` + `confirm_execution` 双确认）、git diff 捕获和超时控制。两种 backend 在真实任务上验证：Codex 完成 power 函数实现（41.8s，2 文件 +7 行）、Claude Code 完成 sqrt 函数实现含除零处理（22.4s，2 文件 +10 行），切换零代码改动。

**4. 结构化委派机制：** 对比 multica 的 `@mention` markdown 路由（prompt 驱动，不可测试），用 Pydantic 模型实现结构化委派——leader 输出包含 `target_agent_id`/`backend`/`skill_refs`/`reason` 的 `DelegationDecision`，经过 roster 验证后创建子任务。委派决策可序列化、可回放、可测试，8 个 briefing 测试覆盖 roster 生成/leader 排除/缺失 agent 跳过等边界，7 个 LLM decide 测试覆盖 JSON 解析/markdown 代码块/API 错误降级/无 key 回退。

**5. 任务恢复与容错：** 基于 `parent_task_id` 重试链和 `failure_reason` 分类，实现分级容错——`agent_error` 自动 retry（≤`max_attempts`）、`timeout` 标记失败并重试、`runtime_recovery` 用于 daemon 崩溃后 stale claim 恢复。在注入失败场景中验证 retry 链正确创建 attempt+1 新任务并保留 handoff_prompt，max_attempts 耗尽后正确停止重试，5 个集成测试覆盖 handoff 透传/retry/CLI 传参/squad 失败循环。

**6. 评测闭环：** 设计两层评测——LLM-as-judge 评估（对执行结果打 1-5 分，无 API key 时确定性回退：completed=5/failed=1/cancelled=0）和 benchmark harness（批量任务收集 success rate/avg score/duration/retry/failure breakdown）。补了 multica 未实现的评估层，8 个 eval 测试覆盖完成/失败/取消/缺失任务评估 + benchmark 指标收集/失败分类/序列化。

**测试：** 120 个测试覆盖状态机/原子 claim/retry/failure 分类/squad briefing/leader 委派/回调通知/LLM decide/backend 安全 gate/diff 捕获/E2E squad loop/评估/benchmark，ruff 全部通过。

**GitHub：** https://github.com/Hackerismydream/ariadne-0630
