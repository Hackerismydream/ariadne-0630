# deep-011-F: daemon 后台化 + running 孤儿崩溃恢复

> 依赖 PR-B（要有独立执行进程）+ PR-C（要有 heartbeat 时间戳可消费）。deep-011 收尾一口气：
> 把 PR-B 埋下的前提假设「真实 backend 的 UI 用法前提是后台有 daemon 在跑」从**甩给用户的文档**
> 变成**产品能力**。架构决策已定，照做。开放题一律停下问 Claude，不自行发挥。
>
> 架构规格全文见 `docs/architecture/daemon-lifecycle.md`，本文件是执行清单。

## 根因（已核验）

- `daemon.py` 自注 "Synchronous loop — no threads, no asyncio"——daemon 是被调用进程一圈圈驱动的**同步循环对象**，不是常驻进程。agent 执行 `proc.wait()` 同步阻塞在发起命令的进程里。CLI/请求一退，活就失管。
- 全仓无 `nohup`/`setsid`/`os.fork`/daemonize。`--detach` 只把 issue/taskrun 建进 DB 立即返回，**不负责让任何进程继续执行**。PR-B 把这层甩给「用户自己开着 daemon-start」，代码里没有衔接。
- `recover_stale_claims` 只 `SELECT ... WHERE status='claimed'`（`task_repo.py:295`）。task 一旦进 `running`，daemon 中途被杀 → **永久卡 `running`，无人复活**。
- `task-state-machine.md:69` 早已定义 `runtime_recovery` = "Daemon restarted mid-execution, task was running"，但**没有代码实现它，转移表里也没有 `running →` 的恢复边**。本 PR 补齐这份 spec 早就承诺、却没落地的一块。

## 架构裁定（Claude 已决，codex 不要重选）

1. **后台化走 `os.setsid` + fork 自守护**，不用 systemd/launchd（平台绑定、装起来重），不用「让用户 nohup」（把走开体验留成手动活，违背产品调性）。记入 ADR-0014。
2. **PID 文件 `~/.ariadne/daemon.pid`** 做单实例守卫 + `stop` 的句柄；**日志 `~/.ariadne/daemon.log`** 承接 detach 后无终端的 stdout/stderr。
3. **崩溃恢复 = reclassify + 可选 re-queue，绝不 resume-in-place**。agent 子进程随父死了，没有活进程可重连；重跑走全新 worktree 才是隔离安全、确定性的恢复。
4. **恢复在进 claim 循环之前跑一次**（startup 时），不是常驻 sweeper（单机，一次表扫描即可，不引 Redis TTL）。
5. **`ORPHAN_TIMEOUT_SECONDS = 90`**（> PR-C 的 10s 心跳间隔，留 ~9 拍余量），区别于 `stale_claim_timeout` 的 60s——避免长执行只是 stdout 静默就被误判死亡。
6. **不做隐式 auto-start**：起 daemon 是显式用户动作，`ari daemon start` 作为前提暴露给文档/前端，但现在它真能后台化。

## 改动

### 1. daemon 自守护（`cli.py` daemon-start 命令 + 新 daemon 生命周期辅助）
- `ari daemon start`：`os.setsid` + fork 脱离控制终端；写 PID → `~/.ariadne/daemon.pid`；重定向 stdout/stderr → `~/.ariadne/daemon.log`；随后进入现有 `Daemon.start()` 循环体。
- **单实例守卫**：start 前读 PID 文件——若进程活着，拒绝启动（不起双 daemon）；若 PID 对应进程已死，视为 stale，覆盖重启。
- **SIGTERM handler**：翻 `_running = False` 干净停循环。
- **不改** claim 循环体、原子 claim SQL、per-issue 串行化。

### 2. running 孤儿恢复（`daemon.py` startup + `task_repo.py` / `task_service.py`）
- 新增 `recover_orphans(runtime_id, now)`，在**进 claim 循环前调用一次**：
```
SELECT * FROM task WHERE status = 'running'
  AND (runtime_id = <本 daemon 上次的 runtime>            -- 自己崩
       OR last_heartbeat_at < now - ORPHAN_TIMEOUT)       -- 任意死 runtime
每条孤儿：
  → failed, failure_reason = 'runtime_recovery'
  if attempt < max_attempts:  retry_task()  → 新 queued task（parent_task_id 设好）
  else:                       停在 failed（终态，issue 可转 FAILED）
  写 activity_log: {event_type: 'orphan_recovered', ...}
```
- 复用 PR-C 写入的 lease `last_heartbeat_at` 作为判活依据（这就是 C 埋时间戳的天然消费端）。
- 恢复后的重试 re-execution **仍走 worktree 隔离 gate**，绝不写主仓库。

### 3. `ari daemon status` 三态诚实报告（`cli.py`）
- 读 PID 文件 + `daemon_state` heartbeat，区分：
  - **healthy**：PID 在 + 进程活 + heartbeat 新鲜
  - **crashed**：PID 在 + 进程死（孤儿等下次 start 恢复）
  - **absent**：无 PID 文件（从没起过）
- 除非 heartbeat 真新鲜，任何情况都不报 healthy。

### 4. 状态机文档补边（`docs/architecture/task-state-machine.md`）
- Legal Transitions 表加两行：`running → failed`（recover_orphans, runtime_recovery）、`failed → queued`（孤儿 attempt 未尽 → retry）。
- （Claude 会同步改，codex 若先到此步按规格补。）

## 测试（先写失败测试再修）

- `test_daemon_start_backgrounds_and_survives_parent_exit`：start 后发起进程可退出，daemon PID 仍活。
- `test_daemon_start_refuses_when_live_pid_exists`：live PID 存在时二次 start 被拒。
- `test_daemon_start_overwrites_stale_pid`：死 PID 被下次 start 回收。
- `test_daemon_stop_terminates_and_removes_pidfile`：stop SIGTERM + 等退出 + 删 PID 文件。
- `test_daemon_status_reports_healthy_crashed_absent`：三态由 PID 存活 + heartbeat 年龄区分。
- `test_recover_orphans_reclassifies_running_to_failed`：prior runtime 的 running → failed + runtime_recovery。
- `test_recover_orphans_retries_when_attempts_remain`：attempt 未尽 → 新 queued task + parent_task_id。
- `test_recover_orphans_terminal_when_attempts_exhausted`：到 max_attempts 停 failed，不无限重试。
- `test_recover_orphans_runs_before_claim_loop`：恢复在首次 claim 前跑一次。
- `test_stale_heartbeat_running_task_is_orphaned`：lease heartbeat 超 orphan_timeout 的 running 被判孤儿。
- `test_fresh_heartbeat_running_task_is_not_orphaned`：仍在心跳的长执行不被误 reclassify。
- `test_orphan_recovery_emits_activity_event`：每次恢复写一条 `orphan_recovered`（SSE 可见）。

## 验收

- `ari daemon start` 后关掉发起终端，daemon 仍在跑，`ari daemon status` 报 healthy。
- kill -9 daemon 后重启，上一轮卡 `running` 的 task 被 reclassify 成 failed 并（若 attempt 未尽）重新 queued，不再永久卡死。
- 长执行（>90s 但仍心跳）不被误判孤儿。
- `ruff` 零告警；现有测试不回归；前端 `npm run build` 通过（本 PR 基本不碰前端）。

## 边界

- **不碰** PR-D 前端失败展示、PR-E 取消的前端/API 表层（本 PR 到「daemon 能后台化 + 崩溃能恢复 + cancel 能跨进程送达」为止）。
- 不引入 Redis/WebSocket/worker 池/Postgres（守 local-first）。
- 单机单常驻 daemon（多 worker 是 post-秋招，已在 backlog）。
- 恢复只 reclassify + re-queue，**绝不 resume-in-place / 进程收养**。
- 不做隐式 auto-start——起 daemon 是显式动作。
- 碰到本文档没覆盖的架构选择 → **停下写进报告问 Claude，不自行决定**。

---

## 附：deep-011 review 并入 F 的两条 MEDIUM（进程模型同源问题）

deep-011 A-E review 挖出两条 MEDIUM，根因都是「API 和 daemon 是两个进程，但代码假设它们在一个进程里」——正是 F 的进程模型地盘，故并入 F 统一解决，不单开 PR。

### M1. cancel 跨进程投递（`_ACTIVE_PROCESS_GROUPS` 进程内全局失灵）
- 现状：`backends.py` 的 `_ACTIVE_PROCESS_GROUPS` 是**进程内**模块全局。cancel 跑在 API 进程里（`api.py:411`），那里注册表是空的 → **API/前端发起的 cancel 一个子进程都杀不到**；真 kill 只发生在 daemon 进程内、且要等下次 `backend_heartbeat` 或 execute 返回。
- F 落地后 daemon 是独立进程，这个问题**更严重**（两进程彻底分家）。所以 cancel 必须走**跨进程投递**，不能靠进程内注册表。
- 裁定做法：cancel 只写 DB 标志（issue/taskrun→cancelled，已实现），**daemon 在 claim/执行循环里轮询 DB 的 cancelled 标志**，发现后 kill 自己进程内注册的进程组。这与 F 的「daemon 是唯一执行进程」一致——只有 daemon 持有进程组，也只有 daemon 负责 kill。
- 需明确契约：cancel 是**最终一致、非即时**（延迟 ≤ 一个 heartbeat/poll 周期）。写进文档，别让前端/用户以为是瞬时。

### M2. 消除 daemon 执行路径的 dry-run 静默降级
- 现状：`daemon._execute_member_task`（`daemon.py:206-214`）遇到未知/不可用 backend **静默回退 dry-run 执行**，而 claim 路径（`task_service._match_capability`）已改成严格 provider 匹配、不匹配就不 claim。两条路径语义分裂：一条拒绝、一条降级成假成功。
- 与本项目立意冲突：**不把 fallback/dry-run 包装成成功**。daemon 遇到 agent 声明的 backend 不可用时，应让 task **failed（`failure_reason=provider_error` 或等价）**，而不是静默 dry-run 跑出一个假 completed。
- 裁定：daemon 执行路径对齐 claim 路径的严格性——backend 不可用 → 显式失败 + 落 failure_reason，不降级。dry-run 只在**用户显式选 dry-run backend** 时才跑，绝不作为兜底。

### 这两条的测试（并入 F 测试清单）
- `test_cancel_delivered_across_process_via_db_flag`：cancel 写 DB 后，daemon 轮询发现并 kill 进程组（非依赖进程内注册表）。
- `test_daemon_unknown_backend_fails_not_dryrun`：agent 的 backend 不可用时，daemon 让 task failed + failure_reason，**不**静默降级 dry-run 成假成功。
