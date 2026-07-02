# deep-011-D: 前端失败/重试链展示

> 依赖 PR-A（要有 `IssueStatus.FAILED`）+ PR-C（要有 heartbeat 事件）。纯前端 PR。
> 让失败/重试无需读 SQLite 就能看懂。

## 根因
- incident 里前端只显示 `(no diff captured)`，用户不知道是失败了、还是超时了、还是在跑。
- retry 链（timeout → retry → timeout）在 UI 里不可见。

## 改动（全部在 `frontend/`）

### 1. 详情页失败信息展示
详情页（`IssueDetailPage.tsx`）对每个 taskrun 展示：
- attempt number / max attempts
- parent retry chain（沿 `parent_task_id` 串起来）
- `failure_reason`（timeout/agent_error/…）
- elapsed duration + last event time
- **no-diff 的解释性文案**，替代裸 `(no diff captured)`：
  - failed → `no diff captured because execution failed`
  - timeout → `provider timed out after {N}s`

### 2. 重试链可视化
- timeout → retry → timeout 显示成一条链（terminal 风：用 `└─` / `│` ASCII 树，符合 DESIGN.md）。

### 3. heartbeat 渲染（消费 PR-C 的事件）
- transcript 消费 `backend_heartbeat` 事件，显示 `elapsed 185s / 300s`（ASCII 进度条 `[||||....]`，复用 `AsciiProgress`）。

### 4. 失败态视觉
- issue `failed` → `[ERR]`（PR-A 已加映射），详情页整体呈失败态，**不与成功 leader 任务的完成态视觉混淆**。

## 视觉自查（DESIGN.md 契约）
- 用 Chrome DevTools MCP / Computer Use 截图自查：失败态、重试链、heartbeat 进度条是否符合 terminal 风格（phosphor、无圆角、ASCII 树/进度条）。
- 组件用 isolated preview harness 单独调（失败态的 taskrun 卡、重试链组件）。
- 符合 DESIGN.md 才算完。

## 测试
- New Task 提交后（非 dry-run/detached）立即路由到 detail（PR-B 已保证，这里验前端渲染）。
- detail 渲染 running heartbeat。
- detail 渲染 retry chain。
- detail 渲染 failed issue + timeout 原因。
- **detail 在所有 member taskrun failed 时不显示 `[DONE]`**（呼应 PR-A）。

## 验收
- timeout → retry → timeout 在 UI 是一条可见的链。
- 最终 issue 状态 `failed` 视觉明确，不和成功混淆。
- 长任务运行中，前端显示 elapsed/timeout budget。

## 边界
- 纯前端，**不碰后端**。消费的事件/字段都是 A/B/C 已经产出的。
- 不引入图表库/重型依赖，ASCII 树+进度条用现有组件。
- 若发现后端缺字段 → 停下写进报告问 Claude，不自己去改后端。
