id: deep-004
scope: ui
status: pending
depends-on: [deep-001, deep-003]
```

## Objective

Add FastAPI control plane + single-page HTML dashboard showing issues, tasks, timeline, and agent roster.

## Context

- Design doc: [docs/architecture/dashboard-layout.md](../../architecture/dashboard-layout.md)

## Path

```
src/ariadne/api.py           # FastAPI app with 4 endpoints + static HTML
src/ariadne/dashboard.html   # single-page dashboard (static)
src/ariadne/cli.py           # add `ariadne api-serve` command
pyproject.toml               # add fastapi + uvicorn dependency
tests/test_api.py            # new
```

## Requirements

1. `pyproject.toml`: add `fastapi>=0.100` and `uvicorn>=0.20` to dependencies
2. `api.py`:
   - `GET /` → serve dashboard.html
   - `GET /api/issues` → list issues
   - `GET /api/tasks` → list tasks with trace_id
   - `GET /api/tasks/{id}/timeline` → activity_log for task's trace_id
   - `GET /api/agents` → list agents
3. `dashboard.html`:
   - Pure HTML + vanilla JS (no build step, no framework)
   - Polls /api/tasks and /api/issues every 2s
   - Left panel: issues list + tasks list (clickable)
   - Right panel: timeline for selected task + agent roster
   - State variants: empty, loading, running, error
4. `cli.py`: `ariadne api-serve --host 127.0.0.1 --port 8766`
5. FastAPI is optional import — `ariadne api-serve` fails with helpful message if fastapi not installed

## Verification

```bash
ruff check src/ariadne/api.py
pytest tests/test_api.py tests/ -v
```

### test_api.py must cover:
- GET / returns HTML
- GET /api/issues returns issue list
- GET /api/tasks returns task list with trace_id
- GET /api/tasks/{id}/timeline returns activity events
- GET /api/agents returns agent list
- Clicking a task updates timeline (integration test)
