# Web Dashboard Layout

Status: Draft

## Design Principles

- Single-page, no routing, no build step — pure HTML + minimal JS
- Served by FastAPI at `GET /` (static HTML file)
- Polls `GET /api/tasks` and `GET /api/issues` every 2s
- Shows real-time task status without WebSocket (simple polling)

## Overall Structure

```
┌──────────────────────────────────────────────────────┐
│  Ariadne Dashboard                          [refresh] │
├──────────────┬───────────────────────────────────────┤
│  Issues      │  Task Timeline                        │
│  ┌────────┐  │  ┌─────────────────────────────────┐  │
│  │ issue1 │  │  │ trace-abc                       │  │
│  │ issue2 │  │  │ ── created  (queued)            │  │
│  │ issue3 │  │  │ ── claimed  (claimed)           │  │
│  └────────┘  │  │ ── started  (running)           │  │
│              │  │ ── progress "starting codex..." │  │
│  Tasks       │  │ ── completed                    │  │
│  ┌────────┐  │  └─────────────────────────────────┘  │
│  │ task1  │  │                                       │
│  │ task2  │  │  Agent Roster                         │
│  │ task3  │  │  ┌─────────────────────────────────┐  │
│  └────────┘  │  │ Coder (codex)     [idle]        │  │
│              │  │ Tester (claude)    [idle]        │  │
│              │  │ Leader             [idle]        │  │
│              │  └─────────────────────────────────┘  │
└──────────────┴───────────────────────────────────────┘
```

## Page-Level Layout

Three columns:
1. **Left**: Issues list + Tasks list (selectable)
2. **Right top**: Task timeline for selected task's trace_id
3. **Right bottom**: Agent roster with live status

## State Variants

| State | Left panel | Right panel |
|-------|-----------|-------------|
| **Empty** | "No issues. Create one via CLI." | "Select a task to see timeline." |
| **Loading** | Skeleton rows | "Loading..." |
| **Running** | Task row shows spinner | Timeline updates live |
| **Error** | Red badge on failed task | Timeline shows failure event |

## API Endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET | `/` | static HTML dashboard |
| GET | `/api/issues` | list of issues |
| GET | `/api/tasks` | list of tasks with trace_id |
| GET | `/api/tasks/{id}/timeline` | activity_log rows for task's trace_id |
| GET | `/api/agents` | list of agents with status |

## Component Tree

```
dashboard.html
├── header (title + refresh button)
├── left-panel
│   ├── issues-section (list)
│   └── tasks-section (list, clickable)
├── right-panel
│   ├── timeline-section (activity_log for selected task)
│   └── roster-section (agents with status)
└── footer (daemon status)
```
