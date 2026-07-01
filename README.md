# Ariadne

Local multi-agent orchestration platform — turns coding agent harnesses (Codex, Claude Code) into pluggable executors orchestrated by a Squad mechanism (leader delegates, members execute, event loop re-evaluates).

Reference: [multica](https://github.com/multica-ai/multica) (38k★, Go + Next.js). This project extracts multica's 3 core orchestration mechanisms and re-implements them in Python. Not a translation — an architecture extraction.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  CLI Layer (typer)                                        │
│  issue/taskrun/runtime/profile/skill/squad/benchmark       │
│  Zero business logic — only serialization and routing     │
├──────────────────────────────────────────────────────────┤
│  Orchestration Layer                                      │
│  orchestrator.py    Squad leader delegation + event loop  │
│  briefing.py        Generate leader briefing (protocol +  │
│                     roster + instructions)                │
│  llm_decide.py      LLM-backed delegation decision        │
├──────────────────────────────────────────────────────────┤
│  Service Layer                                            │
│  store.py           SQLite product facts + transitions    │
│  daemon.py          RuntimeMachine, leases, execution gate │
│  eval.py            BenchmarkRun from product facts       │
├──────────────────────────────────────────────────────────┤
│  Execution Layer                                          │
│  backends.py        ExecutionBackend protocol             │
│                     CodexBackend, ClaudeBackend, DryRun   │
│                     Registry, resume, MCP, safety gate,   │
│                     worktree diff, timeout                │
├──────────────────────────────────────────────────────────┤
│  SQLite (ariadne.db)                                      │
│  Tables: issue, taskrun/task, runtime, lease, profile,    │
│          skill, leader_decision, benchmark_run            │
└──────────────────────────────────────────────────────────┘
```

## Quickstart

```bash
# Install
uv sync --extra dev

# Create an agent
ariadne agent-create --name Coder --backend dry-run

# Create an issue
ariadne issue-create --title "Implement feature X" --assignee-id <agent-id>

# Start daemon (polls for tasks, executes them)
ariadne daemon-start --max-iterations 3 --poll-interval 1

# Check status
ariadne daemon-status
ariadne issue-list

# Run benchmark
ariadne benchmark-run --iterations 5 --backend dry-run

# Compare serial vs bounded squad execution.
# Dry-run output is labelled simulated; real backends require the external gate.
ariadne benchmark-compare --tasks 5 --backend dry-run --max-concurrent 2
```

## Five-Minute v1 Demo

From a clean checkout:

```bash
uv sync --extra dev
uv run ariadne demo-v1 --reset
```

The demo creates a local dry-run squad, executes the daemon, and records
RuntimeMachines, RuntimeCapabilities, TaskRuns, RuntimeLeases, IssueTimeline
events, LeaderDecisions, and a BenchmarkRun. It does not require Codex, Claude,
or provider credentials.

Inspect the generated DB:

```bash
export ARIADNE_DB=.ariadne-demo-v1/ariadne-v1.db
uv run ariadne runtime-list
uv run ariadne capability-list
uv run ariadne taskrun-list
uv run ariadne runtime-lease-list
uv run ariadne leader-decision-list
uv run ariadne benchmark-list
uv run ariadne api-serve
```

See [docs/demo-v1.md](docs/demo-v1.md) for the full verification path.

## Multica Mapping

| Mechanism | Multica source | What we keep | What we change |
|-----------|---------------|--------------|----------------|
| **Task state machine** | migration 001 + 055 | 6 states, failure classification, retry chain | SQLite + `BEGIN IMMEDIATE` instead of Postgres |
| **Squad briefing** | `squad_briefing.go` | 3-section briefing (protocol + roster + instructions) | Structured `DelegationDecision` instead of `@mention` markdown |
| **Daemon claim loop** | `daemon.go` | Poll-claim-execute, heartbeat, stale recovery | Synchronous loop, SQLite-based, no server |

See [docs/architecture/multica-mapping.md](docs/architecture/multica-mapping.md) for full details.

## Design Decisions

1. **SQLite over JSON/JSONL** — durable state transitions need transactional integrity (atomic claim), but we don't need a server.
2. **Structured delegation, not @mention** — `DelegationDecision` Pydantic model is testable, replayable, validates against roster. Multica's `@mention` markdown is not.
3. **LLM injected, not hardcoded** — orchestrator receives a `llm_decide` callable. Tests use `deterministic_decide` without LLM calls.
4. **Safety gate is non-negotiable** — dual confirmation (`ARIADNE_ENABLE_EXTERNAL_EXECUTION=1` + `confirm_execution=True`) before any real CLI execution.
5. **LangGraph skipped for v1** — single LLM call per leader activation is simpler than a supervisor graph. Documented in backlog.

## Testing

```bash
uv run ruff check src/ariadne/
uv run pytest tests/ -v
```

The suite covers state transitions, atomic claim, TaskRun compatibility,
runtime registration, leases, IssueTimeline, AgentProfiles, Skills,
LeaderDecisions, ExecutionPolicy, BenchmarkRuns, squad orchestration, backend
safety gates, backend registry extension, session resume, MCP config injection,
skill verification evidence, and the clean-checkout demo.

## Benchmark Evidence

Ariadne benchmark numbers are generated from artifact-backed runners rather
than terminal-only output. Each runner writes a run directory with metadata,
case manifest, metrics, exported SQLite product facts, summaries, and artifact
hashes.

```bash
uv run python benchmarks/runners/artifact_spine.py --artifact-dir artifacts/benchmarks/artifact
uv run python benchmarks/runners/control_plane_concurrency.py --tasks 500 --workers 16 --artifact-dir artifacts/benchmarks/control
uv run python benchmarks/runners/state_machine_recovery.py --artifact-dir artifacts/benchmarks/state
uv run python benchmarks/runners/squad_routing.py --artifact-dir artifacts/benchmarks/squad
uv run python benchmarks/runners/trace_replay.py --artifact-dir artifacts/benchmarks/trace
uv run python benchmarks/runners/real_backend_patch.py --provider synthetic --artifact-dir artifacts/benchmarks/real
uv run python benchmarks/runners/aggregate.py --artifact-dir artifacts
```

Dry-run, synthetic real-backend smoke, live Codex/Claude backend runs, LLM
routing, local pytest/ruff, and GitHub CI are reported as separate accounts.
CI pass rate is only reportable from GitHub reported checks; local pytest is
not a substitute for CI. Live Codex/Claude patch success requires explicit
external execution and is not mixed with dry-run or synthetic smoke results.
`ariadne benchmark-compare` follows the same rule: dry-run comparisons report
`simulated: true`; real backend comparisons report `blocked` until
`ARIADNE_ENABLE_EXTERNAL_EXECUTION=1` is set and the provider CLI is available.

## Project Structure

```
src/ariadne/
├── models.py          # 4 enums + 11 Pydantic models
├── store.py           # SQLite persistence + state machine
├── daemon.py          # Poll-claim-execute loop + heartbeat
├── briefing.py        # Squad leader briefing generation
├── orchestrator.py    # Leader delegation + event loop
├── llm_decide.py      # LLM-backed delegation (OpenAI-compatible)
├── backends.py        # Codex/Claude/DryRun + safety gate + diff
├── policy.py          # Layered ExecutionPolicy gate
├── eval.py            # BenchmarkRun from product facts + evaluation
├── benchmarking.py    # Artifact-backed benchmark runners + aggregate reports
└── cli.py             # Typer CLI entry point
benchmarks/runners/    # External benchmark runner entry points
tests/                 # Test suite
docs/                  # Architecture docs + delivery plan
```

## Non-goals

- No production frontend beyond the local inspection dashboard
- No knowledge pipeline / RAG / vector DB
- No multi-workspace / auth / Postgres
- No autopilot / scheduled tasks / chat sessions
- No Feishu / GitHub / Inbox / Supervisor / Memory / Backlog
