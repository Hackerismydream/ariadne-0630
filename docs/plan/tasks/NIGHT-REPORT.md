# NIGHT-REPORT

## Structure Design Before Implementation

### Phase 1 Frontend Landing

- Create a standalone `frontend/` Next.js App Router project. It owns its own
  `package.json`, TypeScript config, Tailwind config, tests, and build scripts.
  No Python package files import frontend code.
- Keep the visual system in `frontend/app/globals.css`: phosphor terminal
  tokens, zero radius, CRT scanline overlay, monospace typography, focus and
  reduced-motion rules.
- Keep API contracts and fetch/EventSource helpers in `frontend/lib/`. These
  are hand-aligned to the HTTP JSON contract from deep-009; no Python import or
  generated shared types.
- Implement reusable terminal primitives in `frontend/components/`:
  `StatusBadge`, `AsciiProgress`, `Pane`, `ShellPrompt`, `Transcript`, and
  issue/task-specific composed panels.
- Implement the three required surfaces:
  `/` issue list with New Task modal, `/issues/[id]` issue detail with SSE
  transcript and diff, and the New Task flow as a modal rather than a route.
- Frontend tests cover pure status/progress/transcript formatting first; browser
  smoke covers the full HTTP/SSE workflow after the app builds.

### Known Integration Risk

- Current `POST /api/issues` is synchronous by design. The frontend will show
  persisted SSE events as a replay/live transcript on the detail page. If
  browser integration proves this is insufficient for real in-flight progress,
  any backend change must be introduced with a failing test first and stay within
  the local-first no-Redis/no-WebSocket boundary.

## Phase 1/2 Result: Frontend + Dry-Run Integration

Implemented `frontend/` as a standalone Next.js App Router project:

- `/`: issue list, active taskrun count, SSE-driven refresh, and New Task modal.
- New Task modal: shell-style prompt, backend selector, direct/squad selector,
  and `POST /api/issues` submit.
- `/issues/[id]`: aggregated issue snapshot, taskruns, SSE transcript, and
  changed-files/diff pane.

The frontend stays HTTP-only. It imports no Python code and reads no backend
files. API contracts live in `frontend/lib/types.ts`; calls live in
`frontend/lib/api.ts`.

Browser smoke was run against:

```bash
ARIADNE_DB=/tmp/ariadne-night-frontend.db uv run ariadne api-serve --host 127.0.0.1 --port 8000
cd frontend && npm run dev -- --hostname localhost --port 3000
cd frontend && npm run test:e2e -- --reporter=line
```

Result: dry-run flow passes end to end. The browser creates an issue through
`POST /api/issues`, navigates to `/issues/{id}`, sees `[DONE]`, receives
persisted SSE transcript events, and renders the diff/changed-files pane. The
latest visual smoke screenshot is `/tmp/ariadne-detail.png`.

## Bugs Found And Fixed

### 1. SSE Stream Used A Sync Generator

Reproduction: an EventSource connection to `/api/events` kept a synchronous
FastAPI streaming generator alive in AnyIO's threadpool. Under repeated browser
smoke runs, long-lived SSE clients could occupy threadpool capacity and make
normal API requests wait behind stream iterators.

Fix: changed `_event_stream()` in `src/ariadne/api.py` to an async generator and
replaced `time.sleep()` with `await asyncio.sleep()`.

Proof:

```bash
uv run pytest tests/test_api.py -q
# 14 passed
```

New regression: `test_events_stream_is_async_to_avoid_threadpool_starvation`.

### 2. Direct `run_intent()` Completed Work But Left Issue In `backlog`

Reproduction: `run_intent(..., backend="dry-run")` completed the taskrun, but
the issue stayed `backlog`, so the frontend detail page showed `[BACKLOG]` after
successful execution.

Fix: after explicit task execution, `runner.py` now marks issues with completed
taskruns as `IssueStatus.DONE`.

Proof:

```bash
uv run pytest tests/test_runner.py tests/test_api.py -q
# 20 passed
```

New regression: `test_run_default_marks_completed_issue_done`.

### 3. Detail Transcript Could Show Unrelated Activity

Reproduction: the issue detail page opened SSE before its initial issue snapshot
was loaded, so it had no taskrun id filter yet. Old activity events from other
issues could appear in the transcript.

Fix: the detail page opens EventSource only after the initial issue snapshot is
loaded, filters issue timeline events by `issue_id`, and filters activity events
by known `taskrun_id`.

Proof:

```bash
cd frontend && npm run test:e2e -- --reporter=line
# 1 passed
```

## Verification Snapshot

```bash
uv run pytest -q
# 215 passed in 4.96s

uv run ruff check src/ariadne/
# All checks passed!

cd frontend && npm test
# 3 passed

cd frontend && npm run build
# Compiled successfully

cd frontend && npm run test:e2e -- --reporter=line
# 1 passed
```

`npm audit --audit-level=moderate` reports two moderate advisories through
Next's nested PostCSS dependency. The suggested fix requires `npm audit
fix --force` and would install a breaking/unacceptable Next version, so this is
recorded as a dependency follow-up rather than forced in this slice.

## Still Not Done

- Real `codex`/`claude` backend validation has not been run yet. The current
  proof is dry-run only.
- No real backend performance numbers were produced. No numbers are estimated.
- True in-flight execution streaming is still bounded by the synchronous
  `POST /api/issues`/`run_intent()` model. The current frontend shows persisted
  SSE replay/live table polling events from deep-009, not a background job queue
  execution stream.
