# claim 分层方案对比（deep-007 边界决策）

> 你要看两种切法的真实代码再拍板。下面基于 main 上 `store.py:918` 的
> `claim_taskrun_for_runtime_machine` 真实实现（60+ 行，混了事务/容量检查/
> per-issue 串行/能力匹配/SQL）。

## 现状：一个方法混了 4 种职责

```python
def claim_taskrun_for_runtime_machine(self, runtime_machine_id, lease_seconds=60):
    with self._lock:
        self._conn.execute("BEGIN IMMEDIATE")          # ← 事务控制
        runtime_machine = self._conn.execute(...)       # ← 持久化
        active = self._conn.execute("SELECT COUNT(*)...")# ← 持久化
        if active >= runtime_machine["max_concurrent"]:  # ← 业务规则(容量)
            return None
        queued = self._conn.execute(                     # ← 持久化 + 业务规则
            "...NOT EXISTS(...active.status IN (claimed,preparing,running))")  # per-issue 串行
        for candidate in queued:                         # ← 业务规则(能力匹配)
            ... match capability ...
        # + 建 lease + 更新 task 状态                     # ← 持久化
```

问题：读这个方法要同时理解「怎么存」和「为什么这么判断」。改容量策略要在 SQL 堆里找。测容量逻辑必须连数据库。

---

## 方案 A：业务判断归 Service，repo 只做持久化

**repo 提供细粒度持久化原语（都是纯 SQL，可单独测）：**
```python
# task_repo.py
def count_active_leases(self, runtime_machine_id) -> int: ...
def select_claimable_tasks(self) -> list[Row]:
    """queued 且所在 issue 无活跃兄弟 task（per-issue 串行的 SQL 部分）"""
def select_available_capabilities(self, runtime_machine_id) -> list[Row]: ...
def mark_claimed(self, task_id, lease) -> None: ...
```

**service 编排业务规则（无 SQL，读起来就是业务流程）：**
```python
# task_service.py
def claim_for_runtime(self, runtime_machine_id, lease_seconds=60) -> TaskRunClaim | None:
    with self.repo.transaction():                        # 事务边界在 service
        machine = self.repo.get_runtime_machine(runtime_machine_id)
        if machine is None:
            return None
        if self.repo.count_active_leases(runtime_machine_id) >= machine.max_concurrent_taskruns:
            return None                                  # 容量规则,一眼可见
        for task in self.repo.select_claimable_tasks():  # per-issue 串行已在 repo SQL 保证
            capability = self._match_capability(task, self.repo.select_available_capabilities(...))
            if capability:
                return self.repo.mark_claimed(task.id, self._make_lease(...))
        return None
```

**优点**
- 业务规则集中、无 SQL 噪声，`claim_for_runtime` 读起来就是「查容量→取候选→配能力→占用」的业务流程。
- 容量/能力匹配逻辑可脱离数据库单测（repo 可 mock）。
- repo 原语可复用（`count_active_leases` 别处也能用）。

**代价**
- claim 逻辑跨两层：读全貌要看 service + repo 两个文件。
- 事务边界要设计好（service 开事务，repo 方法在事务内执行）——这是唯一的技术难点。
- per-issue 串行的 SQL 仍在 repo（因为它是 SQL 层的 NOT EXISTS），只是「为什么串行」的注释在 service。

---

## 方案 B：claim 整块留在 Service，repo 不拆那么细

**repo 只做最粗的持久化，claim 整个方法搬进 service（含它自己的 SQL）：**
```python
# task_service.py
def claim_for_runtime(self, runtime_machine_id, lease_seconds=60) -> TaskRunClaim | None:
    with self.repo.connection() as conn:                 # 借 repo 的连接
        conn.execute("BEGIN IMMEDIATE")
        machine = conn.execute("SELECT * FROM runtime_machine WHERE id=?", ...)
        if active_leases(conn, ...) >= machine["max_concurrent_taskruns"]:
            return None
        queued = conn.execute("...NOT EXISTS(...)")       # SQL 仍和业务混在一起
        ... 能力匹配 + 建 lease ...
```

**优点**
- claim 全貌在一个方法里，不用跳文件。
- 事务边界简单（就在这一个方法内）。
- 迁移成本低：几乎是把现有方法整体搬个位置。

**代价**
- 没真正解决「持久化 + 业务混在一起」——只是从 store 搬到 service，方法本身还是 60 行混合体。
- 无法脱离数据库测容量/能力逻辑。
- service 里出现裸 SQL，破坏「service 无 SQL」的干净边界。

---

## 我的推荐：方案 A

理由：
1. **它才真正解决你最初的抱怨**（「代码臃肿、职责不清」）。B 只是搬家，A 是真分层。
2. **面试可讲**：「我把 claim 拆成 repo 持久化原语 + service 业务编排，容量和能力匹配逻辑能脱离 DB 单测」——这是能展示的架构判断。B 讲不出这个。
3. 事务边界那个「难点」其实是标准做法：service 持有 repo，repo 暴露一个 `transaction()` 上下文管理器，service 在里面调多个 repo 方法。一次设计，全项目复用。

**唯一该用 B 的情况**：如果 claim 逻辑未来几乎不会改、且你觉得跨两层读全貌的成本 > 职责分离的收益。但 deep-006 刚证明 claim 是**高频变更点**（per-issue 串行、并发上限都是最近加的），所以它更该被清晰分层——A 胜。

## 建议
选 A。我据此把 deep-007 的阶段 2（Service 层）细化：定义 repo 的 `transaction()` 契约 + claim 的 repo 原语清单 + service 编排骨架。其余实体（get/list/create 纯 CRUD）不涉及这个争议，直接 repo 化即可。
