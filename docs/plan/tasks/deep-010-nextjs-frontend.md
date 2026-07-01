# deep-010: Next.js 极简前端（验收界面 / 终态雏形）

## Context

终态是 Web 前端，技术栈定为 **Next.js**（用户决策）。本任务做一个**极简但真实**的 Next.js 前端，让用户能「看着 agent 跑、看着 diff 出来」——把验收从「敲命令看输出」变成「点一下看结果」。

**核心约束：抄 multica 的信息架构，不抄它的体量。** multica 前端是 1380 个 TS/TSX 文件、一个团队全职做的产品。我们一个人（用 AI 生成）做的是它的**核心 DNA**，不是它的全部页面。

**依赖**：deep-009 必须先完成——前端调的 API（issue 聚合、POST 建 issue、SSE 实时流、CORS）由 deep-009 提供。没有 deep-009，这个前端是调不通的空壳。

## 从 multica 前端提炼的「可复制的少数决定」

multica 前端分析给出的核心 UX DNA，只抄这几条（其余太贵，跳过）：

| 抄 | 为什么 | 跳过 | 为什么太贵 |
|----|--------|------|-----------|
| **issue 为唯一核心对象** | 一个对象多个视图，零认知负担 | 全文搜索+facet | 需要 SQL 全文/ES |
| **列表页（Linear 风）** | 一眼看所有 issue + agent 状态 | 看板拖拽 | 状态管理复杂，v1 不必 |
| **详情页 = 实时进度 + diff** | 核心体验：看 agent 在跑、看结果 | 线程化评论 | parent_id 链 + 解析状态，太重 |
| **实时状态指示（跑/完成）** | 一眼知道有没有在动 | Cmd+K 命令面板 | 好但非必需，v2 再说 |
| **进度三层可视** 简化成**一层** | 详情页内嵌流式 transcript 够用 | 活动 coalescing | 后端去重规则，易错 |

## 页面结构（3 个页面，不多）

### 1. Issue 列表页 `/`
- 表格/列表：每行一个 issue（标题、状态 badge、指派的 agent、活跃 taskrun 数）。
- 顶部一个显眼的「New Task」按钮 + 一个「N agents working」实时指示（SSE 驱动的计数）。
- 数据源：`GET /api/issues`（deep-009 已聚合，前端直接渲染）。

### 2. 新建任务（弹窗，不单独页面）
- 一个输入框（任务描述）+ backend 下拉（dry-run/codex/claude）+ mode 切换（direct / squad）。
- 提交 → `POST /api/issues` → 跳转到详情页。
- 对应 `ariadne run` 的可视化版本——CLI 和 UI 走同一后端逻辑（deep-008 runner / deep-009 API）。

### 3. Issue 详情页 `/issues/[id]`
- 上半：issue 元信息 + 状态。
- 中间：**实时执行 transcript**——SSE 订阅 `GET /api/events`，agent 每步（thinking/tool_use/tool_result，来自 deep-006 结构化 ProgressUpdate）实时追加。这是核心体验。
- 下半：**结果 diff**——taskrun 完成后显示 changed_files + diff（deep-009 详情响应已含）。
- 数据源：`GET /api/issues/{id}` 初始快照 + `GET /api/events` SSE 增量。

## 技术选型

- **Next.js（App Router）** + TypeScript。
- 状态/数据：先用最轻方案——`fetch` + SWR 或原生 `useState`+`EventSource`。**不引入 Zustand/TanStack Query**（multica 用，但对 3 页面是过度）。除非 codex 判断确有必要。
- 样式：Tailwind（Next 默认易接）。组件不引入重型库，够用即可。
- 放在 `frontend/` 或 `apps/web/`（独立于 Python 包，`uv` 不管它）。
- 开发：前端 `npm run dev`（默认 3000），后端 `ariadne api-serve`（8000），靠 deep-009 的 CORS 打通。

## 明确不做（防止膨胀成 multica）
- 不做多 workspace、认证、账户（local-first 单用户）。
- 不做看板拖拽、Cmd+K、线程评论、活动 coalescing（v2 candidate，不进本轮）。
- 不做 desktop/mobile（multica 有，纯工程量，零 agent 能力贡献）。
- 不追组件数量/页面数量——3 个页面覆盖「列表→建→看」闭环即达标。
- 不上 Zustand/TanStack Query 除非 codex 证明必要。

## 验证
```bash
# 后端
uv run ariadne api-serve      # :8000 (deep-009)
# 前端
cd frontend && npm install && npm run dev   # :3000
```
人工验收（这才是 M4 的真正达成形态）：
1. 浏览器打开 `localhost:3000`，看到 issue 列表。
2. 点「New Task」，输入两个任务描述，选 dry-run，提交。
3. 详情页**实时**看到 agent 执行 transcript 流式追加。
4. 完成后看到 diff / changed_files。
5. 全程不碰命令行、不碰 UUID。

**这一套跑通 = 终态雏形达成 = 你能用页面验收了。**

## 回报要求
列出：3 个页面是否都实现、New Task 是否调 `POST /api/issues`、详情页 SSE 是否实时追加 transcript、diff 是否显示、是否避免引入 multica 级别的重型依赖、前端目录结构、`npm run dev` + `api-serve` 是否跑通闭环。
