# Backlog (Non-blocking findings, deferred items)

## Completed foundations

| Item | Status |
|------|--------|
| Backend registry seam | `register_backend()`, `get_backend()`, and `available_backends()` support in-process extension without editing the built-in registry literal. |
| Truthful single-vs-squad benchmark comparison | `ariadne benchmark-compare` reports `simulated: true` for dry-run and `blocked` for real backends until external execution is explicitly enabled. |
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

(Will be populated as tasks are verified.)
