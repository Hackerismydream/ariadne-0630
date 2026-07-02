# 讨论存档 · 2026-07-02 · 主故事定位 + 常驻 daemon 约束

> 形式：人（方向）× Claude Code（架构裁定）的对话轨迹。只讨论/计划/架构，不写代码。
> 最终拍板的决策会收敛进 `docs/adr/`，本文件保留推理过程，防止反复。

---

## 给 fable 的导览（冷读入口，先读这段）

你（fable）被请来的方式是——**放开发散，不必守我们之前的规划**。下面这些是 opus 和人聊出来的结论，给你当**参考和起点**，不是给你戴的镣铐。你更聪明，若看到更好的路子，推翻它们、给出理由即可；我们要的是最优解，不是让你复述我们。

**我们聊到哪了（供你快速上下文，可全盘质疑）**
1. 主 thesis 试拟为：**agent team 的 harness，建在模型吃不掉的协调层（隔离/工作单元/可见性/证据）**。见「主 thesis」节。
2. 产品调性：**派活给 agent 然后走开**（"让大家懒一点"）。
3. 我们否掉了 codex 的 5 个新顶层对象（`ProjectVersionDelivery` 等），倾向 Issue 就是交付单元。见「codex 的初判」节。
4. 常驻 daemon 定为硬约束；不照抄 multica 的 server，而**把 server 角色折叠进单个本地 daemon**。见「关键陷阱」节。

**已经动工、别返工的部分**
- **daemon 后台化 + running 孤儿恢复 = deep-011-F**，已落成架构文档 + task + ADR-0014，codex 在做（B→C→D→E→F）。这块是被约束逼出的解，除非你发现硬伤，否则别推翻——它在实施中。
- 冻结边界：Redis/WS/Postgres/多租户/RAG（Never by design）。这是产品定位，不是技术妥协。

**最想请你发散的地方**
- **智能路由**：现在雏形、默认关着（见「验证 · 路由现状」节）。要当 Agent 岗 showcase，缺"能力声明"层。文末「路由：留给 fable 的开放设计题」摆了几个 fork 和我们的一些顾虑——**但那只是我们的思路，你完全可以另起炉灶**。产出方向后再落 ADR/task。
- 顺带：上面 4 条结论你若觉得哪条站不住（尤其 #1 主 thesis、#4 单 daemon 折叠），直接挑。人明确说了要你发散，不要你服从。

---

## 触发问题

作为校招强项目投 Agent 开发岗（体现 Agent 能力面 + 后端能力），Ariadne 的**故事**还欠什么？愿景锚定：**本地 Agent team，面向 AI builder 提效**。

## codex 的初判（被部分否掉）

codex 结论：不缺"多 agent 能跑"的机制，缺一个更硬的主故事。建议加 5 个新顶层对象/契约：`ProjectVersionDelivery`、`BuildContextManifest`、`EvidenceManifest`、per-role artifact contract、real runtime 闭环。

**裁定：诊断对（缺主故事），药方反（在往里加名词）。** 理由：
- 每加一个顶层名词就多一个面试被追问的把柄，校招一人项目喊 "control plane" 显过度包装。
- 与 CLAUDE.md 已拍的边界冲突：「API 按 issue 收敛，不暴露 15 张表」。再造 `ProjectVersionDelivery` 顶层对象正是要躲的「打开是一堆表」坏味道。**Issue 本身就是那个可验证交付单元**，不在其上再叠一层。
- codex #1（统一顶层对象）直接否掉。

## 真实痛点（人纠正后确立）

不是编出来的"agent 会撒谎/信任层"，而是**人亲历的痛**：一直手动调度多个 code agent，让 Claude 写架构、Codex 去执行，发挥各自作用——但**全靠手动调度，很麻烦**。

→ 引出三个核心能力：
1. **智能路由**（谁干什么，异构能力匹配）
2. **交接 handoff**（上游产物结构化喂下游）
3. **并行隔离**（deep-006 已落地 worktree）

## 关键辨析（避免踩浅）

**同质 fan-out ≠ 异构角色编排。** 市面多数"多 agent"是把一个任务复制给 N 个同类 worker（浅、常见）。用户的真实痛是**异构、角色分工、带交接**。对 Agent 开发岗尤其致命：如果编排是"人决定谁干、工具只并行执行"，agent 能力面很薄，本质是漂亮的异构 CLI 调度器。真正 agentic 的是 **leader 自己做能力路由**。

→ CLAUDE.md 里的 fork 已写好答案：
- **默认模式**（人拆 N 任务并行）= 后端深度，agent 味淡 → 当可靠底座
- **`--squad`**（leader 拆解 + 路由）= agentic 核心 → **深度投资压这里，扛 Agent 岗叙事**

## "为什么那么多人用 multica" 的辨析

multica 官网文案（"assign an issue the way you'd hand work to a teammate; it executes, reports progress, replies in comments; open a chat and talk to it directly"）**一个字没提路由/交接/隔离**——全在讲**体验**。

**结论：三个核心（路由/交接/隔离）是引擎不是卖点。** 人爱的是「派活给同事然后走开」的手感（熟悉的 issue 心智模型 + 异步可见性 + agent 是 workspace 一等公民）。引擎全在用户看不见处。
→ 硬约束：**引擎做到证明后端/agent 功力，委派 UX 做到 demo 让人觉得"像 team 而不是 CLI"，两个都要，别只做引擎那一半。**

## 主 thesis（拍定）

> **agent team 的 harness，建在模型吃不掉的协调层（工作单元 + 隔离 + 可见性 + 证据）。模型越强它越强，而不是被模型吃掉。**

来自人的原话思想：「搭好 agent team 的 harness，模型进化 / code agent 变强，harness 也跟着变强——这是一种沉淀」。

**拧紧：harness 沉不沉淀，取决于建在哪一层。** 按抗模型进化排序：
- **隔离 = 最扛揍**：worktree 是文件系统属性，与模型强弱无关，永不被吃。
- **交接 = 中等**：context 变长 / 共享 memory 后，显式 handoff 必要性下降，但协调问题不消失，形式会变。
- **路由 = 最易被吃**：今天 Claude 管架构/Codex 管执行是在**补模型能力差**；模型收敛后"派谁"越来越不重要。

标准答案（面试被问"凭什么不像 LangChain 被模型吃掉"）：LangChain 抽象的是**模型的推理过程**（chains/prompt template），模型自己变强就不需要脚手架。Ariadne 的 harness 建在**模型能力之外**的东西（文件系统隔离、工作单元生命周期、人类可见性、崩溃恢复、证据落盘），模型再强也不会替你做。

**路由定位（拍定）：当下 showcase（体现 agent 深度），不是地基。地基是协调+安全层。** 即使三年后路由废了，项目内核还在。人补充：2026-07 这个时间节点，智能路由仍是很强的 Agent 开发岗叙事——认可路由当亮点。

## 产品调性（拍定）

**「派活给 agent 然后走开」**——符合调性「解放大家、让大家懒一点」。

## "走开"逼出的架构约束（本轮核心推论）

选"派活走开" = 否掉一整类实现。**"走开"意味着 agent 必须活得比终端长。**
- agent 若是 CLI 子进程 → 一走就死 → "走开"是假的，调性崩。
- 真"走开" → 必须有**活着的后台 daemon**，agent 挂它下面，CLI 只是它的客户端。

**常驻 daemon = 硬约束（拍定）。** multica 也这么做（`~/code/multica`）。不再争。

"走开"自动逼出三个必答问题（每个都是后端深度，不用额外造对象）：
1. **进程存活**：agent 挂在谁下面活着？（daemon vs CLI 子进程）→ 决定"走开"真假
2. **崩溃恢复**：走开时机器睡/daemon 挂，回来 run 什么状态？能续还是丢？→ 后端硬度 L2
3. **回来的可见性**：靠什么看结果？daemon 记 timeline + client 重连读 → 复刻 multica "reports progress"

**⚠ 报警**：codex 上一轮原话提到 **"detached squad daemon 路径"是硬缺口**——即当前代码可能托不住这个调性。地基若塌，路由做得再花也是沙上建楼。**顺序铁律：先确认 daemon/恢复托得住"走开"，再打磨路由 showcase。**

## 后端硬度阶梯（面试可用）

- L0：for 循环调 API（人人有）
- L1：并行跑 + 收结果（很多人）
- L2：状态机，进程重启后 run 能续（少数，后端信号）
- L3：隔离，并行 agent 不互踩（更少，worktree）
- L4：可验证完成，拒绝相信 agent 自报（几乎没人到）

Ariadne 有底子站到 L3+，别的项目多在 L2/L3。

## 智能路由的设计原则（避免 20 行 if-else）

- **能力声明**：每个 backend 带 capability profile（Claude=架构/推理，Codex=执行/补丁），路由 = 任务需求匹配 profile，不硬编码模型名。加新 agent 只改声明——这是"沉淀"在路由层的体现。
- **leader 做决策不是分类**：agentic 含量在"拆 + 判断需求"，不在最后一步匹配。
- **克制**：路由是 showcase 不是地基。先保地基（daemon/恢复）真托得住，再打磨路由。别把钱砸错层。

## 待代码求证（本轮 3 个 subagent 并行核查）

1. multica daemon 存活 + task lifecycle + client 重连模型（参考）
2. ariadne：daemon 进程模型 / `--squad` 是否跑通 / CLI 退出后 agent 死不死 / 崩溃恢复
3. ariadne：backends 是否真跑 / capability profile 有无 / 路由现状（智能 vs 人指定）

> 求证结论见下方「验证」节（subagent 回来后补）。

## 方向冲突裁定（拍定）

发现仓库存在两代方向：旧的 `docs/INDEX.md` frozen constraints（`No frontend/web UI`、`CLI-first, FastAPI optional`、`<3000 lines`、`manual issue creation`）vs 新的 CLAUDE.md（要 Next.js 前端 + squad 路由）。

**裁定：以 CLAUDE.md 为准，INDEX 那份 frozen constraints 作废。往前冲，不往回收。**
（INDEX 的文档索引表保留，过时约束段落待收口后清理。）

## 验证（subagent 结论）

### 路由 / backends 现状（已核，backends.py/orchestrator.py/llm_decide.py）
一句话：**路由骨架在，但默认「没脑子」，且真·智能路由默认关着。**
- backends **真**：Claude/Codex 真 `subprocess.Popen` 起 CLI，非 stub。差别只是命令模板（`codex exec` vs `claude --print --output-format json`）。CLI 缺失返回 blocked 而非假成功。
- **dry-run 是默认且会静默兜底**：CLI/API/daemon 默认 `backend="dry-run"`，未知 backend 名静默退化成 dry-run no-op（`daemon.py:208-209`）。→ 这正是"假完成"风险的来源。
- **无能力元数据**：没有 "Claude=架构/Codex=执行" 这种 per-backend profile。`preferred_capabilities` 存的是 backend 名字串，且只当**权限 gate**用（`policy.py:156`），不参与选谁。
- **默认路由退化成 `[0]`**：非 squad 取 `agent.backends[0]`；squad 默认 `deterministic_decide` 取 `roster[0]+backends[0]`（第一个成员、第一个 backend）。
- **智能路由只在配了 API key 时活**：`llm_decide`（默认 DeepSeek），没 key / 失败 3 次塌回 deterministic。即便活了也只看 backend 名字串 + skills（无能力元数据可看）。已有 `route_accuracy` benchmark，说明路由被当作可评估能力，但仍雏形。
- **squad 无真·拆解**：一次只 delegate 一个子任务，顺序推进（"one delegation per activation"），不是一次拆成 fan-out plan。leader 被禁止自己干活。

→ 印证上轮判断：智能路由=雏形、默认关着。要扛 Agent 岗叙事，缺的正是**能力声明层**（= "沉淀"思想在路由层的落地）。

### daemon 存活 / 崩溃恢复现状（已核，daemon.py/runner.py/backends.py/task_repo.py）
**总判断：现在的代码托不住"派活走开"。没有真正的常驻托管进程。**
- **daemon 不是常驻进程，是"被驱动一圈就退"的同步循环对象**（`daemon.py:4` 自注 "Synchronous loop — no threads, no asyncio"）。靠调用它的那个 Python 进程阻塞在 `start()` 里活着。
- agent 执行**同步阻塞在发起命令的进程**里：`backend.execute()` → `subprocess.Popen(start_new_session=True)` + `proc.wait()`。父进程（CLI）死了 wait 就断，`start_new_session` 只是进程组隔离，**不是后台化**。
- **全仓无 nohup/setsid/双fork/daemonize**。`--detach` 语义只是"建好 issue/taskrun 立即返回"，把活留 DB 里，**不负责让任何进程继续执行**。
- **detached squad 没有消费者**（= codex 说的那个缺口的真面目）：detach 分支入队 leader task 后直接 return，不构造 daemon/orchestrator（`runner.py:265-276`）；API 又对 codex/claude-code 强制 `detach=True`（`api.py:354`）。→ squad leader task 落库即 queued，无人认领，除非用户**另外手动开着**一个 `daemon-start`，而代码里没有这层衔接。
- **崩溃恢复只覆盖 `claimed`，不覆盖 `running`**：`recover_stale_claims` 只 `SELECT ... WHERE status='claimed'`（`task_repo.py:295`）。task 一旦进 `running`，daemon 中途被杀 → **永久卡 running，无人复活**。
- **heartbeat 只写不判活**：`last_heartbeat_at` / lease `expires_at` 有字段有写入，但没有消费端据此判定 runtime 掉线并抢占其 running task。

→ 离"真能走开"至少差 4 件具体的事：①进程后台化 ②detached squad 的常驻消费者 ③running 孤儿 task 回收 ④heartbeat 判活+抢占。

### multica daemon 参考（已核，~/code/multica，Go）
**总架构 = server-mediated（服务端中介）**：中心 server(Postgres) 是唯一权威状态 + 任务队列，本地 daemon 是执行器，Web/CLI 是薄客户端。三者都不直接互跑，各自读写 server。
- **daemon 常驻**：`multica daemon start` 后台拉起，日志 `~/.multica/daemon.log`。一堆常驻 goroutine 循环（workspaceSync/taskWakeup/heartbeat/gc/...）+ 主循环 pollLoop。
- **agent 是短命子进程**：daemon 不"挂"agent 常驻，是收到 task 时临时 spawn agent CLI（claude/codex），跑完即退。存活靠 daemon 心跳，不靠 agent 常驻。
- **存活三层**：daemon 每 15s 心跳 → server 写 Redis TTL key + DB last_seen backstop；server 端 sweeper 每 30s 扫，>150s 无心跳标 offline。
- **task 状态权威在 server Postgres**（不在 daemon 内存），6 状态：queued/dispatched/running/completed/failed/cancelled。**超时兜底状态机**：dispatched>300s→failed、running>9000s→failed、queued>2h 过期。崩溃恢复走 `RecoverOrphanedTasks`。
- **"走开后回来看进度"靠 server 持久化 + client 无状态重连**：agent 执行时 daemon 把进度/每条消息流式回写 server 的 `task_message` 表（按 seq 单调序号）。用户回来任意 client 增量拉 `--since <seq>`。**进度从不驻留在 client 或 agent 里**，所以断开重连、多端同读天然成立。
- **两条通道**：claim（daemon 每 3s 轮询认领 queued task）+ wakeup（WS 推，有新 task 立即触发，断了 fallback 回轮询）。
- **squad delegation 复用 comment+@mention**：leader 派活 = 发带 @mention 的评论 → server 解析 mention → 给被 @ 的 member 入队 queued task → member daemon claim 执行。没有独立 RPC 派发通道。leader "dispatch 后停手" 契约。

---

## ⚠ 关键陷阱：multica 的存活靠 server，不是靠 daemon（ariadne 不能照抄）

multica "走开可重连" 的**真正支点是中心 server(Postgres/Redis) 持久化一切**——daemon 只是执行器，进度从不留在 client/agent。这套依赖 Postgres + Redis + server 进程 + WS。

**但 ariadne 的 frozen 边界是 local-first、SQLite、无 server、无 Redis、单机。** 直接照抄 = 变成 multica clone，违反 CLAUDE.md「Never by design」。

→ **ariadne 的正解：把 multica 里"server 扮演的权威角色"折叠进本地常驻 daemon 自己。** 即：
- multica = client → **server(权威+队列+持久化)** → daemon(执行) → agent
- ariadne = client(CLI/前端) → **常驻 daemon(权威+队列+持久化, 直接坐在 SQLite 上)** → agent

ariadne 的 daemon 要同时扮演 multica 的 server + daemon 两个角色。这反而**更简单**（少一跳网络、少一个进程、少 Postgres/Redis），且正是 local-first 的价值主张。可直接搬的具体机制（去掉 server 中介后）：
1. **进度流式落盘 + 序号游标**：agent 执行时把每条消息写进 SQLite `task_message`(task_id, seq)，client 用 `--since <seq>` 增量拉。→ 这就是"走开回来看进度"的技术本体，且纯 SQLite 可做。
2. **超时兜底状态机**：dispatched/running/queued 各自超时阈值 → 自动判 failed/过期。→ 正好补上 ariadne「running 孤儿无人回收」的缺口。
3. **心跳判活**（单机可极简）：daemon 写 heartbeat + 启动时扫超时 running task 回收。单机不需要 Redis TTL，SQLite 一张表 + 启动扫描即可。

## 后台化路子（待人拍，三选一）

1. **`setsid`/双 fork 自做 Unix daemon**：最贴 local-first、零依赖；要自己管 PID 文件/日志/僵尸回收。
2. **launchd/systemd 交给系统**：最稳；平台绑定、装起来重。
3. **前台 `daemon-start` + 文档教用户 `nohup`**：最省事；"走开"体验最糙。
（multica 走的是 1 的思路：`daemon start` 后台化 + 日志到 `~/.multica/daemon.log`。）

## 本轮收口：已落地

待拍清单在对话中全部拍完，产出已进仓库：

- **daemon 后台化路子**：走 `os.setsid` + fork 自守护（三选一里的方案 1，贴 local-first，multica 同思路）。
- **"常驻 daemon + running 恢复"定位**：确认是硬前置，但不是新造 `deep-008.5`（那是旧队列幻觉）——它是 **deep-011 的收尾 PR-F**，因为一半已在 B/C 里。
- 产出文件：
  - `docs/architecture/daemon-lifecycle.md`（架构规格）
  - `docs/plan/tasks/deep-011-F-daemon-lifecycle-recovery.md`（codex 死框 task）
  - `docs/adr/0014-daemon-lifecycle-and-crash-recovery.md`（决策记录，Proposed）
  - `task-state-machine.md` 补 `running→failed`/孤儿 `failed→queued` 两条边
  - `deep-011-INDEX.md` + `architecture/README.md` 索引挂接
- 依赖顺序：F 依赖 B+C，codex 现到 C，不冲突。

---

## 路由：留给 fable 的开放设计题

> 本轮**有意**没拍。opus 认为这是有多个合法解的开放设计，现在锤定是过早（违背 CLAUDE.md「这个抽象现在真需要还是过早」的缰绳），且会抢掉换 fable 来思考的那一手。下面只摆 fork 和约束，**不预设答案**。

**现状（已核，见「验证 · 路由现状」节）**：backends 真能跑，但路由默认"没脑子"——非 squad 取 `backends[0]`，squad 默认 `deterministic_decide` 取 `roster[0]+backends[0]`；真·智能路由只在配了 API key 时活，且只看 backend 名字串（**无任何能力元数据**）。

**要解的问题**：让路由从 `[0]` 变成"任务需求匹配 agent 能力"，且这一层要**建在模型吃不掉的地方**（呼应主 thesis）。

**fork 1 — 能力声明的形状**
- A：per-backend 静态 profile（Claude=架构/推理，Codex=执行/补丁），人工声明。
- B：per-agent-profile 能力标签（已有 `preferred_capabilities`，但现在只当权限 gate 用）。
- C：让 leader 在 briefing 里读到能力描述，由 LLM 自己判断（动态，无静态 schema）。
- 张力：静态声明可解释、可测、可当"沉淀"；但越静态越像人肉 if-else，agentic 味越淡。

**fork 2 — leader 做真·拆解还是单步委派**
- 现状：一次只 delegate 一个子任务，顺序推进（"one delegation per activation"）。
- A：保持单步（简单、可控、事件循环已实现）。
- B：一次拆成 fan-out plan（更像"team"，但要处理依赖/并发/失败回滚，复杂度陡增）。
- 判断依据：demo 里"像 team"的手感 vs 后端复杂度，哪个对秋招叙事更值。

**fork 3 — 怎么保证路由是"沉淀层"而非被模型吃掉的那半**
- 已定判断：路由是**当下 showcase**（2026-07 节点强），但**最易被模型进化吃掉**（模型收敛后"派谁"变不重要）。
- 开放题：能不能把路由设计成"即使模型全能了仍有价值"的形态？（如：路由决策落盘成可复盘的 evidence、能力声明本身成为可积累的资产、路由 accuracy 成为可 benchmark 的指标——已有 `route_accuracy` 雏形。）
- 或者：**坦然承认路由是短期亮点**，把长期地基押在隔离/可见性/证据，路由只做到"够 demo 惊艳"即止，不过度投资。这也是一个合法解。

**约束（不可破）**：守 local-first（不引 Redis/WS/Postgres）；能力声明要可测；别把路由做成 200 行 if-else 也别做成需要常驻 LLM 才能跑的重构件；每加一个构件要挣得存在理由（CLAUDE.md 缰绳）。
