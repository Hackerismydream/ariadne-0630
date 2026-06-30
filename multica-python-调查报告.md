# Multica Python 版作为秋招 Agent 岗简历项目：深度调查报告

> 日期: 2026-06-30
> 核心问题: 如何用 Python（FastAPI + LangGraph）在 1 个月内做一个聚焦「Agent 编排协作」的 multica 精简版，使其成为秋招 Agent 开发岗有竞争力的简历项目？
> 相关项目: https://github.com/multica-ai/multica
> 探索: 3 轮，一手来源核验（multica 源码 + 官方文档 + 框架 API 元数据 + 牛客/掘金/CSDN 社区帖 + JD 摘要）

---

## 一、Multica 到底是什么（读源码后的澄清，不是看 README 的印象）

很多人会假设 multica 的编排是「一张多 agent 图」。**这是错的。**

追到一手源码（`server/internal/handler/squad_briefing.go`、`server/internal/daemon/daemon.go`、migrations）后，multica 的编排协作子系统本质是：

> **「事件驱动的 @mention 路由」叠加在一个「DB 持久化的任务状态机」之上，而不是编译式图。**

### 1.1 Squad（核心编排原语）= leader agent + @mention 委派

读 `squad_briefing.go` 后的精确机制：

1. Squad = 一组 agent（+可选人类成员）+ 一个 leader agent。Squad 是一等 assignee（`issue.assignee_type CHECK IN ('member','agent','squad')`，migration 084）。
2. 给 Squad 分配 issue → 入队一个 task 给 **leader**（不是所有成员）→ leader claim → 被注入三段 system prompt：
   - **Squad Operating Protocol**（硬编码的协调规则，原文在 `squadOperatingProtocol` 常量）：leader 的职责是「coordinate, NOT do the work yourself」
   - **Squad Roster**（成员名单 + 每人的 skills + 精确的 `[@Name](mention://agent/<UUID>)` markdown）
   - **Squad Instructions**（用户自定义）
3. Leader 发**一条** delegation comment @mention 被选中的成员 → 该 mention 解析后**触发每个被提及者一个新 task** → leader 记录 evaluation → **stop**。
4. Leader 再触发规则（事件循环）：成员有新活动 → 重新唤醒 leader 评估。

**关键洞察**：multica 的「编排」= ① 任务生命周期状态机 ② 评论事件循环决定是否唤醒 leader ③ leader 是一次 LLM 调用，输出 = @mention 路由决策 ④ agent 执行外包给外部 CLI。

### 1.2 任务状态机（migration 001 + 055 一手核验）

```
agent_task_queue.status: queued → dispatched → running → completed/failed/cancelled
issue.status: backlog → todo → in_progress → in_review → done/blocked/cancelled
```

- task 有 `attempt` / `max_attempts` / `parent_task_id` / `failure_reason`（migration 055：retry + lease 机制）
- `failure_reason` 分类：`agent_error` / `timeout` / `runtime_offline` / `runtime_recovery` / `manual`
- daemon 轮询 claim（`POST /api/daemon/runtimes/{id}/tasks/claim`），支持抢占式回收 stale dispatched task

### 1.3 Daemon-Server 协议（`client.go` 一手核验）

```
claim → start → progress(summary, step, total) → messages[] → complete/fail
+ heartbeat（15s）+ cancel 轮询（5s）+ usage 上报
```

- 进度流：`ReportProgress(taskID, summary, step, total)` + `ReportTaskMessages`（带 seq/type/tool/content/input/output）
- 这是 WebSocket 实时流的后端来源

### 1.4 这个差异是选型的决定性因素

| 维度 | multica（一手源码） | LangGraph/CrewAI/AutoGen（框架） |
|------|---------------------|--------------------------------|
| 生命周期 | 跨天、DB 持久、评论事件触发 | 一次 invoke 跑完的有界运行 |
| 状态存储 | PostgreSQL | checkpointer（短期）/ 无 |
| 编排机制 | @mention 路由（prompt 驱动） | 图节点 / 角色链 / 群聊 |
| agent 执行器 | 外部 CLI（Claude Code 等） | 框架内节点 |

**没有任何一个 Python 框架开箱即用等于 multica。「把有界运行框架适配成长期事件驱动 issue 控制平面」恰恰是 1 个月项目里最值得做、最能体现工程深度的部分。**

---

## 二、Python 框架选型（一手核验 star/活跃度/编排模型）

> 数据来源：`gh api repos/<repo>` 认证调用，2026-06-30 当天

| 框架 | Stars | 编排模型 | 能否实现 Squad+leader | 维护状态 |
|------|-------|---------|---------------------|---------|
| **LangGraph** | 36,074 | 编译式状态图（supervisor/swarm/handoff） | ✅ supervisor=leader, handoff=@mention | 活跃 ✅ |
| CrewAI | 54,592 | 角色化（hierarchical process） | ✅ 最直接（manager=leader） | 活跃 ✅ |
| AutoGen | 59,363 | 对话/群聊（SelectorGroupChat） | ✅ LLM 选下一发言者 | ⛔ **维护模式** |
| MS Agent Framework | 11,761 | 图式工作流（handoff/group+checkpointing） | ✅ | 活跃 ✅ |
| Pydantic AI | 18,085 | 单 agent + pydantic-graph | ⚠️ 需手搓 | 活跃 ✅ |

### 关键变化（2026 现行版，已核验）

- **LangChain 官方现行多 agent 分类**已从旧的 supervisor/swarm 演进为：**Subagents / Handoffs / Skills / Router / Custom workflow**。官方 supervisor 库 README 顶部写「now recommend using supervisor pattern directly via tools」。
- **AutoGen 进入维护模式**（README CAUTION 块原文核验），License 是 **CC-BY-4.0**（对代码很不寻常）。新项目强烈不建议。

### 推荐：LangGraph + FastAPI(WebSocket) + PostgreSQL

理由（诚实权衡后）：
1. 多 agent 编排原语最丰富且最显式——supervisor（=leader）/ handoff（=@mention 路由）/ 条件边 / 多级层级都是一等公民
2. checkpointer 直接解 multica 的 durable 任务态
3. 简历信号最强：36k★、LangChain 出品、市场认知度最高、面试官最可能认识
4. 差距即工程量：长期 issue + 评论事件循环 + WebSocket + 外部执行器适配，需要自建——这正是有深度的项目边界

**备选**：求快出 demo → CrewAI Hierarchical；求类型安全/生产感 → Pydantic AI。
**明确不建议**：AutoGen（维护模式）。

---

## 三、秋招 Agent 岗要什么（一手社区帖 + JD 核验）

### 3.1 JD 反复出现的技能要求

| 技能 | 频率 | 与本项目相关性 |
|------|------|--------------|
| Python + FastAPI/Flask 后端 | 极高 | ✅ 直接命中 |
| LangChain/LangGraph | 高 | ✅ 直接命中 |
| Function Calling / Tool Use | 高 | ✅ 命中 |
| Multi-Agent / 多 Agent 协作 | 高 | ✅ 直接命中（项目聚焦点） |
| ReAct / Plan-and-Execute 设计模式 | 高 | ✅ 命中 |
| MCP 协议 | 中高（2026 新增热点） | ⚠️ 需主动加入 |
| RAG + 向量数据库 | 高 | ❌ 项目聚焦编排，需补充 |
| Agent 评估体系 | 中（加分项） | ⚠️ 需主动设计 |
| 记忆系统/上下文工程 | 中高 | ✅ 可命中 |

来源：华为 AI Agent JD（BOSS）、快手 AI Agent JD（GitHub 面经库）、卡码编程 2026 面试题汇总

### 3.2 面试官的评估标准（牛客一手帖，来源：nowcoder.com/discuss/874981010608308224、861207388076900352）

**「水项目」的 4 个特征**（面试官眼中的 Demo 而非产品）：
1. 只展示模型能力，忽略系统工程（无 API 层/数据库/日志/错误处理/兜底）
2. 只是简单调用框架（用户输入 → 大模型 → 工具 → 返回，线性流程）
3. 上下文只存对话历史（无专门的上下文管理架构）
4. 缺乏监控和评测体系（只能靠主观体验判断效果）

**高分项目的核心**（牛客原话）：「一个真正有含金量的 Agent 项目，本质上不是一个 Prompt Demo，而是一个**完整的 AI 应用系统**。核心不只是'大模型'，而是如何围绕大模型构建一个稳定、高效、可持续迭代的应用系统。」

**多 Agent 编排的定位**（牛客原话）：「多智能体架构设计也是**企业级 Agent 系统与简单 Demo 项目之间的重要区别**」——但会被深挖「为什么需要多 Agent 而不是单 Agent」。

### 3.3 本项目竞争力的诚实评估

**优势**：
- ✅ 技术栈高度命中 JD（Python+FastAPI+LangGraph）
- ✅ Multi-Agent 编排是企业级与 Demo 的分水岭
- ✅ 「managed agents 平台」比「又一个聊天机器人」有差异化
- ✅ multica 有热度（38k★），面试官可能知道，有话题性

**风险**：
- ⚠️ 原版是 Go+TS，Python 重写易被质疑「换语言搬运」
- ⚠️ multica 核心是「管理已有 agent CLI」非「实现 agent 编排逻辑」——若只套 LangGraph 做 task routing 而没深入协作机制设计，深度不够
- ⚠️ 1 个月时间紧，容易停留在 Demo 层
- ⚠️ 缺 RAG，技能覆盖不全
- ⚠️ 多 Agent 易被追问「为什么不用单 Agent」

---

## 四、推荐方案：项目定位与 MVP 架构

### 4.1 重新定位（规避「换语言搬运」质疑）

**不要说「复刻 multica」，而说「参考 multica 的 managed agents 理念，用 Python + LangGraph 重新设计与实现一个 Agent 编排协作平台」。**

强调你的**技术决策**：
- 为什么用 LangGraph 的 StateGraph 而不是 multica 的工单模型
- 为什么用 Python（生态、LangGraph 原生、类型安全）
- 你做了哪些 multica 没有的东西（评估体系、可观测性等）

### 4.2 核心场景（论证「为什么需要多 Agent」）

选一个**明确需要多角色协作**的场景，避免被质疑过度设计。推荐：

**代码评审团场景**（天然多角色 + 贴近 Agent 开发岗）：
- Coder Agent（实现/修复）
- Reviewer Agent（审查代码质量）
- Manager Agent（=leader，路由 + 汇总）
- 可选：Knowledge Agent（带 RAG，补齐 RAG 技能缺口）

痛点论证：单 Agent 上下文爆炸（要同时懂实现规范 + 审查标准 + 项目架构）+ 角色冲突（自己写自己审不可信）。

### 4.3 MVP 架构（1 个月，砍功能保深度）

```
┌─────────────────────────────────────────────────┐
│  FastAPI + WebSocket（控制平面）                  │
│  - Issue 看板 CRUD + 任务状态机                    │
│  - 评论事件循环（决定是否唤醒 leader）              │
│  - WebSocket 实时进度流                            │
├─────────────────────────────────────────────────┤
│  LangGraph 编排层（每次 leader 触发 = 一次子图运行） │
│  - Supervisor 节点（=leader，读 roster → 委派决策） │
│  - Handoff（=@mention 路由到成员节点）              │
│  - checkpointer（任务状态持久化）                   │
├─────────────────────────────────────────────────┤
│  Agent 执行层                                     │
│  - 成员节点 = LLM + tools（function calling）      │
│  - 简化版：不调外部 CLI，直接用 LLM + 工具           │
│  - 接入 1-2 个 MCP server（补 MCP 技能）           │
├─────────────────────────────────────────────────┤
│  PostgreSQL（持久化）                             │
│  - issue / task / squad / agent / activity 表     │
└─────────────────────────────────────────────────┘
```

### 4.4 与 multica 的关键差异（这是你的「技术决策」故事）

| multica 的做法 | 你的 Python 版做法 | 为什么 |
|---------------|-------------------|--------|
| @mention 路由（prompt 驱动，leader 输出 markdown） | LangGraph handoff tool（结构化委派） | 更可控、可测试、可观测 |
| 外部 CLI 当执行器 | LLM + function calling 当执行器 | 1 个月内可落地，聚焦编排而非 CLI 适配 |
| 无评估体系 | 加 Agent 评估体系（LLM-as-judge） | 补齐 JD 加分项，证明生产级思维 |
| LangSmith 可观测 | 加 Trace + 量化指标对比 | 面试加分，可讲底层 |

---

## 五、1 个月路线图

| 周次 | 目标 | 交付物 | 面试可讲点 |
|------|------|--------|-----------|
| **W1** | 控制平面骨架 | FastAPI + Postgres + issue/task/squad 表 + 任务状态机 | 状态机设计、DB schema、异步任务调度 |
| **W2** | LangGraph 编排核心 | supervisor 子图 + handoff 委派 + 单场景跑通端到端 | 编排设计决策、为什么用 supervisor、防死循环机制 |
| **W3** | 实时流 + 评估 + 可观测 | WebSocket 进度流 + LLM-as-judge 评估 + Trace 日志 | 可观测性设计、评估体系、量化指标对比 |
| **W4** | 打磨 + 文档 + demo 视频 | README + 架构图 + demo + 单/多 Agent 对比基准 | 完整闭环、量化成果、技术决策叙事 |

**W1 必须做的风险 spike**：验证「LangGraph 把外部执行器包成节点」的工程量。这是最大技术风险点。

---

## 六、面试时的讲法（按优先级）

### 🥇 编排协作的设计决策（不是「我用了 LangGraph」）
- 为什么这个场景需要多 Agent 而不是单 Agent？职责边界怎么划分？
- Agent 间如何通信？状态如何流转？为什么用 StateGraph？
- 怎么防止死循环？（最大步数 + 重复动作检测 + 超时控制——脱口而出）

### 🥇 控制策略 / Orchestrator 设计
- leader 如何做意图识别和任务路由？
- 支持 ReAct 还是 Plan-and-Execute？为什么？
- 工具调用失败时的容错策略（重试 → 降级 → 兜底，三级）

### 🥈 工程系统化能力（区分 Demo 和产品）
- FastAPI API 层设计、异步处理
- 会话状态/上下文持久化
- 可观测性：Trace/日志/指标（接入 Langfuse 或自建）
- 评估体系：LLM-as-judge 对输出打分

### 🥈 量化成果
- 「单 Agent vs 多 Agent 在 XX 任务上的完成率对比」
- 「任务路由准确率 Y%」「响应延迟从 A 优化到 B」

### 🥉 底层原理（必考）
- Function Call 底层实现（LLM 输出 JSON 指令，你的代码执行）
- MCP 解决什么问题？和 Function Call 什么关系？
- Agent 记忆系统：短期 vs 长期

---

## 七、风险规避清单

| 风险 | 规避方法 |
|------|---------|
| 被当成「换语言搬运」 | 说「参考理念重新设计」，强调技术决策（LangGraph StateGraph vs 工单模型） |
| 多 Agent 被质疑过度设计 | 选明确需要多角色的场景（代码评审团），论证单 Agent 痛点 |
| 缺 RAG | 加一个 Knowledge Agent（带 RAG），覆盖检索型+工具型协作 |
| 停在 Demo 层 | 宁可功能少，每个功能做到生产级深度（可观测+评估+容错+量化） |
| MCP 盲区 | 实际接入 1-2 个 MCP server，能讲清 MCP vs Function Call |
| 死循环/稳定性 | 实现并讲清：最大步数、重复检测、超时控制、三级容错 |

---

## 八、验证记录

| 结论 | 方法 | 结果 |
|------|------|------|
| multica Squad = leader + @mention 路由，非图 | 读 `squad_briefing.go` 源码 + `multica.ai/docs/squads` | ✅ 已核验 |
| 任务状态机 queued→dispatched→running→completed/failed | migration 001 + 055 源码 | ✅ 已核验 |
| daemon-server 协议（claim/start/progress/messages/complete） | `client.go` 源码 | ✅ 已核验 |
| LangGraph 36,074★ / supervisor 库推荐改用 tools 模式 | `gh api` + supervisor README | ✅ 已核验 |
| AutoGen 维护模式 + CC-BY-4.0 license | microsoft/autogen README CAUTION 块 | ✅ 已核验 |
| CrewAI Hierarchical = manager 规划/委派/校验 | `docs.crewai.com/concepts/processes` | ✅ 已核验 |
| 秋招面试官评估标准（水项目特征+高分项目核心） | 牛客一手帖 | ✅ 已核验 |
| JD 技能要求（Python+FastAPI+LangGraph+Multi-Agent） | 华为/快手 JD + 卡码面经 | ✅ 已核验 |

## 九、盲点

- **LangGraph 把外部执行器包成节点的工程量**：未实际写代码验证，建议 W1 做 spike。[UNVERIFIED]
- **LangGraph checkpointer ↔ 长期 issue 状态映射**：未核验社区最佳实践，需原型验证。[UNVERIFIED]
- **月之暗面/智谱/MiniMax/DeepSeek 具体校招 JD**：未能抓取一手来源，结论基于大厂共性推断。[UNVERIFIED]
- **各框架并发执行多成员的语义细节**：multica leader 可一次 @多个成员并发；LangGraph supervisor 默认串行，并发需显式配置——未核验每个框架细节。[UNVERIFIED]

---

## 十、一句话结论

> **方向对、技术栈命中 JD、有差异化，竞争力中等偏上。1 个月时间，砍功能保深度：选 1 个强场景、3-4 个 Agent、做透编排机制 + 工程化 + 评估体系 + 可观测性，每个点都能经得起面试官追问底层原理，比铺 10 个浅功能强 10 倍。核心叙事不是「复刻 multica」，而是「参考 managed agents 理念，用 LangGraph 重新设计了一个 Agent 编排协作平台，并解决了有界运行框架到长期事件驱动控制平面的适配问题」。**
