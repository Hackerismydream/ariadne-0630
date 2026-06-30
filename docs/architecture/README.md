# Architecture

## Purpose

Local multi-agent orchestration platform. Manages the full lifecycle of
coding-agent tasks: from issue assignment, through Squad leader delegation,
to harness execution (Codex/Claude Code), with retry, failure classification,
and progress tracking.

## Boundary

```
In scope                                  Out of scope
─────────                                 ───────────
issue/task/squad CRUD                     knowledge compilation / RAG
task state machine (queued→completed)     frontend / web UI
squad leader briefing + delegation        multi-workspace / auth
daemon poll-claim-execute loop            Postgres / hosted DB
Codex/Claude backend integration          autopilot / chat / scheduled tasks
retry + failure classification            Feishu / GitHub / Inbox
structured logging + trace ID             vector DB / embedding search
LLM-as-judge evaluation                   memory / backlog planning
```

## Layer Architecture

```
┌──────────────────────────────────────────────────────────┐
│  CLI / API Layer                                          │
│  cli.py (typer)          api.py (FastAPI, optional)       │
│  Issue CRUD, daemon control, board export                 │
│  Zero business logic — only serialization and routing     │
├──────────────────────────────────────────────────────────┤
│  Orchestration Layer                                      │
│  orchestrator.py    Squad leader delegation + event loop  │
│  briefing.py        Generate leader briefing (protocol +  │
│                     roster + instructions)                │
│  No direct DB writes — calls store layer                  │
├──────────────────────────────────────────────────────────┤
│  Service Layer                                            │
│  store.py           SQLite persistence + state transitions│
│  models.py          Pydantic models (Task, Squad, Agent…) │
│  daemon.py          Poll loop, heartbeat, claim, cancel   │
├──────────────────────────────────────────────────────────┤
│  Execution Layer                                          │
│  backends.py        ExecutionBackend protocol             │
│                     CodexBackend, ClaudeBackend           │
│                     Command template, safety gate, diff   │
├──────────────────────────────────────────────────────────┤
│  Storage                                                   │
│  SQLite (multica_py.db)                                   │
│  Tables: issue, task, squad, squad_member, agent,         │
│          activity_log, run_message                        │
└──────────────────────────────────────────────────────────┘
```

## Layer Rules (hard, enforced by tests)

| Rule | Enforcement |
|------|-------------|
| CLI/API layer has zero business logic | No `import` of orchestrator/store internals beyond public methods |
| Orchestration layer never writes DB directly | All writes go through `store.py` public methods |
| Execution layer never knows about squads | Backend receives `ExecutionContext`, returns `ExecutionResult` |
| models.py has zero dependencies on other layers | Pure Pydantic, no imports from store/orchestrator/backends |
| Each layer testable in isolation | Mock only the layer below, never the layer above |

## Module Index

| Module | Doc | Lines (target) |
|--------|-----|----------------|
| models.py | [task-state-machine.md](task-state-machine.md) | ~250 |
| store.py | [task-state-machine.md](task-state-machine.md) | ~300 |
| daemon.py | [task-state-machine.md](task-state-machine.md) | ~350 |
| briefing.py | [squad-orchestration.md](squad-orchestration.md) | ~150 |
| orchestrator.py | [squad-orchestration.md](squad-orchestration.md) | ~400 |
| backends.py | [harness-backend.md](harness-backend.md) | ~400 |
| api.py | (inline in README) | ~250 |
| cli.py | (inline in README) | ~200 |

## Design Decisions

### D1: SQLite over JSON/JSONL

Ariadne used JSON/JSONL files. Multica uses Postgres. We use SQLite —
durable state transitions need transactional integrity (claim must be atomic),
but we don't need a server. SQLite gives us `BEGIN IMMEDIATE` for atomic claims.

### D2: LangGraph for orchestrator internal flow, not for the control plane

The task queue and daemon are plain Python + SQLite. LangGraph is used only
inside `orchestrator.py` to model the leader's delegation decision as a
supervisor graph (leader node → handoff → member node). The control plane
(state machine, claim loop) is framework-agnostic.

### D3: Structured delegation, not @mention

Multica's leader delegates by posting `[@Name](mention://agent/<UUID>)` markdown.
We use a `DelegationDecision` Pydantic model — serializable, testable,
replayable. This is a deliberate deviation, documented in multica-mapping.md.

### D4: No daemon-as-process-manager

`daemon start` runs a polling loop in the foreground (or `--background` via
simple fork). No systemd, no supervisor, no process manager. Ctrl-C stops
cleanly, in-flight tasks are marked `failed` with `failure_reason=runtime_recovery`.

## Relationship to multica

See [multica-mapping.md](multica-mapping.md) for the mechanism-by-mechanism
mapping. Summary: we extract task state machine (migration 001/055), squad
briefing (squad_briefing.go), and daemon claim loop (daemon.go), re-implemented
in Python with SQLite + LangGraph.
