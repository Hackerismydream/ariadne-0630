# AGENTS.md — Ariadne 项目结构与协作约定（codex 实施者读）

> 本文件是 codex 实施前的**强制前置**。动手写代码前先读完，并确认改动落在正确的边界内。
> 违反边界的实现会被打回。三方协作：方向规划(人) / 架构决策(Claude Code) / 实施(codex，即你)。

## 0. 铁律速览（先记这几条）

1. **前后端分离，物理隔离**：Python 后端和 Next.js 前端是**两个独立子项目**，各自的依赖、构建、目录互不侵入。
2. **禁止后端里塞前端**：Python 包 `src/ariadne/` 内**不得**再出现 `.html`/`.tsx`/`.css`/前端资源。现存的 `src/ariadne/dashboard.html` 是历史遗留，待迁出，**不许再往里加同类文件**。
3. **前端只通过 HTTP API 与后端通信**：不共享代码、不读后端文件、不 import Python。唯一契约是 REST + SSE。
4. **实现前先做结构设计**：动手前，在对应 task 文档或 PR 描述里先写「我要新增/改动哪些文件、落在哪个边界、为什么」，确认无误再写代码。
5. **每个 task 一个独立分支 + 一个聚焦 commit 序列**；做完贴 `pytest`/构建输出。

## 1. 目标架构（终态）

```
ariadne-0630/
├── backend/                 # 【Python】agent 编排侧(控制平面 + 执行 + API)
│   └── src/ariadne/         #   注:当前仍在仓库根 src/ariadne/,迁移见 §4
├── frontend/                # 【Next.js】Web 前端(deep-010 起)
│   ├── app/                 #   App Router 页面
│   ├── components/
│   └── package.json         #   独立依赖,uv 不管它
├── docs/                    # 设计文档 / ADR / task 计划
├── AGENTS.md                # 本文件(codex 读)
├── CLAUDE.md                # 架构决策(Claude Code 读)
└── pyproject.toml           # 仅后端
```

**边界原则**：
- `backend/` 只懂业务和数据，**不知道前端存在**。它暴露 HTTP API，仅此。
- `frontend/` 只懂 UI 和调 API，**不知道 SQLite/store/daemon 存在**。它拿到的只有 JSON。
- 两者之间唯一的契约是 **API 形状**（deep-009 定义）。契约变更必须先改 API 文档，再改两端。

## 1.5 架构方向：朝 DDD 演进，克制落地

本项目**目标是 DDD 架构**（现阶段是轻量 DDD，逐步演进）。用 DDD 的积木，但每个积木要挣得存在的理由：

- **要用的积木**：Repository（持久化）、Domain Service（领域服务，跨实体业务规则）、充血实体（只属于某实体的规则搬进它）、聚合根（有一致性边界时）、限界上下文（模块自然浮现时）。
- **不要的仪式**：无差别值对象、领域事件总线、CQRS、防腐层——这些在有真实需求前**不加**。加任何构件前先答「它现在解决什么真问题」，答不上就不加。
- **缰绳**：代码要**优雅、简单易懂**，读起来像业务叙述，不像框架样板。DDD 的价值在清晰边界，不在术语堆砌。
- 遇到「该不该引入某个 DDD 构件」的判断，**问 Claude Code，不自行决定**。

## 2. 后端分层（Python，src/ariadne/）

现有分层，重构(deep-007)后会更清晰。写代码时对号入座：

| 层 | 文件 | 职责 | 禁止 |
|----|------|------|------|
| CLI | `cli.py` | 参数解析 + 调用,零业务逻辑 | 不写业务/SQL |
| API | `api.py` | HTTP 路由 + 序列化,零业务逻辑 | 不写业务/SQL |
| 编排 | `orchestrator.py`/`briefing.py`/`llm_decide.py` | squad 委派 + 决策 | 不碰持久化细节 |
| 服务 | `service/`(deep-007) | 状态机业务规则、跨 repo 事务 | 不写裸 SQL(用 repo) |
| 执行 | `backends.py`/`runner.py` | 调 agent CLI、隔离、diff | 不碰 store |
| 持久化 | `store/`(deep-007) | 纯 SQL + row↔model | 无业务判断 |
| 模型 | `models.py` | 实体/值对象。可含只属于自己的行为(充血) | 不塞跨实体规则(那是领域服务)；不为仪式加值对象 |

**CLI 和 API 共用业务逻辑**：`ariadne run`(CLI)和 `POST /api/issues`(API)必须调用**同一段** runner/service 逻辑，不各写一份。

## 3. 前端边界（Next.js，frontend/）

- 从 **deep-010** 开始存在。deep-009 之前不要建。
- 技术栈：Next.js(App Router) + TypeScript + Tailwind。**不引入 Zustand/TanStack Query**，除非有明确必要并说明理由（3 页面用 `fetch`+`EventSource` 足够）。
- 只调 `backend/` 的 HTTP API。**严禁**读后端文件、共享类型定义靠手动对齐（或从 API 响应推导），不做 Python↔TS 代码生成（超范围）。
- 前端依赖装在 `frontend/`，`npm`/`pnpm` 管理，**绝不进 pyproject/uv**。

## 4. 迁移策略（避免和进行中的 task 冲突）

- **现在不搬 `src/` → `backend/src/`**：会和 deep-007/008/009 大面积冲突。
- 现阶段：Python 代码保持在仓库根 `src/ariadne/`，前端进 `frontend/`。二者已经物理分离，够用。
- `src/ariadne/dashboard.html`：deep-010 前端上线后**删除**，功能由 `frontend/` 取代。在那之前不扩展它。
- 待所有后端 task(007/008/009)落定，再单独做一个「`src/`→`backend/`」的机械迁移 task（那时冲突面最小）。**本轮不做这个搬迁。**

## 5. 协作分工与工作流

- **人**：方向与优先级。
- **Claude Code**：架构决策、边界裁定、写 task 计划和本约定。有架构/DDD 构件疑问找它，不自己拍板改边界。
- **codex（你）**：按 task 文档实施，**主要通过 Ask Matt 工作流（Matt skill）完成**。**遇到需要改动模块边界、跨层、引入新 DDD 构件、或本约定没覆盖的情况，先停下问，不自行扩大范围。**

**工作流约定**：
- 每个 task 用 Matt skill 实施，改好后**收敛成一个干净 commit**（一个 task 一个 commit，不散落多个 WIP commit）。
- commit message 说清「做了什么 + 落在哪层」，不夹带 AI 署名。
- 动手前先写结构设计（§0.4），确认后再实施。

## 6. 通用铁律

- 不改运行时行为的重构，测试数量和内容不变还全绿；改了就是行为变更，停下说明。
- 新功能带新测试。`ruff` 零告警。
- 不引入 multica 的 SaaS 架构（Redis relay / event bus / 多节点 / 多租户 / 认证）——local-first 单用户。
- 不编造性能数字：真实数据需真实环境跑出，否则标「模拟」。
- 不碰历史记录（ADR、已完成 task 文档、调研报告）。
- 回报列清单（改了哪些文件、落在哪层、测试结果），不只说「完成」。
