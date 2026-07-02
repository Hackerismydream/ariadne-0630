# ADR 0014: Self-Daemonizing Resident Daemon + `running` Orphan Recovery

Date: 2026-07-02

## Status

Proposed. Implemented by deep-011-F (depends on deep-011-B and deep-011-C).

## Context

The product tone is "hand work to the agent team and walk away" (让大家懒一点).
That is a *process-lifecycle* commitment: the executor must outlive the terminal
that launched it, and a crash must not silently strand in-flight work.

The current code does not honor it. `daemon.py` is a synchronous loop object
("Synchronous loop — no threads, no asyncio") driven by whatever foreground
process called `start()`. Real backend execution blocks that process via
`proc.wait()`. When the CLI exits — Ctrl-C, a closed terminal, or an API request
returning — the agent work is orphaned and its task row is stuck in `running`
with no code path back.

Two adjacent PRs get close but stop short:

- deep-011-B made the HTTP request enqueue-and-return, so the *request* no longer
  blocks on execution. But it left the daemon itself tied to a terminal and wrote
  the precondition "the UI assumes a daemon is already running" into docs — a
  chore pushed onto the user, not a capability.
- deep-011-C persists heartbeats during execution (writer side) but nothing reads
  them to detect a dead runtime.

Meanwhile `task-state-machine.md` already defines `runtime_recovery` = "Daemon
restarted mid-execution, task was running" and lists it as retryable, but no code
implements it and the transition table has no `running →` recovery edge. The spec
promised recovery the code never delivered.

multica solves the equivalent problem with a central server (Postgres + Redis)
as the authority and the daemon as a mere executor. That architecture is
forbidden here by the local-first boundary (no Postgres, Redis, server process,
or WebSocket).

## Decision

Fold the authority role multica assigns to its server into the local resident
daemon itself, and make that daemon a real background process with crash
reconciliation.

- **Self-daemonize** via `os.setsid` + fork on `ari daemon start`, matching
  multica's `daemon start` + log-file pattern. Rejected alternatives:
  `systemd`/`launchd` (platform-bound, heavy for a demo) and "tell the user to
  `nohup`" (leaves the walk-away promise a manual chore).
- **PID file** `~/.ariadne/daemon.pid` as single-instance guard and stop handle;
  **log file** `~/.ariadne/daemon.log` for the now-terminal-less stdout/stderr.
- **Recover `running` orphans once at startup, before the claim loop**:
  reclassify to `failed` with `failure_reason=runtime_recovery`, then
  `retry_task()` when `attempt < max_attempts`, else leave terminal. Recovery is
  **reclassify + re-queue, never resume-in-place** — the dead subprocess is not
  adopted; a retry re-runs under a fresh isolated worktree.
- **Heartbeat becomes a consumer**: a `running` task whose lease
  `last_heartbeat_at` is older than `ORPHAN_TIMEOUT_SECONDS = 90` (≈9 missed
  10s beats, above `stale_claim_timeout`'s 60s so a stdout-quiet long run is not
  misjudged) is treated as an orphan of a dead runtime. Single-machine, so this
  is a startup table scan, not a resident sweeper — no Redis TTL.
- **No implicit auto-start**: launching the daemon stays an explicit user action;
  the CLI/frontend surface `ari daemon start` as the prerequisite, now a true
  one-line backgrounding rather than a foreground babysit.

## Consequences

The walk-away promise becomes real: `ari daemon start`, close the terminal, come
back, `ari daemon status` reports truthful health, and a crash mid-execution
leaves a `failed`/re-queued task instead of a permanent `running` lie. This also
completes the L2 rung of the project's backend-depth story (crash recovery),
which is the most interview-valuable and was previously only half-present.

The collapse of "server + daemon" into one process is a feature, not a
compromise: fewer moving parts than multica (one process, one SQLite file, no
Postgres/Redis/WS), which *is* the local-first value proposition.

Tradeoffs and non-goals: recovery does not reconnect to a still-running
subprocess (it reconciles the task row only); a single resident daemon per
machine (multi-worker is deferred, in backlog); and self-daemonizing brings PID
staleness, log rotation, and reap hygiene as maintenance obligations the code
must own.

## Evidence

_(to be populated by deep-011-F implementation)_

- `src/ariadne/cli.py`: `ari daemon start/stop/status` with setsid backgrounding,
  PID-file single-instance guard, and three-state truthful status.
- `src/ariadne/daemon.py`: `recover_orphans()` runs once before the claim loop;
  consumes lease `last_heartbeat_at` for staleness.
- `docs/architecture/daemon-lifecycle.md`: full specification.
- `docs/architecture/task-state-machine.md`: `running → failed` and orphan
  `failed → queued` recovery edges.
- `tests/test_daemon.py`: backgrounding survival, PID guard, orphan
  reclassify/retry/terminal, heartbeat-staleness detection, startup ordering.
