<role>
You are an expert frontend engineer, UI/UX designer, visual design specialist, and typography expert. Your goal is to build Ariadne's Web frontend in a way that is visually consistent, maintainable, and idiomatic to the tech stack.

Before writing any code, first build a clear mental model of the current system:
- Tech stack: Next.js (App Router) + TypeScript + Tailwind. Backend is a separate Python FastAPI service (already built, see API below). This is a FRONT/BACK-SEPARATED project — the frontend ONLY talks to the backend over HTTP, never imports Python, never reads backend files.
- Read `frontend/DESIGN.md` FULLY — it is the binding design contract (Terminal CLI aesthetic, phosphor green, Ariadne-specific components). Every visual decision follows it.
- Read `AGENTS.md` for project boundaries and the Matt workflow.

Once you understand context, propose a concise implementation plan, then build. Prioritize: centralized design tokens, reusable/composable components, minimal one-off styles, long-term maintainability, clear naming. Explain reasoning briefly as you go.

Always: preserve/improve accessibility, maintain visual consistency with DESIGN.md, leave the codebase cleaner than found, ensure responsive layouts, and make deliberate creative choices (layout, motion, typography) that express the design system's personality — never generic boilerplate.
</role>

## 任务：Ariadne Next.js 前端（deep-010）

deep-009 已合入 main：API 已按 issue 收敛，前端可以调通了。本轮做 Next.js 前端。

### 动手前必读（强制前置）
1. **`frontend/DESIGN.md`** — 视觉契约（Terminal CLI 设计系统，令牌/组件/a11y 全在里面）。**这是本任务的最高约束。**
2. **`AGENTS.md`** — 前后端分离铁律、Matt 工作流。
3. `docs/plan/tasks/deep-010-nextjs-frontend.md` — 页面结构设计。
4. 读完**先写结构设计**（目录、组件树、状态方案），确认再动手。用 Ask Matt 工作流，收敛成一个干净 commit。

### 落点
- 全部代码在 **`frontend/`**（Next.js App Router 项目）。**严禁**碰 `src/ariadne/`（Python 后端）。前端依赖用 npm/pnpm，绝不进 pyproject。
- `frontend/DESIGN.md` 已存在，是设计源。令牌落到 Tailwind config / CSS 变量，组件引用令牌，禁止写死颜色。

### 三个页面（照 deep-010，不多做）
1. **列表页 `/`** — issue 列表(终端 `ls -l` 风)，每行：标题 + 状态码徽章 + agent + 活跃 taskrun 数。顶部 `> N agents working` 实时指示 + `[ NEW TASK ]`。调 `GET /api/issues`。
2. **New Task 弹窗** — shell 提示符风输入(见 DESIGN.md §2.5)：任务描述 + backend 下拉 + mode(direct/squad) 切换。提交 → `POST /api/issues` → 跳详情页。
3. **详情页 `/issues/[id]`** — 上=issue 元信息窗格，中=实时执行 transcript(打字机 + SSE)，下=diff 窗格。初始 `GET /api/issues/{id}`，增量 `GET /api/events`(SSE, EventSource)。

### deep-009 真实 API（前端调这些，不要臆造）
- `GET /api/issues` — 列表
- `POST /api/issues` — 建+执行。请求体：`{title, description, backend, mode:"direct"|"squad", agent_name?, detach?, target_repo?}`
- `GET /api/issues/{id}` — 聚合详情(issue + taskruns + diff)
- `PATCH /api/issues/{id}` — 改状态/指派
- `GET /api/issues/{id}/taskruns` — 执行记录
- `GET /api/events` (SSE, text/event-stream) — 实时事件(issue_timeline + activity)，前端用 `EventSource` 订阅
- `GET /api/agents`、`GET /api/agent-profiles`、`GET /api/skills` — 指派选择器数据
- CORS 已放行 `localhost:3000`。后端跑 `uv run ariadne api-serve`(:8000)。

### DESIGN.md 的硬性视觉要求（不做到=失败）
- 状态码徽章精确映射真实枚举(DESIGN.md §2.1)，**颜色+文字双通道**(a11y)。
- ASCII 进度条 `[||||....]`，**禁用图表库**。
- 实时 transcript 打字机效果，事件按 message_type/tool_use/tool_result 结构化渲染(SSE payload)。
- 闪烁块光标 `█`、CRT 扫描线 overlay、ASCII art logo、无圆角、phosphor 辉光。
- 状态码枚举值**从 API 响应映射**，不在前端硬编码状态列表。

### 视觉自查规则（前端不能"编译过就交"，必须自己验证还原效果）

前端的"做完"标准不是代码跑起来，是**界面符合 DESIGN.md**。你必须自查视觉还原，不能写完就交：

1. **预览还原时用 Chrome DevTools MCP 或 Computer Use 自查**：渲染出来后，用 Chrome DevTools MCP（或 Computer Use）实际打开页面、截图、检查渲染效果。对照 DESIGN.md 逐项核验：phosphor 绿、无圆角、扫描线、闪烁光标、ASCII 进度条、状态码双通道、打字机 transcript。**只有当界面符合预期/贴近设计契约时，才结束当前任务**——不符合就继续调，直到对。编译通过但视觉不对 = 未完成。

2. **调试组件用 isolated component preview harness**：调某个组件/功能块（如 `StatusBadge`/`AsciiProgress`/`Transcript`/`ShellPrompt`）时，把它单独渲染到一个临时预览页（isolated harness），集中检查该组件的布局、间距、动效、截图、DOM 状态，确认无误再集成回主页面。不要在完整页面里盲调单个组件。这个临时 harness 页可以调完后删除或留作 `/preview` 开发路由。

上述自查是交付的一部分：回报时要说明你用什么方式（DevTools MCP / Computer Use / harness）验证了哪些组件、截图对照 DESIGN.md 的结果。

### 铁律
- 动手前先写结构设计。用 Matt 工作流，一个 commit。
- 状态方案先用最轻的 `fetch` + `EventSource` + `useState`；**不引入 Zustand/TanStack Query**，除非证明必要。
- 尊重 `prefers-reduced-motion`(关动画)；transcript 用 `role="log" aria-live="polite"`；状态码带 `aria-label` 全称。
- 令牌集中、组件可复用(`StatusBadge`/`AsciiProgress`/`Pane`/`ShellPrompt`/`Transcript` 为原子件)。
- 不做多 workspace/认证/看板拖拽/Cmd+K/线程评论/多端(DESIGN.md 和 deep-010 已划边界)。
- 不碰 Python 后端；不引入 multica SaaS 架构。

### 验证（人工闭环）
```bash
uv run ariadne api-serve            # 后端 :8000
cd frontend && npm install && npm run dev   # 前端 :3000
```
浏览器打开 localhost:3000：看到 issue 列表 → 点 New Task 输两个任务选 dry-run 提交 → 详情页实时看到 transcript 流式追加 → 完成看到 diff。全程不碰命令行、不碰 UUID。

### 回报要求
先给结构设计，再实施。完成后列出：前端目录结构、3 页面是否都实现、New Task 是否调 `POST /api/issues`、详情页 SSE 是否实时追加 transcript、状态码是否双通道且从 API 映射、是否落实 DESIGN.md 的 terminal 视觉(扫描线/光标/ASCII 进度条/无圆角)、是否避免引入重型依赖、`npm run dev`+`api-serve` 闭环是否跑通。
