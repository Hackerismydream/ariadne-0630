# deep-011-A: 正确性 — failed member 不能标 issue done（最优先）

> 独立 PR，不依赖其他。修 deep-011 incident 里最伤的 bug：所有 member 任务 timeout 失败后，
> squad leader 却把 issue 标成 `done`（false positive）。架构决策已定，照做。

## 根因（已核验，在真实代码里）
- `orchestrator.py:_gather_completed_results` 捞 `status IN ('completed', 'failed')`——failed 任务也进结果集。
- `_coerce_leader_decision`：deterministic fallback 下，`completed_results` 非空就可能 coerce 成 `DONE`，即使里面全是 failed。
- incident DB 实证：2 个 timeout failed taskrun + leader 标 `done` = false positive。

## 架构裁定（Claude 已决）
- **加 `IssueStatus.FAILED` 作为一等状态**，不靠 taskrun 派生。理由：失败要在 issue 层有明确表示，派生会让前端/API 到处写派生逻辑。

## 改动

### 1. 加 `IssueStatus.FAILED`（`models.py`）
- `IssueStatus` 枚举加 `FAILED = "failed"`。

### 2. schema CHECK 迁移（`store/schema.py:60`）
- issue 表 `CHECK (status IN ('backlog','todo','in_progress','done','cancelled'))` → 加 `'failed'`。
- 对已有 DB 需迁移路径（参照项目已有的 schema 迁移做法）。

### 3. 修 `_coerce_leader_decision`（`orchestrator.py`）
- legacy `None` 决策的 coerce 规则改为：
  - **至少一个 member 任务 `completed`** → 可映射 `DONE`。
  - **所有终态 member 任务都是 `failed`** → 映射为非成功决策（`FAILED`），**不得 `DONE`**。
- issue 相应落到 `IssueStatus.FAILED`（leader 标 done 的路径 `orchestrator.py:192-202` 附近，failed 时走 FAILED 分支）。

### 4. 前端 + API 状态映射
- API issue 序列化支持 `failed`。
- 前端 `StatusBadge`：`failed` → `[ERR]`（error 红），映射进 DESIGN.md §2.1 的状态码表（已有 IssueStatus 映射，补 failed）。

## 测试（先写失败测试再修）
- `test_squad_failed_member_attempts_do_not_close_issue_done`：建 squad issue → 模拟 2 个 failed member 尝试 → 触发 leader 再评估 → 断言 issue **不是** done。
- `test_squad_failed_member_attempts_mark_issue_failed`：同上，断言 issue == `FAILED`。
- `test_squad_one_completed_member_still_allows_done`：混合场景，有一个 completed → 仍可 done（不过度收紧）。
- 前端：detail 页所有 member taskrun failed 时不显示 `[DONE]`。

## 验收
- max-attempts timeout 链**无法**把 issue 标 done。
- API detail 返回 failed taskruns 带 `failure_reason=timeout`。
- 前端渲染 issue 失败为 `[ERR]`，不是 `[DONE]`。
- `ruff` 干净，现有 215 测试不回归。

## 边界
- 只改决策正确性 + 加状态。**不碰**执行模型（detach/heartbeat 是 PR-B/C）。
- 不引入新依赖、不重构 orchestrator 其他部分。
