# deep-011-B: POST /api/issues 真实 backend detach + daemon 独立执行

> 独立 PR（可与 A 并行）。修 incident 里"页面冻结"根因：HTTP 请求生命周期和后端执行绑死。
> 架构决策已定，照做。

## 根因（已核验）
- `api.py:POST /api/issues` 直接同步调 `run_intent()`——请求全程挂在后端执行上。
- 真实 codex/claude 跑几分钟甚至 600s timeout，浏览器 modal 全程 `running...`，用户以为坏了。
- 这是执行模型问题，不是 CSS。

## 架构裁定（Claude 已决，codex 不要重选）
1. **只对真实 backend（codex/claude-code）detach，dry-run 保持同步**。dry-run 瞬时完成，同步更简单、测试更稳。
2. **后台执行放独立 `ariadne daemon-start` 进程**——**不用** FastAPI BackgroundTasks（避免和请求生命周期纠缠、API 重启丢任务）。API 只 enqueue，独立 daemon claim 执行。这是现有 daemon 的本职，**不发明新执行器**。

## 改动

### 1. `POST /api/issues` 按 backend 分流（`api.py`）
- backend == `dry-run`：维持现状（同步 `run_intent`，立即返回完整结果）。
- backend ∈ {`codex`, `claude-code`}：调 `run_intent(..., detach=True)`（已存在的 detach 路径），**只建 issue + enqueue taskrun，不驱动执行**，立即返回。
- 响应含 `issue_id`、初始 taskruns、`mode`、`backend`、`detached=true`。真实 backend 返回 `202 Accepted`，dry-run 返回 `200`。
- **复用 `run_intent` 的 detach 分支，不在 api.py 写新编排逻辑。**

### 2. 执行交给独立 daemon
- detach 的 taskrun 进 queued，由 `ariadne daemon-start` 进程 claim 执行（现有 claim 循环，无需改）。
- 文档说明：真实 backend 的 UI 用法前提是"后台有 daemon 在跑"。这写进 README/前端提示（前端在 detached 响应后引导用户，或 UI 假设 daemon 已启动）。

### 3. 前端 modal 行为（`frontend/`）
- modal 提交 `POST /api/issues` → 拿到 `issue_id` **立即路由到 `/issues/{id}`**，不等执行完。
- 详情页订阅 SSE 渲染状态变化（已有）。

## 测试（先写失败测试再修）
- `test_post_issue_dry_run_stays_synchronous`：dry-run POST 返回时已完成。
- `test_post_issue_real_backend_detaches`：backend=codex（mock/不真跑）时 POST 立即返回 `detached=true` + queued taskrun，**不阻塞**到执行完。
- 前端：真实 backend 提交后立即跳详情页（可自动化的部分）。

## 验收
- 真实 codex 任务不再把用户卡在 modal。
- 浏览器能在后端执行完成前跳到 issue detail。
- dry-run 冒烟仍通过（同步路径不变）。
- daemon 独立进程能 claim 并执行 detached 的真实任务。

## 边界
- **不碰** heartbeat/进度（PR-C）、不碰失败展示（PR-D）、不碰取消（PR-E）。
- 不用 BackgroundTasks/Redis/WebSocket/worker 系统。
- API 层零业务逻辑——只分流 + 调 run_intent + 序列化。
- 复用现有 daemon claim，不改 claim 逻辑。
