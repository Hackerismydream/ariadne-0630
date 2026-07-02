# deep-011-E: 用户取消运行中的任务

> 依赖 PR-B（要有独立执行进程/subprocess 可 kill）。新能力：用户知道跑错了不必等 600s。

## 根因
- incident 里用户明知"超级玛丽"任务不对，却只能干等 600s timeout。
- 现在没有从 UI/API 取消运行中任务的路径。

## 架构裁定（Claude 已决）
- 用 **`POST /api/issues/{id}/cancel`** 专用端点（不复用 PATCH status=cancelled，语义更清晰、便于前端按钮直连）。

## 改动

### 1. 取消端点（`api.py`）
- `POST /api/issues/{id}/cancel`：取消该 issue 下 queued/running 的 taskruns。
- 复用 store 的 cancel 能力（`cancel_task`/`cancel_taskrun` 已存在），API 层零业务逻辑。

### 2. 运行中 subprocess 可被 kill（`backends.py` + daemon 协作）
- 现状：`backends.py` 用 `subprocess.Popen` 跑 provider，但**无 process-group 追踪**（已核验：只有 Popen，无 killpg）。
- 加：按 active taskrun 追踪 subprocess（进程组）。取消时 best-effort kill 进程组。
- 实现：Popen 用 `start_new_session=True`（新进程组），记录 pid；取消时 `os.killpg`。**保持在 backend 内，不引入新机制。**

### 3. 取消的终态语义
- 取消 → taskrun 标 `cancelled`（**不是** `failed timeout`）。
- 释放 active lease（复用 `release_runtime_lease`）。
- issue 落到 `cancelled`。

### 4. 前端取消按钮（`frontend/`）
- 详情页 running 态显 `[ CANCEL ]` 按钮 → 调取消端点 → SSE 反映 `cancelled`。

## 测试（先写失败测试再修）
- `test_cancel_running_issue_marks_task_cancelled_and_releases_lease`：running taskrun → 取消 → 断言 taskrun `cancelled` + lease released。
- `test_cancel_queued_task`：queued 态取消也生效。
- 前端：running 详情页有 cancel 按钮，点击后状态变 cancelled（可自动化部分）。

## 验收
- 运行中 codex 任务能从 UI/API 取消。
- DB 记 terminal `cancelled`，不是 `failed timeout`。
- 进程组被 best-effort kill（真实 backend 冒烟验证，隔离 worktree）。

## 边界
- 只加取消能力。不碰 A/B/C/D 的范围。
- kill 逻辑留在 backend，不引入信号总线/worker manager。
- 真实 backend 验证必须隔离 worktree。
- process-group kill 跨平台注意：本项目 local-first 主要 macOS/Linux，Windows 可暂标 best-effort/不支持并记进报告。
