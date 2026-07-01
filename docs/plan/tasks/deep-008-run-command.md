# deep-008: `ariadne run` —— 意图级一键并行入口

## Context

审查发现一个比功能缺失更根本的问题：**CLI 是按数据库表设计的，不是按用户意图设计的。** 现有 30+ 命令（`runtime-list`/`capability-list`/`taskrun-timeline`…）逐个暴露数据结构，导致连项目作者本人都无法凭直觉跑通核心场景——验收要手敲七八条命令、手动传 id。

**这不是「CLI 这种形式天然难懂」，是「这个 CLI 没有意图级入口」。** 一个工具如果作者都要查文档才能跑通，CLI 设计就是失败的，跟有没有界面无关。而且：**界面救不了烂 CLI**——直接做 GUI 只会把底层混乱原样透上来。正确顺序是先让 CLI 收敛出意图级命令，界面将来直接映射它。

**本任务**：做一条 `ariadne run` 命令，把「建 agent + 建 issue + 跑 daemon + 看结果」收敛成一条表达用户意图的命令。它同时解决三件事：作者能一眼跑通（即 M4 验收变成敲一条命令）、是界面的正确地基、面试可讲的产品判断。

**样板**：`cli.py:demo_v1`（`cli.py:618`）已经把 create_agent_profile→skill→squad→issue→enqueue→daemon 全流程串起来了。`ariadne run` 本质是把它**参数化 + 面向真实 backend + 打印 diff**。

## 核心设计决策

### 决策 0：统一入口，覆盖 multica 的两条路径（基于 multica trace）
multica trace 确认它有两条完全不同的入口操作：`issue create --assignee <agent>`（单 agent 直接干）vs `--assignee <squad>` + 后续 @mention（leader 拆解分派）。multica **没有批量并行原语**——用户想并行 N 件事得手动建 N 个 issue。

`ariadne run` 做成**统一入口**，比 multica 更收敛：
- **默认（用户拆分模式）**：`ariadne run "A" "B"` —— 用户已把任务拆好，N 个任务 → N 个 agent 直接并行。**这是 multica 没有的批量原语**（面试点：multica 要手敲 N 次 issue create）。
- **`--squad`（leader 拆解模式）**：`ariadne run --squad "重构这个项目"` —— 一个模糊大任务交给 squad leader（走 orchestrator + LLM decide），leader 读懂、拆解、分派给 members。对应 multica 的 squad 核心路径。

### 决策 1：默认模式 = N 任务 → N agent 直接并行
- 每个任务 → 独立 issue → 独立 agent → 各自 taskrun。
- 因为是**不同 issue**，deep-006 的 per-issue 串行不阻止并行（并行正发生在不同 issue 间），并发上限由 runtime `max_concurrent_taskruns` 控制。
- **默认模式不需要 LLM**：任务已由用户显式给出。避免 LLM 调用和不确定性。
- `--squad` 模式才走 orchestrator 的 LLM 委派（复用现有 `Orchestrator` + `llm_decide`，参照 `demo_v1:decide`）。

### 决策 2：按名字指派 + 不存在自动建（multica 的关键易用点）
multica 是 `--assignee "Lambda"`（agent 名字），**不是 UUID**。而 Ariadne 现有 CLI 要手敲 agent-id（UUID）——这正是「难用」的真凶之一，人记不住 UUID。
- `ariadne run` **不让用户碰 UUID**：默认模式自动为每个任务建临时 agent（`Run Agent 1/2/…`），或用 `--agent <名字>` 复用已有 agent（按 name 解析，不存在则建）。
- `--squad <名字>` 同理按 name 解析。

### 决策 3：命令形态 + 阻塞语义
```bash
# 默认：用户拆分，批量并行
ariadne run "任务A描述" "任务B描述" [选项]
# squad：leader 智能拆解
ariadne run --squad "一个模糊的大任务" [选项]
```
选项：
- `--backend/-b`（默认 dry-run；真实用 codex/claude-code）
- `--squad`（切到 leader 拆解模式；可带 squad 名字）
- `--agent <名字>`（复用/指定 agent，默认自动建临时 agent）
- `--target-repo`（默认 `.`）
- `--max-concurrent`（默认 min(cpu, 任务数)）
- `--write-workspace`（默认 false，worktree 隔离；沿用 deep-006）
- `--detach`（默认 false）

**阻塞语义（学 multica 的分步，但默认合并）**：
- 默认**阻塞**到全部完成，然后打印每个任务的 状态/耗时/diff 摘要/changed_files。适合短任务 + 本人验收，不用再敲第二条命令。
- 但要**明确提示**：长任务可 `--detach` 立即返回，之后用 `taskrun-timeline <id>` 看进度——避免用户以为卡死。multica 正是靠 `issue create` + `issue run-messages` 两步来不阻塞，我们保留这个能力但默认合并成一条。

### 决策 4：复用，不新造
- 复用 `Store`、`Daemon`、`get_backend`、`Orchestrator`、隔离优先、并发上限——`ariadne run` 是**编排层薄封装**，零新业务逻辑。
- 默认模式参照 `demo_v1` 的 store/daemon 组装但去掉假 decide；`--squad` 模式复用 `demo_v1:decide` 那套 orchestrator 接线。
- 结果打印复用 deep-006 已存进 taskrun result 的 diff/changed_files。

## 落点
- `src/ariadne/cli.py`：新增 `run` 命令。参照 `demo_v1`（`:618`）的 store/daemon 组装，但用真实参数 + N-issue 映射 + 结果打印。
- 可能抽一个 `src/ariadne/runner.py`（若 `run` 逻辑超过 ~40 行，从 cli 抽出保持 cli 薄）——遵循 CLI 层零业务逻辑原则。
- 不改 store/daemon/backends 的行为，只组合它们。

## 明确不做
- 不做交互式向导（本轮是一条命令直给；向导是后续，意图级命令是它的基础）。
- 不做界面（CLI 地基先扎实）。
- **不新写 squad-leader 委派逻辑**——`--squad` 模式复用现有 `Orchestrator` + `llm_decide`，只是接进 `run` 入口，不改委派本身。
- `--squad` 模式的 LLM 需要 API key；无 key 时走确定性 fallback（现有 `deterministic_decide`），或提示用户。

## 验证
```bash
uv run ruff check src/ariadne/
uv run pytest -q                          # 现有测试不回归
# dry-run 冒烟(零成本) —— 默认批量并行模式
uv run ariadne run "写个 hello 函数" "写个 add 函数" --backend dry-run
# dry-run 冒烟 —— squad 模式(无 key 走确定性 fallback)
uv run ariadne run --squad "重构这个模块" --backend dry-run
# 真实(本人执行,烧 token):在测试 git 仓库
uv run ariadne run "在 README 加简介" "建 CONTRIBUTING 骨架" --backend codex --target-repo ~/tmp/ariadne-target
```
- 通过标准：一条命令跑完，终端直接打印每个任务的 状态/耗时/diff，**无需再敲其他命令**、**无需碰 UUID**。这就是 M4「3 分钟跑通」的达成形态。
- 新增测试：默认模式建 N issue/N agent 并发 + 结果聚合打印；`--squad` 模式走 orchestrator 委派；按名字解析 agent（不存在则建）；`--detach` 立即返回。全部 dry-run 可自动化。

## 回报要求
列出：`run` 命令签名（默认 + `--squad` 两态）、是否复用 demo_v1 组装、默认模式 N-issue 是否绕开 per-issue 串行（应绕开）、`--squad` 是否复用现有 Orchestrator 而非新写、按名字指派是否让用户零 UUID、阻塞/`--detach` 语义、结果打印字段、两条 dry-run 冒烟输出、是否抽 runner.py。
