# deep-011-G: cancel 状态转移合法化（review 修复）

> 独立小 PR，修 deep-011 review 挖出的 HIGH：cancel 绕过状态机、与 `task-state-machine.md` 自相矛盾。
> 纯正确性修复，不碰执行模型/进程模型。架构决策已定，照做，开放题停下问 Claude。

## 根因（已核验）

- `cancel_task`（`src/ariadne/service/task_service.py:184-192`）允许 `QUEUED→CANCELLED`、`CLAIMED→CANCELLED`，但：
  - 这两条转移**不在** `_LEGAL_TRANSITIONS`（`src/ariadne/store/base.py:63,68` 只有 `PREPARING→CANCELLED`、`RUNNING→CANCELLED`）。
  - `cancel_task` **绕过 `_check_transition`**，自己做成员检查后直接调 `mark_task_cancelled`（`task_repo.py:267-274`）。
- 结果：代码行为与 `task-state-machine.md` 白纸黑字的「非法转移一律拒绝，无静默恢复」直接矛盾。状态机严谨是本项目核心卖点，此矛盾一翻 diff 即见光。
- 顺带（LOW#4）：`cancel_issue`/`cancel_task` 对已终态（DONE/COMPLETED/FAILED）的 task 也强制覆盖成 CANCELLED，无源状态守卫（`issue_repo.py:128-133` 无 guard）。属同一个「cancel 该遵守状态机」的问题，一起收窄。

## 架构裁定（Claude 已决，codex 不要重选）

1. **`QUEUED→CANCELLED`、`CLAIMED→CANCELLED` 是合法转移**，正式加进 `_LEGAL_TRANSITIONS` + 文档转移表。cancel 一个还没开始跑的 task 本就该允许，这是遗漏不是设计错。
2. **cancel 必须走状态机校验**（`_check_transition` 或等价路径），不再绕过。非法源状态（已 COMPLETED/FAILED/CANCELLED 终态）→ 拒绝，抛 `InvalidStateTransition`。
3. **不碰跨进程 kill**（`_ACTIVE_PROCESS_GROUPS` 进程内注册表的问题归 deep-011-F）。
4. **不碰 backend 静默降级 dry-run**（daemon 执行路径的问题归 deep-011-F）。

## 改动

### 1. 合法转移表（`src/ariadne/store/base.py`）
- `_LEGAL_TRANSITIONS` 增加 `QUEUED→CANCELLED`、`CLAIMED→CANCELLED`（保留已有的 `PREPARING→CANCELLED`、`RUNNING→CANCELLED`）。

### 2. cancel 走校验（`src/ariadne/service/task_service.py`）
- `cancel_task` 改为经 `_check_transition`（或等价校验）判定源状态合法性，非法源状态抛 `InvalidStateTransition`，不再无条件 `mark_task_cancelled`。
- `cancel_issue` 对已处于终态（DONE/COMPLETED/FAILED/CANCELLED）的 issue/taskrun 不强制覆盖——终态的 taskrun 跳过，不参与 cancel。
- lease 释放逻辑保持现状（已确认正确：`mark_runtime_lease_released`）。

### 3. 文档转移表（`docs/architecture/task-state-machine.md`）
- Legal Transitions 表补两行：`queued → cancelled`（user/API）、`claimed → cancelled`（user/API）。
- 明确终态不可 cancel（与「非法转移一律拒绝」一致）。

## 测试（先写失败测试再修）

- `test_cancel_queued_is_legal`：cancel 一个 queued taskrun → CANCELLED，不抛异常。
- `test_cancel_claimed_is_legal`：cancel 一个 claimed taskrun → CANCELLED。
- `test_cancel_running_is_legal`：running→cancelled 仍合法（回归保护，别改坏）。
- `test_cancel_from_terminal_is_rejected`：cancel 一个已 COMPLETED 或 FAILED 的 taskrun → 抛 `InvalidStateTransition`，状态不变。
- `test_cancel_goes_through_state_machine_check`：断言 cancel 路径经过转移校验（非法源状态被拒），不再绕过。

## 验收

- cancel queued/claimed/running 三种合法源状态均成功。
- cancel 终态 task 被拒绝，不再静默覆盖成功/失败结果。
- `task-state-machine.md` 转移表与代码 `_LEGAL_TRANSITIONS` 完全一致（无文档-代码分歧）。
- `ruff` 零告警；现有 240 测试不回归；前端 `npm run build` 通过（本 PR 不碰前端）。

## 边界

- **只修状态转移合法性**。不碰跨进程 kill（F）、不碰 backend 降级（F）、不碰 issue 表迁移事务性（backlog LOW）、不碰 lease revoked/released telemetry（backlog LOW）。
- 不引入新依赖。一个聚焦 commit，用 Ask Matt 工作流。
- 碰到本文档没覆盖的架构选择 → 停下写进报告问 Claude，不自行决定。
