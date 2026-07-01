# ADR 0013: Isolation-First Real Backend Execution

Date: 2026-07-01

## Status

Accepted.

## Context

The initial real-backend safety design used two confirmations before spawning
Codex or Claude Code:

- an environment variable enabling external execution
- a per-task confirmation flag

That made accidental execution less likely, but it did not solve the deeper
problem. It pushed the designer's anxiety onto the operator: the user had to
remember the correct incantation, and once enabled the command could still write
directly into the target repository.

For a local managed-agent runtime, safety should come from architecture. The
default path should make the safe behavior natural, not ask the user to approve
dangerous behavior with a flag.

## Decision

Make real backend execution isolation-first:

- Git targets run in a detached temporary worktree by default.
- Non-git targets are blocked unless the caller explicitly chooses
  `--write-workspace`.
- Direct target writes are represented internally by the legacy
  `ExecutionContext.confirm_execution` compatibility field.
- Worktree creation failure is a hard stop. The backend never silently falls
  back to writing the target repository.
- Execution results include `metadata["worktree_audit"]` with target path,
  execution path, isolation/write-workspace mode, rendered command, patch
  capture status, and original-repo cleanliness when applicable.

The control-plane policy layer still gates real providers by runtime lease,
machine, capability, AgentProfile policy, skill policy, timeout bounds, target
existence, and redaction policy. It no longer asks for the old environment
variable or confirmation gate.

## Consequences

The common path is safer and easier: run the daemon against a git repo and the
backend writes a disposable worktree, captures the patch, then cleans up. The
dangerous path has the honest name `--write-workspace`.

The tradeoff is that a real backend patch is captured as an artifact, not
applied to the target checkout by default. Applying a patch remains a separate
human or product workflow decision.

## Evidence

- `src/ariadne/backends.py` creates detached worktrees by default, blocks
  non-git targets without write-workspace, and records `worktree_audit`.
- `src/ariadne/cli.py` exposes `--write-workspace` for daemon and squad runs.
- `src/ariadne/policy.py` keeps runtime/profile policy checks but removes the
  old environment/confirmation task gate.
- `tests/test_backends.py`, `tests/test_daemon.py`, and
  `tests/test_execution_policy.py` cover the isolation-first behavior.
