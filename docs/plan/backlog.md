# Backlog (Non-blocking findings, deferred items)

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

## Non-blocking findings from verify

(Will be populated as tasks are verified.)
