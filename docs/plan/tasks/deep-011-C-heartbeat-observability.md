# deep-011-C: 执行中 heartbeat 持久化 + lease 心跳 + timeout 300s

> 依赖 PR-B（detach 后才有"执行中"状态可持续观测）。修 incident 里"静默 600s 页面看着像死了"。
> 架构决策已定，照做。

## 根因（已核验）
- `backends.py` 只在 provider 吐 stdout 行时报进度。incident 里 timeline 只有一条 `starting codex execution`，然后 600s 空白。
- runtime 知道 subprocess 活着、timeout 在倒计时，但这事实没被持久化成用户可见事件。
- lease `expires_at` 是 claim 后 1 分钟，但 subprocess 跑 600s——lease 看着像 stale，dashboard 无法区分"健康"vs"孤儿"。

## 架构裁定（Claude 已决）
- **heartbeat 间隔 10s**（本地 UX 够用，不刷屏）。
- **UI 创建的任务默认 timeout 300s + 可覆盖**；CLI 保留 600s。

## 改动

### 1. 执行中 heartbeat 事件（`backends.py` 执行循环）
- subprocess 活着期间，每 10s 持久化一条 activity 事件：
```json
{"event_type": "backend_heartbeat", "taskrun_id": "...",
 "payload": {"backend": "codex", "elapsed_seconds": 185, "timeout_seconds": 300,
             "execution_repo_path": "...", "pid": 7772}}
```
- 写进 `activity_log`（deep-009 的 SSE 已经推 activity_log，前端自动能收到）。
- 实现要点：Popen 读 stdout 是阻塞的——用一个独立计时线程或在 poll 循环里按 10s 节拍写心跳，不阻塞 stdout 读取。**保持在 backend 内，不引入新执行框架。**

### 2. lease 心跳随执行更新（daemon/backends 协作）
- 执行中 active lease 的 `last_heartbeat_at` 随之推进（复用 `store.heartbeat_runtime_lease`，已存在）。
- lease 不因 provider 执行长就显 stale。

### 3. timeout 分流（`runner.py` / `ExecutionContext`）
- UI/API 路径默认 timeout 300s；CLI `ariadne run` 保留 600s；均可 `--timeout` / 请求字段覆盖。

## 测试（先写失败测试再修）
- `test_backend_heartbeat_events_are_persisted_during_silent_process`：用一个 sleep 的 fake backend/短心跳间隔，断言静默期间 activity_log 有 ≥1 条 `backend_heartbeat`。
- `test_active_lease_heartbeat_updates_during_long_execution`：断言执行中 lease `last_heartbeat_at` 推进。
- `test_ui_default_timeout_is_300s`：API 路径构造的 ExecutionContext timeout==300。

## 验收
- 静默的长 provider 运行期间，`/api/events` 周期性发 `backend_heartbeat`。
- 前端 transcript 显示 elapsed / timeout budget（前端渲染在 PR-D，本 PR 只保证事件被持久化+推送）。
- operator 能区分"provider 还活着"vs"孤儿任务"。

## 边界
- 只加 heartbeat 持久化 + lease 心跳 + timeout 分流。
- **不碰** SSE 机制本身（deep-009 已有轮询，心跳事件走同一通道）。
- 不引入 WebSocket/Redis/异步框架——心跳是"写表"，SSE 轮询表已能推。
- 前端展示归 PR-D，本 PR 到"事件被推送"为止。
