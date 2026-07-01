# deep-006: 架构加固（claim 正确性 + 并发上限 + 流式执行）

## Context

deep-005（M0-M3）已在 origin/main 落地：backend 开放注册、session/MCP、Skill 表+WAL、隔离优先执行，测试 146→194。

本任务来自一次与 multica 的**架构层**对比（不是能力对比）。对比后确认 Ariadne 的控制平面建模（RuntimeLease / ExecutionPolicy / LeaderDecision + policy.py 分层）显式度已**高于** multica，backend 开放注册也已**反超** multica 的编译期 switch——这两处不要动。但有三个架构决策落后于 multica 的成熟做法，且都直接关系「高强度 builder 好用」：

1. **claim 缺 per-issue 串行 → 正确性 bug**：当前 `claim_task`（`store.py:1449`）用 `BEGIN IMMEDIATE` 保证不双claim，但**没有** multica 的核心不变量「同一 issue 同一时刻只有一个活跃 task」。squad 并行下，同一 issue 的多个 task 可能被并发 claim，导致两个 agent 同时改同一个 repo 的同一块。对目标用户是灾难。
2. **并发上限默认 = 1，与「并行」卖点自相矛盾**：`runtime_machine.max_concurrent_taskruns` 和 `agent_profile.max_concurrent_taskruns` 都默认 1（`store.py:104`/`:255`），字段存在但 claim 循环没真正按它放行并发。核心叙事是并行，默认值却是串行。
3. **backend 一次性返回，非流式**：`execute()`（`backends.py`）跑完才返回 `ExecutionResult`，中途只有 `ProgressUpdate.summary` 字符串回调。multica 的 `Backend.Execute()` 返回流式 `Session{Messages, Result}`，每条 agent 消息实时可见。对「一个界面看到进度」的体感差异很大。

**执行铁律（沿用 deep-005）**：只改真实状态、不编造性能数字、每个里程碑带测试并贴 `pytest` 输出、194 测试不回归、ruff 零告警。**每点一个独立 commit。**

---

## 阶段 A：per-issue claim 串行（正确性优先，先做）

**目标**：保证同一 issue 同一时刻最多一个活跃（claimed/preparing/running）task。照搬项目已有的 partial unique index 手法（`store.py:144` `idx_runtime_lease_one_active` 就是同样模式）。

### A.1 加 partial unique index（`store.py` task 表 DDL 后，约 `:201`）
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_one_active_per_issue
    ON task(issue_id) WHERE status IN ('claimed', 'preparing', 'running');
```
- 注意：这是新增 index，对已有 DB 需在 schema 迁移路径处理（参照现有 `task_new` 迁移块 `store.py:380` 的做法）。若已有数据违反约束，迁移要先化解（记录 warning，不静默吞）。

### A.2 claim 时跳过「已有活跃兄弟 task」的 issue（`store.py:1449` claim_task）
- 在 `SELECT ... WHERE status='queued' AND agent_id=?` 上追加子查询条件：该 task 的 issue **没有**其他处于 claimed/preparing/running 的 task。参照 multica 的 `NOT EXISTS` 子查询语义：
```sql
AND NOT EXISTS (
    SELECT 1 FROM task active
    WHERE active.issue_id = task.issue_id
      AND active.status IN ('claimed', 'preparing', 'running')
)
```
- `claim_taskrun`（`store.py:1484`）同样处理（两个 claim 路径都要改）。
- index（A.1）是硬保险，子查询（A.2）是软避让避免撞 index 报错——两者都要。

### A.3 语义决策（写进代码注释 + 后续 ADR）
- **明确**：per-issue 串行意味着「同一 issue 的多个 task 顺序执行」，而「多 agent 并行」发生在**不同 issue 之间**。这是正确的并行语义——并行来自多个独立 issue 并发，不是多个 agent 抢一个 issue。squad 委派若要并行，应拆成多个 issue 或多个独立 task 目标不同文件区域。
- 若这个语义与现有 squad 并行测试冲突，**不要**为了让测试过而放宽约束——正确性优先，改测试或改 squad 拆分逻辑。

### A.4 测试
- 同一 issue 两个 queued task，并发 claim 只有一个进 claimed，另一个等前一个终结后才可 claim。
- 不同 issue 的 task 可同时 claimed（并行不受影响）。
- index 违反场景的迁移路径测试。

---

## 阶段 B：并发上限默认值 + claim 真正尊重上限

**目标**：让「并行」成为默认行为，并让 claim 循环真正按 per-runtime / per-agent 上限放行。

### B.1 默认值（`store.py:104` runtime_machine DDL）
- `max_concurrent_taskruns` 默认从 `1` 改为合理并发（建议 `4`，与 M0 benchmark 的默认 `min(cpu_count,4)` 一致；真实上限由 daemon 运行时按 `os.cpu_count()` 决定，DDL 只给保守默认）。
- `agent_profile.max_concurrent_taskruns`（`:255`）保持默认 1 或提到 2——per-agent 串行通常是期望（一个 agent 别同时跑太多），但要可配置。

### B.2 daemon claim 循环尊重上限（`daemon.py`）
- daemon 当前 `max_concurrent_taskruns=1`（`daemon.py:99` 构造）。改为从 runtime_machine 记录读，且 claim 前检查「该 runtime 当前 running taskrun 数 < max」「该 agent_profile 当前 running 数 < 其 max」——参照 multica 的 `CountRunningTasks()` 双层检查。
- 达到上限时 claim 返回 None（无容量），不阻塞。

### B.3 测试
- runtime max=4 时，4 个不同 issue 的 task 能同时 running；第 5 个等空位。
- agent_profile max=1 时，同一 profile 的 task 串行，即使 runtime 有空位。
- 与阶段 A 组合：不同 issue 且未超上限 → 并行；同 issue → 串行。

---

## 阶段 C：流式执行（体感优先，成本高，可后置到 M4 验收之后）

**目标**：执行过程中的 agent 消息实时可见，而非跑完才返回。**先确认这是「好用」的真瓶颈再投入**（anti-pattern #8：别过早为假设的瓶颈重构）。M4 人工验收若发现「等待时界面空白」是主要痛点，才做本阶段。

### C.1 结构化 progress（低成本第一步，`models.py:457` ProgressUpdate）
- `ProgressUpdate` 现在只有 `summary: str`。加结构化字段：`message_type: str | None`（assistant/tool_use/tool_result）、`tool_name: str | None`、`content: str | None`。
- `_ShellBackend` 的流式读取处（Popen stdout 逐行）把每行解析后填结构化字段，而非只截前 200 字符塞 summary。
- 这一步不改架构（仍是回调），只让回调内容更丰富。**先只做这步，多数体感问题这步就解决。**

### C.2（评估后再定）真流式 Session
- 若 C.1 不够，再考虑把 `execute()` 返回改为可迭代 Session（yield 消息 + 最终 result）。这是大改动，影响 daemon 调用点、api.py SSE、所有 backend 子类。
- **不在本任务默认范围**。C.1 做完 + M4 验收后单独评估，写进 backlog。

---

## 明确不动（Ariadne 已反超 multica）
- **backend 开放注册**（`register_backend`）：比 multica 编译期 switch 更好，保留。
- **显式控制平面建模**（RuntimeLease/ExecutionPolicy/LeaderDecision + policy.py 分层）：显式度高于 multica，是加分项。
- 不引入 multica 的 SaaS 架构：Redis relay、event bus、metrics/analytics、多节点——单机单用户用不上。

## 优先级
A（正确性 bug）→ B（并行默认值，M0 benchmark 前提）→ C.1（结构化 progress）。**C.2 后置**，等 M4 验收确认瓶颈。

## 验证
```bash
uv run ruff check src/ariadne/
uv run pytest -q
```
- 阶段 A：per-issue 串行测试全绿；不同 issue 并行不受影响；194 + 新增测试无回归。
- 阶段 B：双层上限测试全绿；默认并行生效。
- 阶段 C.1：结构化 progress 测试；SSE dashboard 仍正常。
- 每阶段一个 commit，贴 `pytest` 输出。

## 回报要求
不要只说「架构加固完成」。列出：A 的 index/子查询改了哪、per-issue 串行语义是否与 squad 测试有冲突及如何解决、B 的默认值改成多少、C 只做了 C.1 还是评估了 C.2。
