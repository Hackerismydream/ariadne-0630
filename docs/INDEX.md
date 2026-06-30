# Documentation Index

> Entry point for all design docs. Update this file whenever a new scope or doc is added.

## Project

Ariadne: local multi-agent orchestration platform. Turns coding agent harnesses
(Codex, Claude Code) into pluggable executors orchestrated by a Squad mechanism
(leader delegates, members execute, event loop re-evaluates).

Reference: [multica](https://github.com/multica-ai/multica) (38k★, Go + Next.js).
This project extracts multica's 3 core orchestration mechanisms and re-implements
them in Python. Not a translation — an architecture extraction.

## Design Docs

| Scope | Doc | Purpose |
|-------|-----|---------|
| architecture | [README.md](architecture/README.md) | System architecture, layer boundaries, module index |
| architecture | [task-state-machine.md](architecture/task-state-machine.md) | Task lifecycle, status transitions, failure classification |
| architecture | [squad-orchestration.md](architecture/squad-orchestration.md) | Leader briefing, delegation, event loop |
| architecture | [harness-backend.md](architecture/harness-backend.md) | ExecutionBackend protocol, Codex/Claude integration |
| architecture | [trace-observability.md](architecture/trace-observability.md) | Trace ID, activity log, timeline |
| architecture | [deep-execution.md](architecture/deep-execution.md) | Claude JSON parse, worktree isolation, streaming |
| architecture | [real-squad.md](architecture/real-squad.md) | Real squad loop with LLM + Codex/Claude |
| architecture | [dashboard-layout.md](architecture/dashboard-layout.md) | Web dashboard layout |
| architecture | [multica-mapping.md](architecture/multica-mapping.md) | Each mechanism → multica source file it derives from |

## Delivery Plan

| Doc | Purpose |
|-----|---------|
| [plan/README.md](plan/README.md) | Delivery workflow, task status, execution mode |
| [plan/analysis/](plan/analysis/) | Module decomposition, integration enumeration |
| [plan/tasks/](plan/tasks/) | Task files (one per deliverable) |
| [plan/backlog.md](plan/backlog.md) | Non-blocking findings, deferred items |

## Constraints (frozen)

1. **Local-first**: Python, SQLite, single-user. No Postgres, no auth, no WebSocket server.
2. **< 3000 lines** of product code. If adding a feature pushes past 3000, cut scope.
3. **3 multica mechanisms only**: task state machine, squad briefing, daemon claim loop.
4. **Knowledge pipeline frozen**: no RAG, no source-to-issue compilation. Manual issue creation.
5. **CLI-first**: `ari`-style CLI is the primary interface. FastAPI is optional thin layer.
6. **Tests first**: every module's state machine and contract has tests before implementation.

## Non-goals (explicit)

- No frontend / web UI
- No Feishu / GitHub / Inbox / Supervisor / Memory / Backlog
- No multi-workspace, no auth, no Postgres
- No knowledge orchestration / RAG / vector DB
- No autopilot / scheduled tasks / chat sessions
