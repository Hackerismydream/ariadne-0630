# deep-007: 轻量分层重构（Repository + Service，拆解 God Object）

## Context

代码审查发现 `store.py` 已达 2093 行，是一个 God Object：一个文件塞了 13 个实体（runtime_machine/capability/lease、task/taskrun、issue、skill、agent_profile、squad、benchmark_run、leader_decision、timeline）的全部 CRUD + 状态机业务逻辑，含 49 处重复的 `_row_to_*` 转换样板。

**方向：朝 DDD 演进，克制落地**（用户决策，见 CLAUDE.md「架构方向」）。目标是 DDD 架构，用它的积木——Repository、Domain Service、充血实体、聚合根、限界上下文——但**每个积木要挣得存在的理由**，拒绝过度仪式（无差别值对象、领域事件总线、CQRS、防腐层，无真实需求就不加）。缰绳是「优雅、简单易懂」：代码读起来像业务叙述，不像框架样板。本任务是这条演进路径的第一步：先立 Repository + Domain Service，实体按需从贫血转向必要的充血。

**采用轻量两层分层**：
- **Repository 层**：纯持久化，一个实体一个 repo，消灭 God Object 和重复样板。
- **Service 层**：状态机业务逻辑（claim/retry/complete/fail/recover），从 store 里剥出来，给 daemon/orchestrator 调用。

审查确认现状：**没有 service 层**（README 画的是理想图），业务逻辑散在 store.py（状态转移）、daemon.py（执行编排）、orchestrator.py（委派 + 直接调 store 十几处）。本任务把边界划清。

**前置依赖：必须在 deep-006 阶段 A/B 完成后做**——deep-006 改 claim 逻辑，本任务移动 claim 逻辑，同时做会冲突。deep-006 落地后再拆，且有 194 测试兜底，是安全的机械重构。

**执行铁律**：纯重构，**不改任何行为**。194 测试全程绿（重构的唯一正确性判据就是测试不变还全绿）。ruff 零告警。每个实体的迁移一个 commit，便于回滚。

---

## 阶段 1：Repository 层（拆 God Object）

把 `store.py` 拆成 `store/` 包，一个实体域一个 repo 文件：

```
src/ariadne/store/
├── __init__.py        # 导出 Store（保持对外 API 不变，见下）
├── base.py            # 连接、WAL、事务、泛型 _row_to 样板
├── task_repo.py       # task/taskrun 的 CRUD（不含状态机业务，那些进 service）
├── runtime_repo.py    # runtime_machine/capability/lease CRUD
├── issue_repo.py      # issue/timeline_event CRUD
├── squad_repo.py      # squad/member/leader_decision CRUD
├── skill_repo.py      # skill/agent_profile/agent CRUD
└── benchmark_repo.py  # benchmark_run CRUD
```

### 1.1 base.py 消灭 49 处 `_row_to_*` 重复
- 提供一个泛型 `def row_to(model_cls: type[T], row: sqlite3.Row) -> T`，用 Pydantic 的字段名自动映射 + JSON 字段（`*_json`）自动反序列化。
- 各 repo 用它替代手写的 49 个 `_row_to_xxx`。保留少数需要特殊处理的（如嵌套 JSON）作为覆盖。
- 连接管理、`PRAGMA journal_mode=WAL`、`_lock`、事务上下文都归 base。

### 1.2 保持对外 API 不变（关键，降风险）
- `Store` 类仍存在于 `store/__init__.py`，作为**门面**（facade）：内部持有各 repo 实例，把现有方法名委派过去。
- 即 `store.get_task(id)` 仍可用 → 内部转 `self._task_repo.get(id)`。
- 这样 daemon/orchestrator/cli/api/eval 的所有 `self.store.xxx` 调用**零改动**，194 测试也不用改。拆分对调用方透明。
- 好处：重构风险被 facade 隔离，可以一个 repo 一个 repo 迁移，每步测试全绿。

### 1.3 迁移顺序（每个一 commit）
base → benchmark_repo（最独立）→ skill_repo → issue_repo → runtime_repo → squad_repo → task_repo（最核心，最后）。每迁一个，跑 `pytest -q` 确认 194 绿。

---

## 阶段 2：Service 层（剥离状态机业务逻辑）

把 store 里的**业务方法**（不是纯 CRUD）剥到 service：

```
src/ariadne/service/
├── __init__.py
├── task_service.py     # claim/start/complete/fail/retry/cancel/recover_stale
└── lease_service.py    # 租约 acquire/heartbeat/release/revoke + 过期回收
```

### 2.1 剥离清单（从 store 移出的业务逻辑）
- `claim_task` / `claim_taskrun` / `claim_taskrun_for_runtime_machine`（`store.py:831/1449/1484`）
- `start/complete/fail/cancel/retry_task` + taskrun 版本（`:1488-1710`）
- `recover_stale_claims`（`:1710`）
- lease 的 `heartbeat/release/revoke`（`:971-1015`）
- 这些方法内部改为调用 repo 做持久化，业务规则（状态转移合法性、重试次数、失败分类）留在 service。

### 2.2 调用方改为经 service
- daemon.py、orchestrator.py 里直接调 store 业务方法的地方（如 orchestrator `self.store.fail_task`、`self.store.enqueue_taskrun`），改为经 `task_service`。
- **纯 CRUD 读取**（`get_task`/`get_issue`/`get_squad`）可继续直接走 store facade，不必强制过 service——service 只管**有状态转移的业务**。避免为 getter 造无意义的穿透层（anti-pattern #8）。

### 2.3 per-issue 串行 + 并发上限归属 service（方案 A，已定）

决策见 `deep-007-claim-layering-comparison.md`——**方案 A**：业务判断归 service，repo 只做纯持久化原语。claim 的 SQL 拆成细粒度 repo 方法，service 编排业务流程。

**2.3.1 repo 暴露事务契约（base.py）**
```python
# store/base.py
@contextmanager
def transaction(self):
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK，配合 self._lock。
    service 在此上下文内调用多个 repo 方法，保证原子。"""
    with self._lock:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
```
- 事务边界从此归 service（service 决定哪些 repo 调用是一个原子单元），repo 方法本身不再各自 `BEGIN IMMEDIATE`。

**2.3.2 task_repo 的 claim 持久化原语（纯 SQL，无业务判断，可单测）**
- `count_active_leases(runtime_machine_id) -> int`
- `select_claimable_tasks() -> list[Row]`：queued 且所在 issue 无活跃兄弟（per-issue 串行的 **SQL 部分**——`NOT EXISTS(...active.status IN claimed/preparing/running)` 留在这里，因为它是 SQL 层去重，不是可编程的业务分支）
- `select_available_capabilities(runtime_machine_id) -> list[Row]`
- `get_runtime_machine(id) -> Row | None`
- `mark_claimed(task_id, lease) -> None`

**2.3.3 task_service 编排（无裸 SQL，读起来即业务流程）**
```python
# service/task_service.py
def claim_for_runtime(self, runtime_machine_id, lease_seconds=60) -> TaskRunClaim | None:
    with self.repo.transaction():
        machine = self.repo.get_runtime_machine(runtime_machine_id)
        if machine is None:
            return None
        # 容量规则（业务）——一眼可见，可脱离 DB 单测
        if self.repo.count_active_leases(runtime_machine_id) >= machine.max_concurrent_taskruns:
            return None
        for task in self.repo.select_claimable_tasks():
            capability = self._match_capability(
                task, self.repo.select_available_capabilities(runtime_machine_id))
            if capability:
                return self.repo.mark_claimed(task.id, self._make_lease(...))
        return None
```
- 容量检查、能力匹配（`_match_capability`）、租约构造（`_make_lease`）是**业务规则**，留 service。
- per-issue 串行的 SQL 在 repo，但「为什么串行」的注释和语义归属写在 service（指向 deep-006 的正确性理由）。
- profile 级并发上限检查同理：repo 提供 `count_active_for_profile`，service 判断。

---

## 边界原则（划错就白拆）

- **repo = 纯持久化**：只有 SQL + row↔model。无状态机、无业务判断、无跨实体规则。
- **service = 业务规则**：状态转移合法性、重试策略、并发/串行约束、失败分类。跨 repo 的事务在 service 编排。
- **实体按需充血**：只属于某实体的规则可以搬进 models.py 的实体（充血）；跨实体规则归 Domain Service。**不为仪式引入值对象/领域事件**——有真实需求再加，加前问「解决什么真问题」。
- **facade 保对外兼容**：`Store` 门面存活，调用方无感。

## 明确不做
- 不引入 DDD 的过度仪式（无差别值对象、领域事件总线、CQRS、防腐层）——有真实需求再加。
- 不拆 cli.py/eval.py/api.py（本轮只拆 store 的 God Object + 建 service 层；其他大文件收益递减，另议）。
- 不改任何运行时行为——纯结构重构。

## 验证
```bash
uv run ruff check src/ariadne/
uv run pytest -q   # 全程保持 194 绿，一个都不能少/改
```
- 判据：重构前后测试**数量和内容都不变**，全绿。任何测试需要改动 = 说明你改了行为，停下检查。
- 每个 repo/service 迁移一个 commit，贴 `pytest -q` 输出。
- 拆完确认：`wc -l store/*.py service/*.py` 每个文件 < 500 行；`grep -c "_row_to" store/base.py` 收敛到个位数。

## 回报要求
分阶段列出：拆出哪几个 repo（各多少行）、泛型 row_to 消灭了多少重复、剥了哪些业务方法到 service、facade 是否让调用方零改动、194 测试是否全程未改动即全绿。
