# Daemon Lifecycle & Crash Recovery

> Derives from: multica `server/cmd/multica/cmd_daemon.go` (background daemon start),
> `server/internal/daemon/daemon.go:601` (Run + resident loops),
> `server/cmd/server/runtime_sweeper.go` (orphan/timeout state machine),
> `server/internal/handler/task_lifecycle.go:24` (`RecoverOrphanedTasks`).
>
> This doc closes the gap between the state machine's *promise* and the code's
> *reality*: [task-state-machine.md](task-state-machine.md) already defines
> `runtime_recovery` = "Daemon restarted mid-execution, task was running" (line 69),
> but no code recovers `running` orphans and no legal transition edge exists for it.
> deep-011-F implements what the spec already committed to.

## Purpose

Make "hand work to the agent team and walk away" real. Today the daemon is a
synchronous loop object driven by the caller's foreground process; when the CLI
exits (Ctrl-C, closed terminal, or an API request returning), in-flight agent
work is orphaned and its task row is stuck in `running` forever. This doc
specifies the **process lifecycle** (the daemon outlives the terminal) and the
**crash-recovery contract** (a restarted daemon reconciles orphaned `running`
tasks), so that a walked-away user returns to truthful state instead of a lie.

This is a *process-lifecycle* concern, distinct from deep-011-B's
*request-lifecycle* concern. deep-011-B stopped the HTTP request from blocking on
execution (enqueue-and-return). deep-011-F stops the **daemon process itself**
from being tied to a terminal, and reconciles what a crash left behind.

## The multica lesson (why we do NOT copy its architecture)

multica's "walk away and reconnect" works because a central **server (Postgres +
Redis)** is the authoritative store; the daemon is only an executor and progress
never lives in the client or agent. That requires Postgres, Redis, a server
process, and a WebSocket — all four are forbidden by this project's local-first
boundary.

Ariadne's answer: **fold the role multica's server plays into the local resident
daemon itself.** One process is both authority (owns SQLite state + queue +
persisted progress) and executor.

```
multica:   client ─► server(authority+queue+persistence) ─► daemon(exec) ─► agent
ariadne:   client(CLI/frontend) ─► resident daemon(authority+queue+persistence,
                                    sitting directly on SQLite) ─► agent
```

Fewer moving parts (one process, no Postgres/Redis/WS), and the collapse *is* the
local-first value proposition: `ari daemon start` and one SQLite file, versus a
server + Redis others must stand up.

## Boundary

```
In scope (deep-011-F)                     Out of scope
─────────                                 ───────────
daemon backgrounds itself (setsid)        systemd / launchd service units
PID file + log file lifecycle             multi-worker / worker pool
`running` orphan reconciliation on start  distributed lease arbitration
heartbeat-staleness → orphan detection    live process re-attach / resume-in-place
`ari daemon start/stop/status` surface    Redis TTL liveness, WebSocket wakeup
single resident daemon per machine        auto-start daemon from CLI/API implicitly
```

Explicitly NOT done: re-attaching to a still-running agent subprocess after
daemon restart (the subprocess is orphaned by the crash; we reconcile the task
row, we do not adopt the process). Recovery means **reclassify + optionally
re-queue**, not resume-in-place.

## Process model

```
                        ari daemon start
                              │
              ┌───────────────┴────────────────┐
              │  double-fork / os.setsid        │  detach from controlling terminal
              │  write PID → ~/.ariadne/daemon.pid
              │  redirect stdout/stderr → ~/.ariadne/daemon.log
              └───────────────┬────────────────┘
                              ▼
              ┌────────────────────────────────┐
              │  resident daemon process        │  outlives the launching terminal
              │  ┌──────────────────────────┐   │
              │  │ startup: recover_orphans()│   │  ← runs ONCE before the loop
              │  └──────────────────────────┘   │
              │  ┌──────────────────────────┐   │
              │  │ poll-claim-execute loop   │   │  ← existing Daemon.start() body
              │  │  (heartbeat each iter)    │   │
              │  └──────────────────────────┘   │
              └────────────────────────────────┘
                              ▲
              client (CLI / frontend) reads state + progress from SQLite;
              never holds execution, safe to disconnect and reconnect
```

The agent subprocess remains a short-lived child spawned per task (unchanged from
today, matching multica's "agent is a short-lived child, not resident"). What
changes is that its **parent is now a detached resident daemon**, not the user's
terminal.

### Backgrounding (chosen approach)

Self-daemonize via `os.setsid` + fork, matching multica's `daemon start` +
`~/.multica/daemon.log` pattern. Rationale (recorded in ADR-0014):

- **Zero external dependency**, stays inside the local-first boundary. No
  platform-specific unit files.
- Rejected: `systemd`/`launchd` (platform-bound, heavy install for a 秋招 demo);
  "tell the user to `nohup`" (leaves the walk-away promise as a manual chore,
  contradicts the "让大家懒一点" product tone).

Obligations that come with self-daemonizing (all in scope):

- **PID file** `~/.ariadne/daemon.pid` — single-instance guard (refuse to start
  if a live PID is already recorded), and the handle `ari daemon stop` uses.
- **Log file** `~/.ariadne/daemon.log` — stdout/stderr have no terminal after
  detach, so they redirect here. This is the only way a walked-away user sees
  daemon-level errors.
- **Reap/exit hygiene** — SIGTERM handler flips the loop's `_running = False` for
  a clean stop; stale PID file (process dead) is detected and overwritten on next
  start.

## Crash-recovery contract

A restarted daemon MUST reconcile tasks the previous daemon left mid-flight,
**before** entering the claim loop. Today `recover_stale_claims` only handles
`claimed` (never-started) tasks; `running` orphans are invisible.

```
recover_orphans(runtime_id, now)   # called once at startup, before the loop
  │
  ├─ SELECT * FROM task WHERE status = 'running'
  │     AND (runtime_id = <this daemon's prior runtime>       # our own crash
  │          OR last_heartbeat_at < now - orphan_timeout)     # any dead runtime
  │
  └─ for each orphan:
        reclassify → failed, failure_reason = 'runtime_recovery'
        if attempt < max_attempts:  retry_task()  → new queued task (parent set)
        else:                       stays failed (terminal), issue may go FAILED
        emit activity_log event: {event_type: 'orphan_recovered', ...}
```

Why reclassify-then-retry instead of resume-in-place: the agent subprocess died
with its parent; there is no live process to reconnect to, and re-running under a
fresh worktree is the isolation-safe, deterministic recovery. `runtime_recovery`
already carries `Retry? = yes, immediate` in the failure table — this makes that
row real.

### Heartbeat becomes a consumer, not just a writer

deep-011-C persists `backend_heartbeat` and advances lease `last_heartbeat_at`
during execution (writer side). deep-011-F adds the **reader side**: a running
task whose lease heartbeat is older than `orphan_timeout` is treated as an orphan
of a dead runtime. Single-machine, so this is a table scan at startup, not a
resident sweeper goroutine — no Redis TTL needed.

```python
ORPHAN_TIMEOUT_SECONDS = 90   # > deep-011-C heartbeat interval (10s) with margin
```

Chosen 90s (not 60s like `stale_claim_timeout`) so a healthy long execution that
merely paused stdout is never mistaken for dead: heartbeat fires every 10s, so
90s means ~9 missed beats before we declare it orphaned.

## State machine additions

Adds the missing edges to [task-state-machine.md](task-state-machine.md).

| From | To | Trigger | Actor |
|------|----|---------|-------|
| running | failed | `recover_orphans()` at daemon startup, `failure_reason=runtime_recovery` | daemon recovery |
| failed | queued | orphan with `attempt < max_attempts` → `retry_task()` | daemon recovery |

The existing table already lists `claimed → queued` for "claim timeout / daemon
restart". This extends restart handling to the `running` state, which the diagram
and failure table anticipated but the transition table omitted.

## CLI surface

```
ari daemon start     # self-daemonize; refuse if live PID exists; recover orphans; loop
ari daemon stop      # SIGTERM the PID; wait for clean exit; remove PID file
ari daemon status    # read PID file + daemon_state heartbeat; report alive/dead,
                     #   last heartbeat age, active runtime, in-flight task count
```

`status` must distinguish three cases truthfully: (a) PID file present + process
alive + fresh heartbeat = healthy; (b) PID file present + process dead = crashed
(and orphans await next start); (c) no PID file = never started. No case is
reported as healthy unless the heartbeat is actually fresh.

## What this does NOT change

- The claim loop body, atomic claim SQL, and per-issue serialization (unchanged).
- deep-011-B's enqueue-and-return API path (F is what makes B's "assumes a daemon
  is running" precondition into a real capability).
- deep-011-C's heartbeat writer (F consumes what C writes).
- No implicit auto-start of the daemon from CLI/API — starting it is an explicit
  user action; the docs/frontend surface `ari daemon start` as the prerequisite,
  now a one-liner that truly backgrounds.

## Tests Required

| Test | What it verifies |
|------|-----------------|
| `test_daemon_start_backgrounds_and_survives_parent_exit` | After `ari daemon start`, the launching process can exit and the daemon PID stays alive |
| `test_daemon_start_refuses_when_live_pid_exists` | Second `start` with a live PID is rejected, not a double daemon |
| `test_daemon_start_overwrites_stale_pid` | PID file whose process is dead is reclaimed on next start |
| `test_daemon_stop_terminates_and_removes_pidfile` | `stop` SIGTERMs the daemon, waits for exit, removes PID file |
| `test_daemon_status_reports_healthy_crashed_absent` | The three truthful states are distinguished by PID liveness + heartbeat age |
| `test_recover_orphans_reclassifies_running_to_failed` | A `running` task from a prior runtime → `failed`, `failure_reason=runtime_recovery` |
| `test_recover_orphans_retries_when_attempts_remain` | Orphan with `attempt < max_attempts` → new `queued` task with `parent_task_id` set |
| `test_recover_orphans_terminal_when_attempts_exhausted` | Orphan at `max_attempts` stays `failed`, no infinite retry |
| `test_recover_orphans_runs_before_claim_loop` | Recovery executes once at startup before the first claim |
| `test_stale_heartbeat_running_task_is_orphaned` | `running` task with lease heartbeat older than `orphan_timeout` is detected as orphan |
| `test_fresh_heartbeat_running_task_is_not_orphaned` | A long execution still heartbeating is NOT reclassified |
| `test_orphan_recovery_emits_activity_event` | Each recovery writes an `orphan_recovered` activity_log event (visible via SSE) |

## Boundary reminders (local-first,守 CLAUDE.md)

- No Redis, no WebSocket, no worker pool, no Postgres.
- Single resident daemon per machine (multi-worker is post-秋招, already in backlog).
- Recovery is reclassify + re-queue, never resume-in-place / process adoption.
- Real backend re-execution after recovery still goes through the worktree
  isolation gate — a recovered retry never writes the main repo.
