# Architecture

## Purpose

Local managed-agent team runtime. It manages the lifecycle of coding-agent
work from Issue and TaskRun facts, through Squad leader delegation, to isolated
Codex/Claude Code execution, verification evidence, retry, failure
classification, and trace replay.

## Boundary

```
In scope                                  Out of scope
─────────                                 ───────────
issue/taskrun/squad/profile/skill CRUD    knowledge compilation / RAG
queued→claimed→running→terminal machine   hosted multi-tenant service
RuntimeMachine/Capability/Lease facts     auth / billing / tenants
squad leader briefing + delegation        Postgres / network scheduler
daemon poll-claim-execute loop            autopilot / scheduled inbox work
Codex/Claude/dry-run backend adapters     Feishu / GitHub product sync
worktree-isolated real execution          vector DB / embedding search
BenchmarkRun from product facts           public plugin marketplace
FastAPI dashboard for inspection          production frontend app
```

## Layer Architecture

```
┌──────────────────────────────────────────────────────────┐
│  CLI / API Layer                                          │
│  cli.py (Typer)          api.py (FastAPI dashboard API)   │
│  Issue/TaskRun/runtime/profile/skill/squad/benchmark I/O  │
│  Zero business logic — only serialization and routing     │
├──────────────────────────────────────────────────────────┤
│  Orchestration Layer                                      │
│  orchestrator.py    Squad leader delegation + event loop  │
│  briefing.py        Generate leader briefing (protocol +  │
│                     roster + instructions)                │
│  llm_decide.py      Structured DelegationDecision parsing │
├──────────────────────────────────────────────────────────┤
│  Runtime Control Plane                                    │
│  store.py           SQLite/WAL product facts + state      │
│                     transitions                           │
│  daemon.py          Runtime registration, leases, claim,  │
│                     heartbeat, retry, verification        │
│  policy.py          Runtime/lease/profile/skill gates     │
├──────────────────────────────────────────────────────────┤
│  Execution Layer                                          │
│  backends.py        ExecutionBackend protocol             │
│                     Backend registry + Codex/Claude/DryRun│
│                     Resume/MCP, worktree isolation, diff  │
├──────────────────────────────────────────────────────────┤
│  Storage                                                   │
│  SQLite (ariadne.db, WAL for file-backed stores)          │
│  Tables: issue, task/taskrun, runtime_machine, capability,│
│          lease, agent_profile, skill, leader_decision,    │
│          issue_timeline_event, benchmark_run, activity_log│
└──────────────────────────────────────────────────────────┘
```

## Current Execution Flow

```
Issue
  │
  ├─ assigned to AgentProfile ───────────────┐
  │                                          │
  └─ assigned to Squad ──► Leader TaskRun ──►│
                         │                  │
                         └─ LeaderDecision ─┘
                                      │
                                      ▼
Queued TaskRun
  │
  ├─ RuntimeMachine atomically claims and creates RuntimeLease
  │
  ├─ ExecutionPolicy validates lease, runtime, capability, profile, skills
  │
  ├─ Daemon builds ExecutionContext
  │     ├─ bound Skills materialized into the handoff
  │     ├─ resume_session_id from parent/same trace result
  │     └─ mcp_config_path from AgentProfile policy, then environment fallback
  │
  ├─ Backend registry selects DryRun, Codex, Claude Code, or in-process extension
  │
  ├─ Real backend executes
  │     ├─ default: detached git worktree
  │     ├─ explicit escape hatch: --write-workspace
  │     ├─ stdout progress streamed to IssueTimeline
  │     ├─ diff and changed_files captured before cleanup
  │     └─ worktree_audit recorded in result metadata
  │
  ├─ Skill verification commands run as evidence, not hard gates
  │
  └─ TaskRun becomes completed/failed/cancelled, then leader may re-evaluate
```

## Layer Rules

| Rule | Enforcement |
|------|-------------|
| CLI/API layer has no control-plane state machine logic | Commands call public store/daemon/orchestrator APIs |
| Orchestration decisions are structured | Leader outputs `LeaderDecision` / `DelegationDecision`, persisted as facts |
| Execution layer never knows about squads | Backend receives `ExecutionContext`, returns `ExecutionResult` |
| models.py has zero dependencies on other layers | Pure Pydantic, no imports from store/orchestrator/backends |
| Real execution is isolation-first | Backend worktree tests cover default isolation and write-workspace escape |
| Benchmark metrics come from product facts | BenchmarkRun runners export SQLite-derived facts and summaries |

## Module Index

| Module | Responsibility | Related doc |
|--------|----------------|-------------|
| `models.py` | Pydantic domain objects and enums | [task-state-machine.md](task-state-machine.md) |
| `store.py` | SQLite schema, WAL setup, state transitions, product facts | [task-state-machine.md](task-state-machine.md) |
| `daemon.py` | Runtime registration, heartbeat, claim, execution, retry | [task-state-machine.md](task-state-machine.md) |
| `policy.py` | Layered real-execution policy gate | [harness-backend.md](harness-backend.md) |
| `briefing.py` | Squad briefing from roster and issue facts | [squad-orchestration.md](squad-orchestration.md) |
| `orchestrator.py` | Leader action/no_action/failed/done loop | [squad-orchestration.md](squad-orchestration.md) |
| `llm_decide.py` | JSON parsing and fallback for leader decisions | [squad-orchestration.md](squad-orchestration.md) |
| `backends.py` | Backend registry, command rendering, worktree execution | [harness-backend.md](harness-backend.md) |
| `eval.py` | BenchmarkRun from product facts and comparison helper | [trace-observability.md](trace-observability.md) |
| `benchmarking.py` | Artifact-backed benchmark runners and aggregate report | [trace-observability.md](trace-observability.md) |
| `api.py` | FastAPI inspection/dashboard endpoints | [dashboard-layout.md](dashboard-layout.md) |
| `cli.py` | Typer command surface | README |

## Design Decisions

Architecture decisions are recorded under `docs/adr/`.

| ADR | Decision |
|-----|----------|
| [0010](../adr/0010-open-execution-backend-registry.md) | Open in-process backend registry, no third-party entry points yet |
| [0011](../adr/0011-provider-session-resume-and-mcp-config.md) | Provider session resume and MCP config precedence |
| [0012](../adr/0012-skills-as-capability-packages.md) | Skills as materialized capability packages |
| [0013](../adr/0013-isolation-first-real-backend-execution.md) | Isolation-first real backend execution |

## Provenance

The runtime borrows three proven mechanisms from multica: task lifecycle,
squad briefing, and daemon-style claim/execution. Ariadne re-implements those
mechanisms as a local Python/SQLite control plane rather than a hosted
Go/Postgres service. See [multica-mapping.md](multica-mapping.md) for the
mechanism-by-mechanism mapping.
