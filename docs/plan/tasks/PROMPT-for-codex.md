## 任务：Ariadne `ariadne run` 意图级一键并行入口（deep-008）

你在 ariadne-0630 仓库工作。deep-006 + M5 已合入 main（PR #28），测试 194 绿。本轮只做 deep-008 一个任务。

### 动手前必读（强制前置）
1. **`AGENTS.md`** — 项目结构与边界约定（本次新增，前后端分离铁律）。
2. `docs/plan/tasks/deep-008-run-command.md` — 本任务完整设计。
3. 读完后，**先在 PR 描述或 task 文档里写结构设计**：你要新增/改哪些文件、落在哪一层、为什么。确认无误再写代码。这是 AGENTS.md §0.4 的强制要求。

### 背景
现有 30+ CLI 命令按数据库表设计（runtime-list/taskrun-timeline…），暴露 15 张表，作者本人都无法凭直觉跑通核心场景——验收要手敲七八条命令 + 手动传 UUID。做一条意图级命令收敛它。

### 要做：`ariadne run` 统一入口（两种模式）
```bash
# 默认：用户拆分，N 任务 → N agent 并行（multica 没有的批量原语）
ariadne run "任务A" "任务B" --backend codex
# squad：一个模糊大任务，LLM leader 拆解分派
ariadne run --squad "重构这个模块" --backend codex
```

### 关键决策（照 deep-008 文档执行）
1. **默认模式**：每个任务 → 独立 agent + 独立 issue → 并行执行。不同 issue 天然绕开 deep-006 的 per-issue 串行；不引入 LLM（任务已显式给出）。
2. **`--squad` 模式**：复用现有 `Orchestrator` + `llm_decide`（参照 `cli.py:demo_v1` 的 decide 接线），**不新写委派逻辑**。无 API key 走确定性 fallback。
3. **零 UUID**：自动建临时 agent，或 `--agent <名字>` 按 name 解析（不存在则建）。学 multica 的 `--assignee "名字"`，别让用户碰 UUID。
4. **阻塞语义**：默认阻塞到完成 + 打印每个任务的 状态/耗时/diff/changed_files；`--detach` 立即返回，提示用 `taskrun-timeline` 看。
5. **复用不新造**：复用 Store/Daemon/get_backend/Orchestrator/隔离/并发上限。默认模式借 demo_v1 组装（去掉假 decide），diff 复用 deep-006 已存进 result 的字段。

### 结构要求（关键，为 deep-009 铺路）
- **业务逻辑抽进 `src/ariadne/runner.py`**（若超 ~40 行），`cli.py` 只做参数解析 + 调 runner。理由：deep-009 的 `POST /api/issues` 要**复用同一段 runner 逻辑**，CLI 和 API 不各写一份。
- 遵守 AGENTS.md §2 分层：CLI 零业务逻辑，runner 做编排，不碰前端（本任务纯后端，不涉及 frontend/）。
- 不改 store/daemon/backends 的行为，只组合它们。

### 铁律
- 动手前先写结构设计确认（见上）。
- 命令/阶段一个独立 commit，做完贴 `uv run pytest -q` 输出；`ruff` 零告警。
- 新功能带新测试（dry-run 可自动化）：默认模式建 N issue/N agent 并发、`--squad` 走 orchestrator、按名字解析 agent、`--detach`。
- 现有 194 测试不回归。
- 不做界面、不引入 multica SaaS 架构、不新写 squad 委派逻辑。

### 不是你的任务
真实 backend 的 `--backend codex` 验收 + benchmark 真实数字——所有者本人跑（需真实 CLI + 凭证）。你只保证 dry-run 冒烟通过：
```bash
uv run ariadne run "写个 hello 函数" "写个 add 函数" --backend dry-run
uv run ariadne run --squad "重构模块" --backend dry-run
```

### 回报要求
先给结构设计，再实施。完成后列出：run 命令签名（默认+squad）、是否抽了 runner.py、runner 是否设计成可被 API 复用、是否复用 demo_v1/Orchestrator、默认模式是否绕开 per-issue 串行、按名字指派是否零 UUID、阻塞/detach 语义、结果打印字段、两条 dry-run 冒烟输出、194 测试是否不回归。
