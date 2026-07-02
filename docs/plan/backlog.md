# Backlog (Non-blocking findings, deferred items)

## Completed foundations

| Item | Status |
|------|--------|
| Backend registry seam | `register_backend()`, `get_backend()`, and `available_backends()` support in-process extension without editing the built-in registry literal. |
| Truthful single-vs-squad benchmark comparison | `ariadne benchmark-compare` reports `simulated: true` for dry-run. Real backend mode runs the requested backend against a temporary workspace and reports provider availability or execution failure as first-class results. |
| Backend session resume | Daemon passes the latest same-trace or parent TaskRun `session_id` into `ExecutionContext.resume_session_id`. |
| MCP config injection | Agent-profile `runtime_policy["mcp_config_path"]` or `ARIADNE_MCP_CONFIG` flows into backend command rendering. |
| First-class Skill materialization | Skills persist routing description, prompt content, allowed tools, verification command, source path, and version; TaskRun handoffs include a capability package. |
| Skill verification evidence loop | Skill verification commands run after successful backend execution and are recorded as result metadata, activity log, and IssueTimeline evidence without hard-failing the TaskRun. |
| SQLite WAL mode | File-backed stores enable WAL journal mode for better daemon/API read-write concurrency. |

## Deferred by design

| Item | Reason | Revisit when |
|------|--------|-------------|
| LangGraph supervisor graph in orchestrator | Start with plain if/else; add LangGraph only if it adds value in W2 | W2 spike |
| WebSocket real-time progress | CLI output sufficient for demo | Post-秋招 |
| Frontend / web UI | Out of scope for this project | Never (by design) |
| Knowledge pipeline / RAG | Frozen — Ariadne already has this, not the point here | Never (by design) |
| Multi-worker daemon | Single-worker sufficient for local demo | Post-秋招 |
| Memory / backlog planning | Out of scope | Never (by design) |
| Autopilot / scheduled tasks | Out of scope | Never (by design) |
| Per-agent max_concurrent_tasks | Single worker, no concurrency needed | Post-秋招 |
| Third-party backend entry-point discovery | Built-in backend registry and benchmark smoke are enough until real external backend authors exist | 拉 star 阶段 |
| Human trace replay time study | Current trace benchmark measures artifact coverage; human diagnosis timing needs controlled evaluator study | Mature evaluation phase |

## Star-stage productization

| Item | Reason | Revisit when |
|------|--------|-------------|
| LICENSE and CONTRIBUTING | Needed for external contributors, not for the local runtime foundation. | Public contributor push |
| `uvx` / `pipx` one-command install | Real distribution work should follow a stable CLI surface. | Public quickstart hardening |
| Provider onboarding docs | Useful once real backend runs are exercised by users beyond this repo. | Public quickstart hardening |
| Third-party backend entry-point discovery | Requires actual external backend package authors; the in-process registry is enough now. | External backend request |
| More built-in providers | Add Gemini/Cursor/Copilot only after Codex/Claude/dry-run evidence is stable. | Provider demand exists |

## Non-blocking findings from verify

来自 deep-011 A-E review（2026-07-02，3 个 subagent 交叉核 diff）的三条 LOW，
不阻塞，记录待后续处理：

| # | Finding | 严重度 | 处理时机 |
|---|---------|--------|---------|
| L1 | `issue` 表迁移 `_migrate_issue_table_if_needed`（`store/base.py`）在 `DROP TABLE`/`RENAME` 之间无事务包裹，中途崩溃会丢表。仅 schema 升级那一次触发，本地 sqlite。`task` 表迁移同模式已有先例。 | 低 | 迁移逻辑重构时统一加事务 |
| L2 | `api.py` `timeout_seconds = req.timeout_seconds or 300` 的 falsy 写法：当前 pydantic `Field(None, ge=1)` 拒 0，实际只在 None 兜底，安全；但若将来放宽校验会静默把 0 变 300。 | 低 | 改 `if x is None` 更稳，顺手即可 |
| L3 | cancel 的 lease 语义从 `revoked`（带 `revoke_reason`）改为 `released`，timeline 事件 `lease_revoked`→`lease_released`。下游若有 telemetry/analytics 按 `revoked` 统计，会漏掉用户取消。 | 低 | 有 telemetry 消费方时对齐 |

> HIGH（cancel 绕过状态机）已由 deep-011-G 修复（commit b585d4e）。
> 两条 MEDIUM（cancel 跨进程投递失灵、daemon backend 静默降级 dry-run）已并入
> deep-011-F 统一解决（同属"API/daemon 两进程"根问题）。
