# deep-011: Real Backend Usability And Correctness Fix Plan

Audience: Claude / implementation reviewer.

This document summarizes the first serious real-backend UX failure found after
deep-010. The frontend is usable for dry-run and small real Codex edits, but a
long real task exposed correctness, observability, and execution-model gaps.

## Incident

Manual UI submission:

- title/body: `帮我做一个超级玛丽`
- backend: `codex`
- mode: `squad`
- API DB: `/tmp/ariadne-demo.db`
- API server cwd: `/tmp/ariadne-demo-target`

Observed from the browser:

- The New Task modal stayed on `POST /api/issues running...`.
- The issue list showed one active task but no useful intermediate detail.
- The page looked frozen even though a real backend subprocess was running.

Observed from the runtime:

- `codex exec` ran in an isolated worktree, not in the Ariadne source tree.
- The first member task ran for 600s and timed out.
- Ariadne retried once; the retry also ran for 600s and timed out.
- No diff or changed files were captured.
- After both failures, the squad leader reactivated and marked the issue `done`.

Concrete DB result:

```text
issue-8027437246ff | done | 帮我做一个超级玛丽

taskrun-da08f02c16a1 | completed | leader action decision
taskrun-349b9d4df59c | failed    | timeout | execution timed out after 600s
taskrun-58f083c95300 | failed    | timeout | execution timed out after 600s
taskrun-fc17a852ef84 | completed | leader done decision
```

The final `done` is a false positive. The work did not complete.

## What Failed

### 1. POST Is Still A Long-Running Execution Boundary

`src/ariadne/api.py` currently handles `POST /api/issues` by directly calling
`runner.run_intent()`.

That is fine for dry-run and tiny real patches, but wrong for product UX:

- the browser request remains open for the entire backend execution;
- the New Task modal cannot navigate to the detail page until execution ends;
- real Codex/Claude runs can take minutes or hit the 600s timeout;
- users read the frozen modal as a broken app.

This is a product architecture issue, not a CSS issue.

### 2. Real Backend Silence Produces No Useful Progress

`src/ariadne/backends.py` streams progress only when the provider emits stdout
lines. During this incident, the timeline had only:

```text
progress_reported | starting codex execution
```

Then nothing visible for 600 seconds.

The runtime knows the task is still running, the subprocess is alive, and the
timeout budget is counting down, but that fact is not persisted as user-visible
events.

### 3. Runtime Lease Becomes Misleading During Long Execution

The active lease `expires_at` was one minute after claim time, but the backend
subprocess kept running for 600 seconds. The task stayed `running`, and the
subprocess was real, but lease metadata looked stale.

This makes the dashboard harder to trust:

- an operator cannot tell whether the task is healthy or orphaned;
- stale-claim recovery semantics are ambiguous while execution is active;
- long real provider calls need heartbeat updates tied to the active task.

### 4. Squad Completion Semantics Are Incorrect

`src/ariadne/orchestrator.py` gathers both completed and failed member results.
When the legacy deterministic decider returns `None`, `_coerce_leader_decision()`
currently treats any non-empty member result list as success:

```text
completed_results exists -> DONE
```

But in this incident the result list contained only failed timeout tasks. The
leader therefore closed the issue as `done` after all attempts failed.

Correct behavior:

- completed member work with a patch/result may allow `done`;
- failed member work must not close the issue as `done`;
- if attempts are exhausted and no successful member result exists, the issue
  should become a failure state or remain explicitly blocked/failed.

### 5. Retry Is Blind

After the first timeout, Ariadne scheduled a retry with the same handoff. The
retry also timed out. There is no visible retry plan, no shortened prompt, no
failure-specific remediation, and no user-facing explanation until timeout.

Blind retry is acceptable for transient provider errors, but weak for long,
underspecified coding tasks.

## Recommended Direction

Do not solve this with WebSocket, Redis, event bus, multi-node workers, or a
Multica-style SaaS architecture. Keep Ariadne local-first:

- SQLite remains the source of truth.
- SSE continues to poll persisted `issue_timeline_event` / `activity_log`.
- Real execution stays in isolated git worktrees by default.
- API still reuses `runner.run_intent()` / daemon/runtime components; do not
  duplicate orchestration logic in API routes.

The fix should be a mature local architecture:

1. create work immediately;
2. run long execution outside the HTTP request;
3. persist heartbeats and progress while the provider is silent;
4. represent failure truthfully.

## Implementation Plan

### Phase A: Correctness First - Failed Member Work Must Not Close Issues

Goal: no false `done`.

Changes:

- Add a failing test for the incident:
  - create squad issue;
  - simulate two failed member task attempts;
  - trigger leader re-evaluation;
  - assert the issue is not `done`.
- Update orchestrator decision coercion:
  - if at least one member task completed successfully, legacy `None` may map
    to `DONE`;
  - if all terminal member tasks are failed, map to `FAILED` or a non-success
    decision, not `DONE`.
- Prefer adding `IssueStatus.FAILED`.
  - Current issue statuses are insufficient: `backlog/todo/in_progress/done/cancelled`.
  - A real terminal failure needs first-class representation.
  - API and frontend status badges should map `failed` to `[ERR]`.

Acceptance:

- A max-attempts timeout chain cannot close an issue as `done`.
- API detail returns failed taskruns with `failure_reason=timeout`.
- Frontend renders issue failure visibly, not as `[DONE]`.

### Phase B: Make `POST /api/issues` Return Immediately For Real Runs

Goal: the user sees the detail page immediately.

Recommended API contract:

- `POST /api/issues`
  - creates issue and initial taskrun(s);
  - starts or schedules execution;
  - returns `202 Accepted` or `200` with a serialized run/enqueue result;
  - includes `issue_id`, initial taskruns, mode, backend, and `detached=true`.

Implementation options:

1. Use existing `run_intent(..., detach=True)` from the API, then run a
   background daemon loop for that issue.
2. Add an explicit `run_intent_async/background=True` wrapper that reuses the
   same runner/daemon internals but decouples HTTP lifetime from execution.

Preferred for this repo: option 1 or a thin wrapper over it. Keep business logic
in runner/daemon/orchestrator, not in `api.py`.

Frontend behavior:

- modal submit calls `POST /api/issues`;
- when `issue_id` returns, immediately route to `/issues/{id}`;
- detail page subscribes to SSE and renders state changes.

Acceptance:

- A real Codex task does not leave the user stuck in the modal.
- Browser can navigate to issue detail before backend execution completes.
- Existing dry-run smoke still passes.

### Phase C: Persist Running Heartbeats During Provider Silence

Goal: long-running backend calls visibly remain alive.

Runtime events to persist every 5-10 seconds while a subprocess is active:

```json
{
  "event_type": "backend_heartbeat",
  "taskrun_id": "taskrun-...",
  "payload": {
    "backend": "codex",
    "elapsed_seconds": 185,
    "timeout_seconds": 600,
    "execution_repo_path": "...",
    "pid": 7772
  }
}
```

Also update runtime lease heartbeat while execution is active:

- active lease `last_heartbeat_at` should move during long execution;
- lease should not appear stale solely because provider execution is long.

Acceptance:

- During a silent 600s provider run, `/api/events` emits periodic persisted
  heartbeat events.
- Frontend transcript shows elapsed time / timeout budget.
- Operators can distinguish "provider still alive" from "orphaned task".

### Phase D: Improve Failure And Retry Presentation

Goal: failures are understandable without reading SQLite.

Frontend detail page should show:

- taskrun attempt number and max attempts;
- parent retry chain;
- `failure_reason`;
- elapsed duration;
- last event time;
- no-diff explanation:
  - "no diff captured because execution failed";
  - "provider timed out after 600s";
  - not just `(no diff captured)`.

Runtime should emit a clearer retry event:

```json
{
  "event_type": "retry_scheduled",
  "payload": {
    "retry_taskrun_id": "...",
    "attempt": 2,
    "reason": "timeout"
  }
}
```

Acceptance:

- Timeout -> retry -> timeout is visible as a chain in the UI.
- Final issue status is `failed`, not visually mixed with successful leader
  task completion.

### Phase E: Add User Cancellation

Goal: users should not wait 600s if they know a run is wrong.

Recommended API:

- `PATCH /api/issues/{id}` with `status=cancelled` cancels queued/running tasks;
  or
- add a focused endpoint: `POST /api/issues/{id}/cancel`.

Runtime requirement:

- track subprocess group by active taskrun;
- cancellation should mark taskrun `cancelled`;
- best effort kill provider process group;
- release active lease.

Acceptance:

- A running Codex task can be cancelled from UI/API.
- DB records terminal `cancelled`, not `failed timeout`.

## Test Plan

Backend tests:

- `test_squad_failed_member_attempts_do_not_close_issue_done`
- `test_squad_failed_member_attempts_mark_issue_failed`
- `test_post_issue_can_detach_for_real_backend`
- `test_backend_heartbeat_events_are_persisted_during_silent_process`
- `test_active_lease_heartbeat_updates_during_long_execution`
- `test_cancel_running_issue_marks_task_cancelled_and_releases_lease`

Frontend tests:

- New Task submit routes to detail immediately for non-dry-run/background mode.
- Detail page renders running heartbeat.
- Detail page renders retry chain.
- Detail page renders failed issue and timeout reason.
- Detail page does not show `[DONE]` when all member taskruns failed.

Manual smoke:

1. Start API from disposable git repo.
2. Start Next.js frontend.
3. Submit a real `codex` task that edits README.
4. Confirm immediate navigation to detail.
5. Confirm heartbeat events while running.
6. Confirm diff on success.
7. Submit an intentionally too-large task or use a tiny timeout fixture.
8. Confirm timeout/retry/failure is represented truthfully.

## Non-Goals

- No Redis.
- No WebSocket.
- No hosted worker system.
- No auth or multi-tenant workspace model.
- No full Multica clone.
- No broad runtime refactor without failing tests.

## Key Review Questions For Claude

1. Should `IssueStatus.FAILED` be added now, or should failed terminal state be
   derived from taskruns until a later schema migration?
2. Should `POST /api/issues` always detach, or only detach for real providers
   (`codex` / `claude-code`) while keeping dry-run synchronous?
3. Where should the background runner live in this codebase: FastAPI
   `BackgroundTasks`, a daemon thread owned by API process, or an explicit
   `ariadne daemon-start` process that the UI expects to be running?
4. What is the right heartbeat interval for local-first UX: 5s, 10s, or
   configurable?
5. Should max real-provider timeout remain 600s by default, or should UI-created
   tasks have a shorter default with an advanced override?

## Bottom Line

deep-010 proved the web UI can drive Ariadne. The "Super Mario" run proved the
next layer that must be fixed:

- long-running real execution must not block the submit request;
- silent provider execution must still produce observable heartbeat events;
- failed member work must never close a squad issue as `done`.

This is the natural deep-011 scope.
