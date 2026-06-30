# Multica Mapping

> Each mechanism in multica-py → the multica source file it derives from.
> Read these multica source files to understand the original design before implementing.

## Mechanism 1: Task State Machine

| multica-py | multica source | What we keep | What we change |
|------------|---------------|--------------|----------------|
| `models.py` TaskStatus | `migration 001` CHECK constraint | 6 states (queued→claimed→running→completed/failed/cancelled) | Added `claimed` as explicit state (multica uses `dispatched`) |
| `models.py` FailureReason | `migration 055` failure_reason column | 5 failure types | Same classification, Python enum instead of TEXT CHECK |
| `store.py` claim_task | `handler/daemon.go:1233` ClaimTaskByRuntime | Atomic claim pattern | SQLite `BEGIN IMMEDIATE` instead of Postgres row lock |
| `store.py` retry_task | `migration 055` attempt/max_attempts/parent_task_id | Retry chain | Same model, Python implementation |
| `daemon.py` stale recovery | `daemon.go` stale dispatched task reclaim | Heartbeat timeout → back to queued | Simplified: no `waiting_local_directory` state |

### What we dropped

- `dispatched` state (merged into `claimed` — multica needs both because server and daemon are separate processes; we're single-process)
- `waiting_local_directory` state (multica-specific for local directory resource locking)
- per-agent `max_concurrent_tasks` (single-worker daemon, no concurrency needed for v1)
- GC / workdir cleanup (out of scope)

## Mechanism 2: Squad Briefing

| multica-py | multica source | What we keep | What we change |
|------------|---------------|--------------|----------------|
| `briefing.py` SquadBriefing | `squad_briefing.go:112` buildSquadLeaderBriefing | 3-section structure (protocol + roster + instructions) | Structured Pydantic model, not string concatenation |
| `briefing.py` Operating Protocol | `squad_briefing.go:20` squadOperatingProtocol const | Core rule: "coordinate, NOT do the work yourself" | Adapted for structured delegation (no @mention syntax) |
| `briefing.py` RosterEntry | `squad_briefing.go:129` buildSquadRoster | Member name + role + skills | Added `backends` field (multica infers from runtime) |
| `orchestrator.py` DelegationDecision | `squad_briefing.go` @mention markdown | Leader delegates to member | Pydantic model instead of `[@Name](mention://...)` |
| `orchestrator.py` event loop | `issue_trigger.go` dispatchIssueRun | Member completion → re-trigger leader | Event-driven callback, not comment-parsing |

### What we dropped

- @mention markdown parsing (replaced by structured DelegationDecision)
- `multica squad activity` CLI command (evaluation recording simplified)
- `no_action` outcome (leader either delegates or marks done, no silent exit)
- human squad members (agent-only, v1)
- squad archival with issue reassignment

## Mechanism 3: Daemon Claim Loop

| multica-py | multica source | What we keep | What we change |
|------------|---------------|--------------|----------------|
| `daemon.py` poll loop | `daemon.go` poll interval (3s default) | Periodic claim attempt | Same pattern, asyncio |
| `daemon.py` heartbeat | `daemon.go` heartbeat (15s default) | Runtime liveness signal | SQLite timestamp, not server API call |
| `daemon.py` cancel detection | `daemon.go:2864` watchTaskCancellation | Check if task was cancelled during execution | Poll task status in DB, not server API |
| `backends.py` progress | `client.go:238` ReportProgress | Progress reporting during execution | Callback to daemon, not HTTP POST to server |
| `backends.py` execution | `daemon.go:3387` runTask | Spawn agent CLI, capture output | Same subprocess approach, simplified env setup |

### What we dropped

- 12 CLI auto-detection (we hardcode codex + claude-code)
- execenv / worktree / skill bundle materialization (simplified to handoff file)
- cloud runtime support
- daemon profiles (single profile, single daemon)
- auto-update mechanism
- `MULTICA_*` env var matrix (simplified to `ARIADNE_*` pattern from existing code)

## Summary: Why these 3 and not others

| multica feature | Included? | Reason |
|----------------|-----------|--------|
| Task state machine | ✅ | Core control plane — without it, no durable orchestration |
| Squad briefing + delegation | ✅ | Core differentiation — leader/member separation is the multica essence |
| Daemon claim loop | ✅ | Ties state machine to execution — makes it real, not theoretical |
| Autopilot / scheduled tasks | ❌ | Nice but not essential for demonstrating orchestration |
| Reusable skills | ❌ | Important concept but adds scope; handoff prompt carries skills |
| Multi-workspace | ❌ | Local single-user, no isolation needed |
| WebSocket real-time | ❌ | CLI output is sufficient for demo; API is optional thin layer |
| Project resources | ❌ | Adds complexity without proving orchestration depth |
| Chat sessions | ❌ | Different interaction model, adds scope |
| GitHub integration | ❌ | Ariadne already proved this; not the point of this project |
