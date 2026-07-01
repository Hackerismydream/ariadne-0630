# M4 真实验收清单（本人执行，非 codex）

> M4 是唯一的成熟度硬 gate，也是把简历 benchmark 从「dry-run 模拟」变「真实数据」的唯一途径。
> 本清单照着敲即可。命令基于 origin/main 真实 CLI 核对，非凭记忆。

## 前提

```bash
cd /Users/martinlos/code/ariadne-0630
git stash                       # 暂存本地未提交的文档改动(计划/handoff 等)
git checkout main && git pull origin main
uv sync --extra dev
uv run pytest -q                # 确认基线绿(194)
```

需要机器上已装 `codex` 或 `claude` CLI 并已登录。用哪个就把下面的 `--backend` 换成 `codex` 或 `claude-code`。

**准备一个可被 agent 改动的测试 git 仓库**（不是重要项目）。隔离优先默认在 git worktree 里跑，不会碰你的主工作区——但仍建议用测试仓库。假设路径 `~/tmp/ariadne-target`：
```bash
mkdir -p ~/tmp/ariadne-target && cd ~/tmp/ariadne-target && git init && echo "# test" > README.md && git add -A && git commit -m init
cd /Users/martinlos/code/ariadne-0630
```

## 第一半：烟雾验收（dry-run，零成本，先跑）

```bash
uv run ariadne demo-v1 --output-dir .ariadne-demo-v1
```
✅ 通过标准：输出 `Ariadne Managed Agent Team Runtime v1 demo complete`，无报错。
跑不通 = 控制平面链路有问题，先修这个，不要往下。

## 第二半：真实 backend 验收（M4 硬 gate，烧真实 API）

```bash
export ARIADNE_DB=.ariadne-real.db
export TARGET=~/tmp/ariadne-target        # 你的测试仓库

# 建两个 agent(用真实 backend)
uv run ariadne agent-create --name Coder1 --backend codex
uv run ariadne agent-create --name Coder2 --backend codex
# 记下输出的两个 id: Created agent: <ID> (...)

# 建两个【不同】issue —— per-issue 串行要求不同 issue 才会并行
uv run ariadne issue-create --title "在 README 加一句项目简介" --assignee-id <Coder1-ID>
uv run ariadne issue-create --title "新建 CONTRIBUTING.md 骨架" --assignee-id <Coder2-ID>

# 掐表启动 daemon，观察两个 agent 是否真并行、能否看到各自结果
time uv run ariadne daemon-start --max-iterations 5 --target-repo $TARGET
```
✅ M4 通过标准（方案 A 的定义）：
- 从建 agent 到看到两个 agent 各自产出，**3 分钟内**。
- 两个不同 issue 的 task **并行**执行（不是一个跑完才跑另一个）。
- 能看到各自的 diff / 结果（`uv run ariadne taskrun-list`、`taskrun-timeline <id>`）。

卡住的地方就是 codex 下一个修复任务的输入 —— 记录卡在哪（装不上？跑不起？并行没生效？看不到结果？）。

## 真实 benchmark（替换简历模拟数字）

```bash
uv run ariadne benchmark-compare --tasks 4 --backend codex --max-concurrent 4
```
输出里看 `"simulated": false` + 真实 `speedup`。**这个数字才是能填进简历的真实数据。**

## 预期与提醒

- **真实加速比大概 2-3x，不是模拟的 6x。** `max-concurrent 4` + LLM 决策串行开销 + API 限流决定的。真实的 2.5x 比虚的 6x 在面试可信得多，别失望。
- **烧 token**：`--tasks 4` 跑 serial+parallel = 8 次真实执行。
- **跑完填数字**：用真实 speedup 更新 `简历-Ariadne.md` 的 benchmark 表，把「dry-run 模拟」那几行替换/补一列真实数据，保留诚实标注。

## 跑完回来告诉我
- 烟雾验收过没过
- 真实 3 分钟验收过没过、卡在哪（如果卡）
- 真实 speedup 是多少
我据此决定：是让 codex 修 M4 暴露的问题，还是直接更新简历真实数字 + 收尾。
