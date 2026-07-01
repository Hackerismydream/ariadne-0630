# CLAUDE.md — Ariadne 架构决策与边界（Claude Code 读）

> 我在本项目的角色：**架构决策 + 边界裁定 + 写 task 计划**。不写实施代码（那是 codex），
> 不定方向（那是人）。本文件是我做决策时的依据和已定边界的记录。
> 结构约定的**执行细节**见 [AGENTS.md](AGENTS.md)——本文件不重复，只记「为什么」和「决策日志」。

## 定位（一句话）

面向高强度 AI builder 的本地 agent 编排平台：把多个 coding agent(Codex/Claude/…)当可调度的执行器，一个入口并行干活、看进度、看 diff。终态是 Next.js Web 前端。秋招作品，但要做成真能用的开源产品。

**叙事原则**：主语是问题本身（多 agent 如何并行协作），不是 multica。multica 仅作内部设计参考，对外不提。

## 三方协作

| 角色 | 谁 | 职责 |
|------|----|----|
| 方向 | 人 | 优先级、要做什么、拍板 |
| 架构 | Claude Code(我) | 边界、分层、task 计划、决策裁定 |
| 实施 | codex | 按 task 文档写代码，主要通过 **Ask Matt** 工作流（Matt skill）完成，改好后提交成一个 commit |

我遇到方向问题问人；codex 遇到边界问题问我。codex 的实施走 Matt skill（Ask Matt 工作流），每个 task 收敛成一个干净 commit。

## 核心架构边界（不可让实施漂移）

### 前后端物理分离（本次新增的硬约束）
- **`backend/`(Python, 现暂在 src/ariadne/) 与 `frontend/`(Next.js) 是两个独立子项目。**
- 唯一契约 = HTTP API（REST + SSE）。前端不读后端文件、不共享代码。
- **禁止后端里塞前端**：`src/ariadne/dashboard.html` 是坏味道样本，前端上线后删除，在那之前不扩展。
- 为什么：终态是 Web，若不早钉边界，会长成「打开 Python 包里面是 HTML」的畸形，将来拆不开。

### 后端分层（issue 为核心对象）
- 严格分层：CLI/API(零业务) → 编排 → 服务(业务规则) → 持久化(纯 SQL) → 贫血模型。详见 AGENTS.md §2。
- **API 按 issue 收敛**，不暴露 15 张表。前端只需理解「issue」一个对象——这是 multica 前端「清晰」的根因，也是我们 CLI/API 的设计准绳。
- CLI(`ariadne run`) 和 API(`POST /api/issues`) **共用同一段业务逻辑**，不各写一份。

### 架构方向：朝 DDD 演进，但克制落地
- **目标是 DDD 架构**（哪怕现在还是轻量 DDD）。用 DDD 的**积木**：Repository、领域服务(Domain Service)、充血实体、聚合根、限界上下文(bounded context)。
- **但每个 DDD 积木都要自己挣得存在的理由**——不为仪式而仪式。禁止：无差别值对象、领域事件总线、CQRS、防腐层等尚无实际需求的重型构件。加一个构件前问「它现在解决了什么真问题」，答不上就不加。
- 缰绳是「优雅、简单易懂」：DDD 的价值在清晰的边界和职责，不在术语堆砌。代码读起来该像业务叙述，不像框架样板。
- 演进路径：先 Repository + Domain Service（deep-007），实体逐步从贫血转向必要的充血（把只属于某实体的规则搬进它），限界上下文随模块自然浮现，不强行预划。

### 不做（Never by design）
- multica 的 SaaS 外壳：Redis relay、event bus、多节点、多租户、认证、计费、多端(desktop/mobile)、RAG、autopilot。
- DDD 的过度仪式：见上「克制落地」。

## 当前 task 队列（依赖顺序）

```
deep-006 + M5   ✅ 已合入 main(PR #28)
deep-008  ariadne run(CLI 意图入口)          ← 当前开工
deep-009  API 按 issue 收敛 + SSE + CORS       依赖 008
deep-010  Next.js 前端(3 页面:列表/建/详情)   依赖 009
deep-007  轻量分层重构(Repository+Service)     可并行/穿插,纯重构
M4        真实 backend 验收 + 真实 benchmark    人本人跑,唯一硬 gate
（最后）  src/ → backend/ 机械迁移              待 007/008/009 落定再做
```

顺序铁律：**009 必须在 010 前**（否则前端是调不通的空壳，已由 api.py 现状证实：无 POST 建 issue、无 CORS、无真 SSE）。

## 决策日志（关键裁定 + 理由，防反复）

- **不上完整 DDD** → 修正：**朝 DDD 演进但克制落地**（见「架构方向」）。用 Repository/领域服务/充血实体/聚合根/限界上下文的积木，但每个积木要挣得理由，拒绝过度仪式。缰绳=优雅简单易懂。
- **claim 分层用方案 A** → 业务判断归 service、repo 只做持久化原语。理由：claim 是高频变更点(deep-006 证明)，越该清晰分层。见 deep-007-claim-layering-comparison.md。
- **安全用隔离优先，非确认 gate** → 安全是架构属性(默认 worktree 隔离)，不是弹窗。deep-006 已落地。
- **`ariadne run` 统一入口** → 默认「用户拆分 N 任务并行」(比 multica 的无批量原语更进一步) + `--squad`「leader 拆解」(复用现有 orchestrator)。
- **前端 Next.js，但只抄 multica 的信息架构不抄体量** → issue 为核心 + 3 页面(列表/建/详情实时进度+diff)，跳过看板/Cmd+K/线程评论/多端。
- **性能数字诚实** → 6x 是 dry-run 模拟，已全标注；真实值待 M4 人工跑，禁编造。

## 我做决策时的自查

- 这个改动落在哪一层？跨层了吗？→ 跨层要么拆，要么明确理由。
- 前端的东西有没有漏进后端？后端细节有没有漏进前端 API？
- 是不是又朝 multica 的 SaaS 方向漂移了？→ 拉回 local-first。
- 有没有制造超前污染（文档写了代码还没有的功能）？→ 只描述真实状态。
- 这个抽象现在真需要，还是过早？→ 过早则砍。
