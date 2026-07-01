# deep-009: API 按 issue 收敛 + 前端就绪（Next.js 前置）

## Context

终态是 Next.js Web 前端（已定）。但审查 `api.py`(origin/main, 461 行)发现现有 API **撑不起前端**，有四个硬洞：

1. **按表暴露，非按 issue 收敛**：21 个端点里 runtime-machines/capabilities/leases/leader-decisions 全裸暴露。前端要拼十几个端点才能渲染一个 issue。违背 multica「一个核心对象(issue)」的信息架构——正是这个收敛让 multica 前端「清晰」。
2. **几乎只有 GET，无关键写入**：只有 agent-profiles/skills 能 POST。**没有 `POST /api/issues`（建 issue）、没有触发执行的端点**。前端点「新建任务」时后端无口可调。
3. **无 CORS**：Next.js 跑在独立端口，跨域请求会被浏览器直接拦死，前端第一个请求就 fail。
4. **无真正实时端点**：现有 timeline 是一次性 GET 快照，不是流。而 Ariadne 前端的核心卖点正是「实时看 agent 在跑」——没有 SSE/WS 就没有这个体验。

**本任务**：把 API 收敛成「issue 为核心 + 前端友好」的形状，为 Next.js（deep-010）铺路。**这是前端能否落地的前提，不是可选优化。**

**依赖**：deep-008（`ariadne run`）先做——它定义了「建 issue + 执行 + 看结果」的业务逻辑，API 层复用它，不重写。

**参考**：multica 前端分析给出的最小前端只需 ~12 个端点（list/create/get issue、list tasks、stream messages、list agents）。以此为靶子，不多做。

## 核心设计：issue 为中心的资源模型

前端只需理解一个对象 **issue**，其余（runtime/lease/capability）是内部细节，不进前端 API 首屏。

### 1. issue 资源（前端主对象）
- `GET /api/issues` — 列表（含每个 issue 的当前状态、指派、活跃 taskrun 数、最近活动）。前端列表页直接渲染，不用二次拼装。
- `POST /api/issues` — **新增**。建 issue + 可选立即指派 agent/squad 执行（对应 `ariadne run` 的 API 版）。请求体：`{title, description, backend, mode: "direct"|"squad", agent_name?}`。复用 deep-008 的 runner 逻辑，不新写。
- `GET /api/issues/{id}` — 详情：issue 元信息 + 关联 taskruns + 结果(diff/changed_files) 聚合成**一个响应**，前端一次拿全。
- `PATCH /api/issues/{id}` — 改状态/指派（前端看板拖拽用）。

### 2. 执行与结果（挂在 issue 下，不单独暴露表）
- `GET /api/issues/{id}/taskruns` — 该 issue 的执行记录（活跃+历史），含状态/耗时/diff 摘要。
- `GET /api/taskruns/{id}` — 单个 taskrun 详情 + 完整 diff/changed_files（deep-006 已存进 result）。

### 3. 实时流（前端核心体验）
- `GET /api/events` (SSE) — **新增**。issue/taskrun 状态变化、progress 事件实时推送。前端用 `EventSource` 订阅，看 agent 实时进度。
- 复用 deep-006 的结构化 `ProgressUpdate`（message_type/tool_name/content）作为事件 payload——前端能显示「agent 正在调用 X 工具」。
- 数据源：daemon 执行时已写 activity_log / issue_timeline，SSE 从这里推。不新造事件系统（不引入 multica 的 Redis relay，单进程内存即可）。

### 4. 指派选择器数据
- `GET /api/agents`、`GET /api/agent-profiles`、`GET /api/skills` — 已有，保留，供前端「选 agent」下拉用。

### 5. CORS（前端跨域前提）
- 加 FastAPI `CORSMiddleware`，允许 localhost 前端端口。默认只放行 localhost（本地工具，不对公网开放——沿用 local-first 定位）。

## 明确不做
- 不暴露 runtime-machine/capability/lease/leader-decision 到前端首屏 API（内部细节；调试端点可保留但不进前端主路径）。
- 不引入 WebSocket + Redis relay（multica 的多节点方案）——单进程 SSE 足够，零依赖。
- 不做认证（local-first，单用户；CORS 只放 localhost）。
- 不做全文搜索/facet（multica 靠 SQL 全文，超范围）。

## 落点
- `src/ariadne/api.py`：加 `POST /api/issues`、`GET /api/issues/{id}`（聚合）、`PATCH`、`GET /api/issues/{id}/taskruns`、`GET /api/events`(SSE)、CORSMiddleware。
- `POST /api/issues` 的执行逻辑**复用 deep-008 的 `runner.py`**，API 只是它的 HTTP 入口——CLI 和 API 共用同一段业务逻辑（CLI 层/API 层都零业务逻辑，符合现有分层原则）。
- 若 deep-007 已拆 service 层，API 直接调 service。

## 验证
```bash
uv run ruff check src/ariadne/
uv run pytest -q
uv run ariadne api-serve &   # 或现有启动方式
# 建 issue 并触发执行(dry-run)
curl -X POST localhost:8000/api/issues -d '{"title":"test","description":"写个hello","backend":"dry-run","mode":"direct"}'
# 聚合详情
curl localhost:8000/api/issues/<id>
# SSE 实时流
curl -N localhost:8000/api/events
```
- 通过标准：一个 issue 的列表/创建/详情/执行记录/实时流全部可通过 API 完成，前端无需拼多表端点。CORS 允许 localhost。
- 新增测试：POST 建 issue + 触发执行、聚合详情响应形状、SSE 推送事件、CORS 头。

## 回报要求
列出：新增哪些端点、`POST /api/issues` 是否复用 deep-008 runner、issue 详情是否聚合 taskrun+diff 为单响应、SSE 推什么事件、CORS 放行范围、是否避免暴露 runtime/lease 到前端首屏。
