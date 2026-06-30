# Delivery Plan

## Overview

4-week delivery, 4 phases (one per week). Each phase is independently
mergeable — after each phase ships, the system is in a usable state.

## Task Status

| Task | Status | Scope |
|------|--------|-------|
| core-001 | pending | Project scaffold + pyproject.toml + models.py |
| core-002 | pending | store.py (SQLite + state machine) |
| core-003 | pending | daemon.py (poll + claim + heartbeat) |
| squad-001 | pending | briefing.py (SquadBriefing generation) |
| squad-002 | pending | orchestrator.py (leader delegation + event loop) |
| squad-003 | pending | LangGraph supervisor graph integration |
| backend-001 | pending | backends.py (ExecutionBackend protocol + Codex + Claude) |
| backend-002 | pending | Safety gate + diff capture + progress reporting |
| eval-001 | pending | Evaluation layer (LLM-as-judge + benchmark harness) |
| polish-001 | pending | CLI commands + README + architecture diagram |
| polish-002 | pending | Test coverage gap fill + benchmark data collection |

## Phases

### Phase 1 (W1): Control Plane — issue/task CRUD + state machine + daemon

**Goal**: `cli issue create` → `cli daemon start` → task walks
queued→claimed→running→completed end-to-end (with dry-run backend).

**Tasks**: core-001, core-002, core-003
**Done when**: daemon polls, claims a queued task, marks it running, completes it,
status transitions are correct, all state machine tests pass.

### Phase 2 (W2): Squad Orchestration — leader briefing + delegation

**Goal**: Issue assigned to squad → leader claims → reads briefing → outputs
DelegationDecision → child task created → member claims → dry-run executes →
leader re-evaluated → issue marked done.

**Tasks**: squad-001, squad-002, squad-003
**Depends on**: Phase 1 complete
**Done when**: full squad delegation loop works end-to-end with dry-run backend,
all orchestration tests pass.

### Phase 3 (W3): Real Harness + Event Loop + Retry

**Goal**: Codex and Claude Code backends execute real tasks; retry on failure;
event loop handles member completion correctly with real execution latency.

**Tasks**: backend-001, backend-002
**Depends on**: Phase 2 complete
**Done when**: both backends execute real code changes in a test repo,
retry works on injected failures, safety gates block unauthorized execution.

### Phase 4 (W4): Evaluation + Polish + Benchmark

**Goal**: LLM-as-judge evaluation; README with architecture diagram;
benchmark data for single-agent vs squad comparison.

**Tasks**: eval-001, polish-001, polish-002
**Depends on**: Phase 3 complete
**Done when**: evaluation runs on completed tasks, README is self-contained,
benchmark data fills the `{待测}` placeholders in the resume.

## Execution Mode

Serial execution (no worktree parallelism in v1 — single developer, small scope).

```
core-001 → core-002 → core-003 → squad-001 → squad-002 → squad-003
→ backend-001 → backend-002 → eval-001 → polish-001 → polish-002
```

Each task: develop → commit → verify → merge → next task.

## Recovery

If interrupted, check task status in this file. Resume from the first
non-done task. If a task is `in-progress` with no committed code, reset to
`pending` and re-dispatch.
