# Ariadne

Local managed-agent team runtime for coding work. Ariadne turns an issue into
durable TaskRuns, lets a Squad leader delegate work to member agents, runs
Codex or Claude Code through a pluggable backend, and records timeline, diff,
test, retry, and benchmark evidence in SQLite.

The product is local-first: one machine, one user, SQLite as the control plane,
and real provider execution isolated in git worktrees by default.

![Ariadne architecture overview](docs/assets/ariadne-architecture-overview.png)

The image above is a visual overview. The Mermaid diagrams below are the
authoritative architecture map.

## Architecture

```mermaid
flowchart LR
  User["User"]

  subgraph Surfaces["Product surfaces"]
    CLI["cli.py<br/>ariadne run, daemon, benchmark"]
    Frontend["frontend/<br/>Next.js App Router"]
  end

  subgraph API["HTTP boundary"]
    FastAPI["api.py<br/>FastAPI REST + SSE"]
  end

  subgraph App["Application orchestration"]
    Runner["runner.py<br/>run_intent()"]
    Daemon["daemon.py<br/>claim + execute"]
    Orchestrator["orchestrator.py<br/>squad leader loop"]
    Policy["policy.py<br/>execution gate"]
  end

  subgraph Domain["Lightweight DDD core"]
    Models["models.py<br/>Issue, TaskRun, AgentProfile, Squad, RuntimeLease"]
    TaskService["service/task_service.py<br/>claim, transition, retry"]
    LeaseService["service/lease_service.py<br/>heartbeat, release, expiry"]
  end

  subgraph Persistence["Persistence"]
    Store["store/__init__.py<br/>Store facade"]
    Repos["store/*_repo.py<br/>pure SQL + row mapping"]
    SQLite[("SQLite ariadne.db")]
  end

  subgraph Execution["Execution backends"]
    Registry["backends.py<br/>in-process registry"]
    DryRun["DryRunBackend"]
    Shell["Shell backend base"]
    Worktree["isolated git worktree"]
    Codex["Codex CLI"]
    Claude["Claude Code CLI"]
  end

  User --> CLI
  User --> Frontend
  Frontend --> FastAPI
  CLI --> Runner
  FastAPI --> Runner
  FastAPI --> SQLite
  Runner --> Daemon
  Daemon --> Orchestrator
  Daemon --> Policy
  Daemon --> TaskService
  Daemon --> Registry
  Orchestrator --> TaskService
  TaskService --> Store
  LeaseService --> Store
  Policy --> Store
  Store --> Repos
  Repos --> SQLite
  Registry --> DryRun
  Registry --> Shell
  Shell --> Worktree
  Worktree --> Codex
  Worktree --> Claude
```

### Runtime flow

```mermaid
sequenceDiagram
  participant U as User
  participant F as Frontend or CLI
  participant A as api.py or cli.py
  participant R as runner.py
  participant D as daemon.py
  participant S as service/store
  participant B as backend
  participant DB as SQLite

  U->>F: submit issue or run command
  F->>A: POST /api/issues or ariadne run
  A->>R: run_intent()
  R->>S: create Issue and enqueue TaskRun
  S->>DB: persist issue, taskrun, timeline
  R->>D: drive daemon unless detached
  D->>S: claim TaskRun with RuntimeLease
  S->>DB: taskrun_preparing, lease_acquired
  D->>B: execute handoff
  B-->>D: progress, result, diff, changed files
  D->>S: complete, fail, retry, or cancel
  S->>DB: timeline and activity events
  F-->>DB: SSE reads persisted events through api.py
```

### Frontend module

```mermaid
flowchart LR
  Home["components/HomePage.tsx<br/>issue list + new task"]
  Prompt["components/ShellPrompt.tsx<br/>title, backend, mode"]
  Detail["components/IssueDetailPage.tsx<br/>metadata, transcript, diff"]
  Transcript["components/Transcript.tsx"]
  APIClient["lib/api.ts<br/>fetch + EventSource"]
  Types["lib/types.ts<br/>manual API contract"]
  Backend["api.py<br/>REST + SSE"]

  Home --> Prompt
  Home --> APIClient
  Prompt --> APIClient
  Detail --> APIClient
  Detail --> Transcript
  APIClient --> Types
  APIClient --> Backend
```

The frontend is a separate Next.js project in `frontend/`. It does not import
Python, read SQLite, or share generated types with the backend. Its contract is
HTTP JSON plus SSE events.

### API and runner module

```mermaid
flowchart TD
  REST["REST endpoints<br/>/api/issues, /api/taskruns, /api/agents"]
  SSE["SSE endpoint<br/>/api/events"]
  Payloads["response payload builders"]
  Runner["runner.py run_intent()"]
  Store["Store facade"]
  Timeline["issue_timeline_event<br/>activity_log"]

  REST --> Payloads
  REST --> Runner
  Payloads --> Store
  SSE --> Timeline
  Runner --> Store
```

`api.py` is the HTTP boundary. It serializes data, exposes CORS for the local
Next.js app, and reuses `runner.py` for issue creation and execution. It should
not contain business rules or SQL that belongs in service or repository code.

### Daemon and orchestration module

```mermaid
flowchart TD
  Start["daemon.start()"]
  Register["register RuntimeMachine<br/>probe capabilities"]
  Claim["claim oldest queued TaskRun"]
  IsLeader{"Squad leader task?"}
  Orchestrator["orchestrator.handle_leader_task()"]
  Member["execute member task"]
  Policy["evaluate_execution_policy()"]
  Backend["ExecutionBackend.execute()"]
  Result{"success?"}
  Complete["complete_taskrun"]
  Fail["fail_taskrun"]
  Retry{"attempts remain?"}
  Requeue["retry_taskrun"]
  EventLoop["on_member_task_complete()"]

  Start --> Register --> Claim --> IsLeader
  IsLeader -->|yes| Orchestrator --> EventLoop
  IsLeader -->|no| Member --> Policy --> Backend --> Result
  Result -->|yes| Complete --> EventLoop
  Result -->|no| Fail --> Retry
  Retry -->|yes| Requeue --> EventLoop
  Retry -->|no| EventLoop
```

The daemon is the local runtime loop. It registers the machine, claims TaskRuns
through leases, executes work, records progress, classifies failures, and
retries when the policy allows it.

### Domain services and persistence module

```mermaid
flowchart LR
  Models["models.py<br/>Pydantic entities and enums"]

  subgraph Services["service/"]
    TaskService["TaskService<br/>state machine, claim, retry"]
    LeaseService["LeaseService<br/>lease lifecycle"]
  end

  subgraph StoreLayer["store/"]
    Facade["Store facade<br/>backward-compatible API"]
    TaskRepo["task_repo.py"]
    IssueRepo["issue_repo.py"]
    RuntimeRepo["runtime_repo.py"]
    SkillRepo["skill_repo.py"]
    SquadRepo["squad_repo.py"]
    BenchmarkRepo["benchmark_repo.py"]
    Schema["schema.py"]
  end

  DB[("SQLite")]

  TaskService --> Facade
  LeaseService --> Facade
  Facade --> TaskRepo
  Facade --> IssueRepo
  Facade --> RuntimeRepo
  Facade --> SkillRepo
  Facade --> SquadRepo
  Facade --> BenchmarkRepo
  Schema --> DB
  TaskRepo --> DB
  IssueRepo --> DB
  RuntimeRepo --> DB
  SkillRepo --> DB
  SquadRepo --> DB
  BenchmarkRepo --> DB
  Models -.used by.-> Services
  Models -.used by.-> StoreLayer
```

This is the first lightweight DDD cut. Repositories handle SQL and row mapping.
Services handle state transitions, claim rules, retry rules, and lease
lifecycle. The `Store` facade remains so older call sites keep working while
the internals move toward clearer boundaries.

### Execution backend module

```mermaid
flowchart TD
  Context["ExecutionContext<br/>task, handoff, target repo, skills"]
  Registry["backends.py registry"]
  DryRun["DryRunBackend"]
  Shell["Shell backend base"]
  Handoff["temporary handoff file"]
  Worktree["detached git worktree<br/>default for real providers"]
  Command["render provider command"]
  Codex["CodexBackend<br/>codex exec"]
  Claude["ClaudeBackend<br/>claude --print"]
  Progress["stdout progress callback"]
  Diff["git diff + changed_files"]
  Result["ExecutionResult"]

  Context --> Registry
  Registry --> DryRun --> Result
  Registry --> Shell
  Shell --> Handoff
  Shell --> Worktree
  Shell --> Command
  Command --> Codex
  Command --> Claude
  Codex --> Progress
  Claude --> Progress
  Codex --> Diff
  Claude --> Diff
  Progress --> Result
  Diff --> Result
```

Real provider backends spawn external CLIs. By default they run in a detached
worktree and capture the patch before cleanup. Direct writes to the target
workspace require the explicit `--write-workspace` escape hatch.

### Product facts

```mermaid
erDiagram
  ISSUE ||--o{ TASK : owns
  TASK ||--o{ RUNTIME_LEASE : claimed_by
  RUNTIME_MACHINE ||--o{ RUNTIME_CAPABILITY : exposes
  RUNTIME_CAPABILITY ||--o{ RUNTIME_LEASE : grants
  AGENT_PROFILE ||--o{ TASK : executes
  AGENT_PROFILE ||--o{ AGENT_PROFILE_SKILL : binds
  SKILL ||--o{ AGENT_PROFILE_SKILL : binds
  SQUAD ||--o{ SQUAD_MEMBER : has
  SQUAD ||--o{ LEADER_DECISION : records
  ISSUE ||--o{ ISSUE_TIMELINE_EVENT : records
  ISSUE ||--o{ BENCHMARK_RUN : measures
```

SQLite stores product facts, not just implementation logs. The UI, CLI,
benchmarks, and replay tooling all read from these facts.

## Implemented capabilities

- SQLite control plane: issues, TaskRuns, RuntimeMachines,
  RuntimeCapabilities, RuntimeLeases, AgentProfiles, Skills, Squads,
  LeaderDecisions, IssueTimeline events, ActivityLog entries, and
  BenchmarkRuns are persisted as product facts.
- Atomic runtime claim loop: the daemon claims queued TaskRuns, enforces one
  active TaskRun per issue, honors runtime and profile concurrency limits,
  executes work, classifies failures, and retries when policy allows it.
- Squad orchestration: a leader receives a structured briefing and emits
  `action`, `no_action`, `failed`, or `done`; member terminal states re-trigger
  leader evaluation.
- Shared intent runner: `ariadne run` and `POST /api/issues` use the same
  `runner.py` path instead of duplicating business logic in CLI and API code.
- Pluggable execution backends: dry-run, Codex, and Claude Code backends share
  the same in-process registry used by tests and benchmark providers.
- Isolation-first real execution: real providers run in detached git worktrees
  by default. Direct target writes require `--write-workspace`.
- Local Next.js frontend: the web UI lives in `frontend/`, calls only HTTP
  endpoints, and subscribes to persisted SSE events for live progress.
- Artifact-backed benchmarks: benchmark runners export manifests, metrics,
  SQLite facts, summaries, and hashes so reported numbers can be replayed.

## Quickstart

```bash
uv sync --extra dev
uv run ariadne run "Write a hello helper" --backend dry-run
```

Run multiple explicit tasks:

```bash
uv run ariadne run \
  "Write a hello helper" \
  "Write an add helper" \
  --backend dry-run
```

Run through a squad leader:

```bash
uv run ariadne run --squad "Refactor this module" --backend dry-run
```

Start the local API:

```bash
uv run ariadne api-serve
```

Start the local frontend in another shell:

```bash
cd frontend
npm install
npm run dev
```

Then open `http://localhost:3000`. The frontend expects the backend on
`http://localhost:8000` unless `NEXT_PUBLIC_API_BASE_URL` is set.

## Five-minute v1 demo

From a clean checkout:

```bash
uv sync --extra dev
uv run ariadne demo-v1 --reset
```

The demo creates a local dry-run squad, executes the daemon, and records
RuntimeMachines, RuntimeCapabilities, TaskRuns, RuntimeLeases, IssueTimeline
events, LeaderDecisions, and a BenchmarkRun. It does not require Codex, Claude,
or provider credentials.

Inspect the generated database:

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

| Mechanism | Multica source | What Ariadne keeps | What Ariadne changes |
|-----------|----------------|--------------------|----------------------|
| Task state machine | migration 001 + 055 | States, failure classification, retry chain | SQLite and `BEGIN IMMEDIATE` instead of Postgres |
| Squad briefing | `squad_briefing.go` | Three-part briefing: protocol, roster, instructions | Structured `DelegationDecision` instead of mention markdown |
| Daemon claim loop | `daemon.go` | Poll, claim, execute, heartbeat, stale recovery | Local synchronous loop, SQLite storage, no hosted service |

See [docs/architecture/multica-mapping.md](docs/architecture/multica-mapping.md)
for the mechanism-by-mechanism mapping.

## Design decisions

1. SQLite over JSON or JSONL: state transitions need transactional integrity,
   but Ariadne does not need a server database.
2. Structured delegation over free-form mention text: `DelegationDecision` is
   testable, replayable, and validates against the squad roster.
3. LLM injected, not hardcoded: the orchestrator receives an `llm_decide`
   callable. Tests use deterministic decision logic.
4. Isolation is the default: real CLI execution uses detached git worktrees
   unless the caller explicitly passes `--write-workspace`.
5. Skills are capability packages: bound skills materialize prompt content,
   allowed tools, and verification commands into TaskRun handoffs.
6. Verification evidence is recorded, not used as a hidden hard gate: failed
   skill verification stays visible for leader re-evaluation.
7. Frontend and backend are separate projects: Python owns control-plane logic;
   Next.js owns UI and talks to the backend only through HTTP.

Detailed decision records live under [docs/adr](docs/adr/):

- [ADR 0010: Open Execution Backend Registry](docs/adr/0010-open-execution-backend-registry.md)
- [ADR 0011: Provider Session Resume and MCP Config Injection](docs/adr/0011-provider-session-resume-and-mcp-config.md)
- [ADR 0012: Skills as Capability Packages](docs/adr/0012-skills-as-capability-packages.md)
- [ADR 0013: Isolation-First Real Backend Execution](docs/adr/0013-isolation-first-real-backend-execution.md)

## Testing

```bash
uv run ruff check src/ariadne/
uv run pytest tests/ -v
cd frontend && npm test && npm run build
```

The backend suite covers state transitions, atomic claim, TaskRun
compatibility, runtime registration, leases, IssueTimeline events,
AgentProfiles, Skills, LeaderDecisions, ExecutionPolicy, BenchmarkRuns, squad
orchestration, backend isolation, backend registry extension, session resume,
MCP config injection, skill verification evidence, and the clean-checkout demo.

The frontend suite covers formatting helpers and the local browser smoke path.

## Benchmark evidence

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

Dry-run, synthetic real-backend smoke, live Codex or Claude backend runs, LLM
routing, local pytest or ruff, and GitHub CI are reported as separate accounts.
CI pass rate is only reportable from GitHub checks. Local pytest is not a
substitute for CI. Live Codex or Claude patch success requires the provider CLI
and is not mixed with dry-run or synthetic smoke results.

Current comparison output using `--backend dry-run` is simulated and must be
reported as simulated. Real Codex or Claude comparison numbers remain unfilled
until a real environment with provider CLI credentials runs
`ariadne benchmark-compare --backend codex` or
`ariadne benchmark-compare --backend claude-code`.

## Project structure

```text
src/ariadne/
├── api.py                 # FastAPI REST + SSE boundary
├── cli.py                 # Typer CLI entry point
├── runner.py              # Shared intent runner for CLI and API
├── daemon.py              # Poll, claim, execute, heartbeat, retry
├── orchestrator.py        # Squad leader delegation loop
├── briefing.py            # Squad briefing generation
├── llm_decide.py          # OpenAI-compatible leader decision adapter
├── backends.py            # DryRun, Codex, Claude, registry, isolation, diff
├── policy.py              # Layered ExecutionPolicy gate
├── eval.py                # BenchmarkRun from product facts
├── benchmarking.py        # Artifact-backed benchmark runners
├── models.py              # Pydantic domain models and enums
├── service/
│   ├── task_service.py    # TaskRun state machine, claim, retry
│   └── lease_service.py   # RuntimeLease lifecycle
└── store/
    ├── __init__.py        # Store facade
    ├── schema.py          # SQLite schema
    ├── base.py            # connection, transaction, row mapping
    ├── task_repo.py
    ├── issue_repo.py
    ├── runtime_repo.py
    ├── skill_repo.py
    ├── squad_repo.py
    └── benchmark_repo.py

frontend/
├── app/                   # Next.js App Router pages
├── components/            # Terminal UI components
├── lib/                   # API client, types, formatting
└── package.json           # Independent frontend dependencies

benchmarks/runners/        # External benchmark runner entry points
tests/                     # Backend test suite
docs/                      # Architecture docs, ADRs, delivery plan, assets
```

`src/ariadne/dashboard.html` still exists as a legacy inspection dashboard. New
UI work belongs in `frontend/`; the legacy HTML file should be removed once the
Next.js frontend covers the needed inspection path.

## Current limitations

- `POST /api/issues` currently reuses `runner.py` directly. It works for
  dry-run and small real tasks, but long real-provider tasks still need better
  detached execution and heartbeat UX.
- `IssueStatus` has no first-class `failed` state yet. Failed TaskRuns are
  visible, but issue-level failure semantics need hardening.
- Some older CLI, API, benchmark, and policy paths still read SQLite through
  the `Store` connection for narrow reporting queries. New business logic
  should go through services and repositories.

## Non-goals

- No hosted multi-tenant service.
- No auth, billing, Postgres, Redis relay, or distributed scheduler.
- No RAG, vector database, or knowledge pipeline in this runtime.
- No autopilot, scheduled inbox work, Feishu sync, GitHub issue sync, or memory
  product layer.
- No full Multica clone. Ariadne keeps the local mechanisms it needs and leaves
  the SaaS shell out.
