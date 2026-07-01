# Ariadne

Local managed-agent team runtime for coding work. Ariadne turns issues into
durable TaskRuns, lets a Squad leader delegate to member agents, runs Codex or
Claude Code through a pluggable execution backend, and records the resulting
timeline, diff, tests, retry, and benchmark evidence in SQLite.

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
│  Runtime Control Plane                                    │
│  store.py           SQLite/WAL product facts + transitions│
│  daemon.py          RuntimeMachine, leases, claim, retry  │
│  policy.py          Runtime/profile/skill execution gate  │
│  eval.py            BenchmarkRun from product facts       │
├──────────────────────────────────────────────────────────┤
│  Execution Layer                                          │
│  backends.py        ExecutionBackend protocol             │
│                     CodexBackend, ClaudeBackend, DryRun   │
│                     Registry, resume, MCP, worktree       │
│                     isolation, diff, timeout              │
├──────────────────────────────────────────────────────────┤
│  SQLite (ariadne.db)                                      │
│  Tables: issue, taskrun/task, runtime, lease, profile,    │
│          skill, leader_decision, benchmark_run            │
└──────────────────────────────────────────────────────────┘
```

## Implemented Capabilities

- **SQLite control plane**: issue, TaskRun, RuntimeMachine, RuntimeCapability,
  RuntimeLease, AgentProfile, Skill, LeaderDecision, IssueTimeline, and
  BenchmarkRun are persisted as product facts. File-backed stores enable WAL.
- **Atomic runtime claim loop**: the daemon registers local capabilities,
  claims queued TaskRuns, heartbeats leases, executes work, records progress,
  classifies failures, and retries when policy allows.
- **Squad orchestration**: a leader receives a structured briefing and emits
  `action`, `no_action`, `failed`, or `done`; member terminal states re-trigger
  leader evaluation.
- **Pluggable execution backends**: built-in dry-run, Codex, and Claude Code
  backends share the same in-process registry used by tests and benchmark
  providers.
- **Isolation-first real execution**: real providers run in detached git
  worktrees by default. Direct target writes require `--write-workspace`, and
  every result records `worktree_audit` metadata.
- **Session, MCP, and skill handoff inputs**: provider session resume,
  AgentProfile-level MCP config, materialized Skill capability packages, and
  skill verification evidence are first-class runtime inputs/outputs.
- **Artifact-backed benchmarks**: benchmark runners export manifests, metrics,
  SQLite facts, summaries, and hashes so reported numbers can be replayed.

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
# Dry-run output is labelled simulated; live provider numbers require live runs.
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

## Provenance

| Mechanism | Multica source | What we keep | What we change |
|-----------|---------------|--------------|----------------|
| **Task state machine** | migration 001 + 055 | 6 states, failure classification, retry chain | SQLite + `BEGIN IMMEDIATE` instead of Postgres |
| **Squad briefing** | `squad_briefing.go` | 3-section briefing (protocol + roster + instructions) | Structured `DelegationDecision` instead of `@mention` markdown |
| **Daemon claim loop** | `daemon.go` | Poll-claim-execute, heartbeat, stale recovery | Synchronous loop, SQLite-based, no server |

See [docs/architecture/multica-mapping.md](docs/architecture/multica-mapping.md) for the mechanism-by-mechanism provenance.

## Design Decisions

1. **SQLite over JSON/JSONL** — durable state transitions need transactional integrity (atomic claim), but we don't need a server.
2. **Structured delegation, not free-form mention text** — `DelegationDecision` is testable, replayable, and validates against the roster.
3. **LLM injected, not hardcoded** — orchestrator receives a `llm_decide` callable. Tests use `deterministic_decide` without LLM calls.
4. **Execution isolation is non-negotiable** — real CLI execution defaults to a detached git worktree; direct target writes require `--write-workspace`.
5. **Skills are capability packages** — bound skills materialize prompt content, allowed tools, and verification commands into TaskRun handoffs.
6. **Verification evidence is not a hard gate** — skill verification failures are recorded for leader re-evaluation instead of discarding the TaskRun result.

Detailed decision records live under [docs/adr](docs/adr/):

- [ADR 0010: Open Execution Backend Registry](docs/adr/0010-open-execution-backend-registry.md)
- [ADR 0011: Provider Session Resume and MCP Config Injection](docs/adr/0011-provider-session-resume-and-mcp-config.md)
- [ADR 0012: Skills as Capability Packages](docs/adr/0012-skills-as-capability-packages.md)
- [ADR 0013: Isolation-First Real Backend Execution](docs/adr/0013-isolation-first-real-backend-execution.md)

## Testing

```bash
uv run ruff check src/ariadne/
uv run pytest tests/ -v
```

The suite covers state transitions, atomic claim, TaskRun compatibility,
runtime registration, leases, IssueTimeline, AgentProfiles, Skills,
LeaderDecisions, ExecutionPolicy, BenchmarkRuns, squad orchestration, backend
isolation gates, backend registry extension, session resume, MCP config injection,
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
not a substitute for CI. Live Codex/Claude patch success requires the provider
CLI and is not mixed with dry-run or synthetic smoke results. Real backend
patch smoke runs default to isolated git worktrees; direct target writes require
`--write-workspace`.

Current comparison output using `--backend dry-run` is simulated and should be
reported as simulated. Real Codex/Claude comparison numbers remain unfilled
until a real environment with the provider CLI and credentials runs
`ariadne benchmark-compare --backend codex` or
`ariadne benchmark-compare --backend claude-code`.

## Project Structure

```
src/ariadne/
├── models.py          # 4 enums + 11 Pydantic models
├── store.py           # SQLite persistence + state machine
├── daemon.py          # Poll-claim-execute loop + heartbeat
├── briefing.py        # Squad leader briefing generation
├── orchestrator.py    # Leader delegation + event loop
├── llm_decide.py      # LLM-backed delegation (OpenAI-compatible)
├── backends.py        # Codex/Claude/DryRun + registry + isolation + diff
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
