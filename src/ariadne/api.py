"""FastAPI control plane for Ariadne.

Optional layer — provides REST API + single-page HTML dashboard.
Per docs/architecture/dashboard-layout.md.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ariadne.store import Store

app = FastAPI(title="Ariadne Dashboard")

_db_path = "ariadne.db"


def _get_store() -> Store:
    return Store(_db_path)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Serve the single-page HTML dashboard."""
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@app.get("/api/issues")
def list_issues():
    """List all issues."""
    store = _get_store()
    issues = store.list_issues()
    store.close()
    return [
        {
            "id": i.id,
            "title": i.title,
            "description": i.description,
            "status": i.status.value,
            "assignee_type": i.assignee_type.value,
            "assignee_id": i.assignee_id,
        }
        for i in issues
    ]


@app.get("/api/tasks")
def list_tasks():
    """List all tasks with trace_id."""
    store = _get_store()
    rows = store._conn.execute(
        "SELECT id, issue_id, agent_id, squad_id, status, attempt, trace_id, created_at FROM task ORDER BY created_at DESC"
    ).fetchall()
    store.close()
    return [
        {
            "id": r["id"],
            "issue_id": r["issue_id"],
            "agent_id": r["agent_id"],
            "squad_id": r["squad_id"],
            "status": r["status"],
            "attempt": r["attempt"],
            "trace_id": r["trace_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/taskruns")
def list_taskruns():
    """List all TaskRuns with v1 naming."""
    store = _get_store()
    taskruns = store.list_taskruns()
    store.close()
    return [
        {
            "id": t.id,
            "issue_id": t.issue_id,
            "agent_profile_id": t.agent_profile_id,
            "squad_id": t.squad_id,
            "status": t.status.value,
            "attempt": t.attempt,
            "trace_id": t.trace_id,
            "created_at": t.created_at.isoformat(),
        }
        for t in taskruns
    ]


@app.get("/api/tasks/{task_id}/timeline")
def task_timeline(task_id: str):
    """Get activity log timeline for a task's trace_id."""
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        store.close()
        raise HTTPException(status_code=404, detail="task not found")
    if not task.trace_id:
        store.close()
        return []
    events = store.get_timeline(task.trace_id)
    store.close()
    return events


@app.get("/api/taskruns/{taskrun_id}/timeline")
def taskrun_timeline(taskrun_id: str):
    """Get activity log timeline for a TaskRun's trace_id."""
    store = _get_store()
    taskrun = store.get_taskrun(taskrun_id)
    if taskrun is None:
        store.close()
        raise HTTPException(status_code=404, detail="taskrun not found")
    if not taskrun.trace_id:
        store.close()
        return []
    events = store.get_timeline(taskrun.trace_id)
    store.close()
    return events


@app.get("/api/agents")
def list_agents():
    """List all agents."""
    store = _get_store()
    agents = store.list_agents()
    store.close()
    return [
        {
            "id": a.id,
            "name": a.name,
            "instructions": a.instructions,
            "backends": a.backends,
            "skills": a.skills,
        }
        for a in agents
    ]
