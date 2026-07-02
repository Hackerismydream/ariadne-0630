"""FastAPI control plane for Ariadne.

Optional layer — provides REST API + single-page HTML dashboard.
Per docs/architecture/dashboard-layout.md.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ariadne.models import AssigneeType, IssueStatus, TaskRun
from ariadne.runner import run_intent
from ariadne.store import Store

app = FastAPI(title="Ariadne Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

_db_path = os.environ.get("ARIADNE_DB", "ariadne.db")
_DETACHED_BACKENDS = {"codex", "claude-code"}


def _get_store() -> Store:
    return Store(os.environ.get("ARIADNE_DB", _db_path))


class AgentProfileCreateRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    preferred_capabilities: list[str] = Field(default_factory=list)
    runtime_policy: dict = Field(default_factory=dict)
    max_concurrent_taskruns: int = 1


class SkillCreateRequest(BaseModel):
    name: str
    description: str = ""
    when_to_use: str = ""
    prompt_snippet: str = ""
    tools_allowed: list[str] = Field(default_factory=list)
    test_command: str | None = None
    source_path: str | None = None
    version: str = ""


class IssueCreateRequest(BaseModel):
    title: str
    description: str = ""
    backend: str = "dry-run"
    mode: Literal["direct", "squad"] = "direct"
    agent_name: str | None = None
    detach: bool = False
    target_repo: str = "."
    timeout_seconds: int | None = Field(None, ge=1)


class IssuePatchRequest(BaseModel):
    status: IssueStatus | None = None
    assignee_type: AssigneeType | None = None
    assignee_id: str | None = None


def _agent_profile_payload(store: Store, profile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "instructions": profile.instructions,
        "preferred_capabilities": profile.preferred_capabilities,
        "runtime_policy": profile.runtime_policy,
        "max_concurrent_taskruns": profile.max_concurrent_taskruns,
        "status": profile.status.value,
        "skills": [
            skill.name for skill in store.list_skills_for_agent_profile(profile.id)
        ],
    }


def _skill_payload(skill) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "when_to_use": skill.when_to_use,
        "prompt_snippet": skill.prompt_snippet,
        "tools_allowed": skill.tools_allowed,
        "test_command": skill.test_command,
        "source_path": skill.source_path,
        "version": skill.version,
    }


def _benchmark_run_payload(run) -> dict:
    return {
        "id": run.id,
        "suite_name": run.suite_name,
        "case_name": run.case_name,
        "issue_id": run.issue_id,
        "runtime_policy": run.runtime_policy,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "summary": run.summary,
        "artifact_dir": run.artifact_dir,
        "metrics": run.metrics,
    }


def _runtime_lease_payload(lease) -> dict:
    return {
        "id": lease.id,
        "taskrun_id": lease.taskrun_id,
        "runtime_machine_id": lease.runtime_machine_id,
        "runtime_capability_id": lease.runtime_capability_id,
        "status": lease.status.value,
        "lease_token": lease.lease_token,
        "acquired_at": lease.acquired_at.isoformat(),
        "last_heartbeat_at": lease.last_heartbeat_at.isoformat()
        if lease.last_heartbeat_at
        else None,
        "released_at": lease.released_at.isoformat() if lease.released_at else None,
        "expires_at": lease.expires_at.isoformat(),
        "revoke_reason": lease.revoke_reason,
        "metadata": lease.metadata,
    }


def _leader_decision_payload(decision) -> dict:
    return {
        "id": decision.id,
        "issue_id": decision.issue_id,
        "squad_id": decision.squad_id,
        "leader_task_id": decision.leader_task_id,
        "outcome": decision.outcome.value,
        "reason": decision.reason,
        "delegation_payload": decision.delegation_payload,
        "created_taskrun_ids": decision.created_taskrun_ids,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
    }


def _issue_payload(store: Store, issue) -> dict:
    taskruns = store.list_taskruns_for_issue(issue.id)
    latest_event = _latest_issue_event(store, issue.id)
    active_statuses = {"queued", "preparing", "claimed", "running"}
    return {
        "id": issue.id,
        "title": issue.title,
        "description": issue.description,
        "status": issue.status.value,
        "assignee_type": issue.assignee_type.value,
        "assignee_id": issue.assignee_id,
        "taskrun_count": len(taskruns),
        "active_taskrun_count": sum(
            1 for taskrun in taskruns if taskrun.status.value in active_statuses
        ),
        "latest_event_at": latest_event.created_at.isoformat()
        if latest_event
        else None,
        "created_at": issue.created_at.isoformat(),
    }


def _issue_detail_payload(store: Store, issue) -> dict:
    taskruns = store.list_taskruns_for_issue(issue.id)
    taskrun_payloads = [_taskrun_payload(taskrun) for taskrun in taskruns]
    return {
        **_issue_payload(store, issue),
        "taskruns": taskrun_payloads,
        "diff": _aggregate_diff(taskruns),
        "changed_files": _aggregate_changed_files(taskruns),
    }


def _taskrun_payload(taskrun: TaskRun) -> dict:
    result = taskrun.result or {}
    return {
        "id": taskrun.id,
        "issue_id": taskrun.issue_id,
        "agent_profile_id": taskrun.agent_profile_id,
        "squad_id": taskrun.squad_id,
        "status": taskrun.status.value,
        "attempt": taskrun.attempt,
        "max_attempts": taskrun.max_attempts,
        "parent_taskrun_id": taskrun.parent_taskrun_id,
        "failure_reason": taskrun.failure_reason.value
        if taskrun.failure_reason
        else None,
        "trace_id": taskrun.trace_id,
        "duration_seconds": _taskrun_duration_seconds(taskrun),
        "diff": result.get("diff"),
        "changed_files": list(result.get("changed_files") or []),
        "result": result,
        "error": taskrun.error,
        "created_at": taskrun.created_at.isoformat(),
        "started_at": taskrun.started_at.isoformat() if taskrun.started_at else None,
        "completed_at": taskrun.completed_at.isoformat()
        if taskrun.completed_at
        else None,
    }


def _latest_issue_event(store: Store, issue_id: str):
    timeline = store.get_issue_timeline(issue_id)
    return timeline[-1] if timeline else None


def _aggregate_diff(taskruns: list[TaskRun]) -> str | None:
    diffs = [
        taskrun.result.get("diff")
        for taskrun in taskruns
        if taskrun.result and taskrun.result.get("diff")
    ]
    return "\n".join(diffs) if diffs else None


def _aggregate_changed_files(taskruns: list[TaskRun]) -> list[str]:
    files: list[str] = []
    for taskrun in taskruns:
        if not taskrun.result:
            continue
        for path in taskrun.result.get("changed_files") or []:
            if path not in files:
                files.append(path)
    return files


def _taskrun_duration_seconds(taskrun: TaskRun) -> float | None:
    result = taskrun.result or {}
    result_duration = result.get("duration_seconds")
    if isinstance(result_duration, int | float):
        return float(result_duration)
    if taskrun.started_at and taskrun.completed_at:
        return (taskrun.completed_at - taskrun.started_at).total_seconds()
    return None


def _run_result_payload(result) -> dict:
    return asdict(result)


def _sse_message(event_name: str, payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {event_name}\ndata: {body}\n\n"


async def _event_stream(limit: int | None, poll_interval: float) -> AsyncIterator[str]:
    yielded = 0
    last_issue_cursor: tuple[str, str] | None = None
    last_activity_cursor: tuple[str, str] | None = None
    while limit is None or yielded < limit:
        store = _get_store()
        try:
            issue_events = store.list_issue_timeline_events_after(
                created_at=last_issue_cursor[0] if last_issue_cursor else None,
                event_id=last_issue_cursor[1] if last_issue_cursor else None,
                limit=100,
            )
            activity_events = store.list_activity_events_after(
                created_at=last_activity_cursor[0] if last_activity_cursor else None,
                event_id=last_activity_cursor[1] if last_activity_cursor else None,
                limit=100,
            )
        finally:
            store.close()

        messages: list[tuple[str, dict]] = []
        for event in issue_events:
            last_issue_cursor = (event.created_at.isoformat(), event.id)
            messages.append(
                (
                    "issue_timeline",
                    {
                        "id": event.id,
                        "issue_id": event.issue_id,
                        "event_type": event.event_type,
                        "actor_type": event.actor_type,
                        "actor_id": event.actor_id,
                        "taskrun_id": event.taskrun_id,
                        "runtime_lease_id": event.runtime_lease_id,
                        "leader_decision_id": event.leader_decision_id,
                        "comment_id": event.comment_id,
                        "payload": event.payload,
                        "created_at": event.created_at.isoformat(),
                    },
                )
            )
        for event in activity_events:
            last_activity_cursor = (event["created_at"], event["id"])
            payload = {
                **event,
                "message_type": None,
                "tool_name": None,
                "content": None,
            }
            details = event.get("details") or {}
            if isinstance(details, dict):
                payload["message_type"] = details.get("message_type")
                payload["tool_name"] = details.get("tool_name")
                payload["content"] = details.get("content")
            messages.append(("activity", payload))

        messages.sort(key=lambda item: (item[1]["created_at"], item[1]["id"]))
        if not messages:
            await asyncio.sleep(poll_interval)
            continue
        for event_name, payload in messages:
            yield _sse_message(event_name, payload)
            yielded += 1
            if limit is not None and yielded >= limit:
                return


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
    payload = [_issue_payload(store, issue) for issue in issues]
    store.close()
    return payload


@app.post("/api/issues")
def create_issue(req: IssueCreateRequest):
    """Create an issue and run it through the shared intent runner."""
    store = _get_store()
    task_text = req.description.strip() or req.title
    detach = req.backend in _DETACHED_BACKENDS
    timeout_seconds = req.timeout_seconds or 300
    try:
        result = run_intent(
            store,
            [task_text],
            task_titles=[req.title],
            backend=req.backend,
            squad=req.mode == "squad",
            agent_name=req.agent_name,
            target_repo=req.target_repo,
            detach=detach,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        store.close()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = _run_result_payload(result)
    payload["backend"] = req.backend
    store.close()
    return JSONResponse(
        content=payload,
        status_code=202 if result.detached else 200,
    )


@app.get("/api/issues/{issue_id}")
def get_issue(issue_id: str):
    """Return an issue with its taskruns and aggregate execution result."""
    store = _get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        store.close()
        raise HTTPException(status_code=404, detail="issue not found")
    payload = _issue_detail_payload(store, issue)
    store.close()
    return payload


@app.patch("/api/issues/{issue_id}")
def patch_issue(issue_id: str, req: IssuePatchRequest):
    """Update issue board fields."""
    store = _get_store()
    try:
        issue = store.update_issue(
            issue_id,
            status=req.status,
            assignee_type=req.assignee_type,
            assignee_id=req.assignee_id,
        )
    except KeyError as exc:
        store.close()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = _issue_payload(store, issue)
    store.close()
    return payload


@app.get("/api/issues/{issue_id}/taskruns")
def issue_taskruns(issue_id: str):
    """Return execution records for one issue."""
    store = _get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        store.close()
        raise HTTPException(status_code=404, detail="issue not found")
    taskruns = store.list_taskruns_for_issue(issue_id)
    store.close()
    return [_taskrun_payload(taskrun) for taskrun in taskruns]


@app.get("/api/events")
def events(limit: int | None = None, poll_interval: float = 0.5):
    """Stream issue/taskrun events from persisted local tables."""
    return StreamingResponse(
        _event_stream(limit=limit, poll_interval=poll_interval),
        media_type="text/event-stream",
    )


@app.get("/api/issues/{issue_id}/timeline")
def issue_timeline(issue_id: str):
    """Return IssueTimeline events for an Issue."""
    store = _get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        store.close()
        raise HTTPException(status_code=404, detail="issue not found")
    events = store.get_issue_timeline(issue_id)
    store.close()
    return [
        {
            "id": e.id,
            "issue_id": e.issue_id,
            "event_type": e.event_type,
            "actor_type": e.actor_type,
            "actor_id": e.actor_id,
            "taskrun_id": e.taskrun_id,
            "runtime_lease_id": e.runtime_lease_id,
            "leader_decision_id": e.leader_decision_id,
            "comment_id": e.comment_id,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@app.get("/api/runtime-machines")
def list_runtime_machines():
    """List registered RuntimeMachines."""
    store = _get_store()
    machines = store.list_runtime_machines()
    store.close()
    return [
        {
            "id": m.id,
            "name": m.name,
            "status": m.status.value,
            "version": m.version,
            "device_info": m.device_info,
            "last_heartbeat_at": m.last_heartbeat_at.isoformat()
            if m.last_heartbeat_at
            else None,
            "max_concurrent_taskruns": m.max_concurrent_taskruns,
            "workspace_root": m.workspace_root,
            "repo_allowlist": m.repo_allowlist,
            "metadata": m.metadata,
            "created_at": m.created_at.isoformat(),
            "updated_at": m.updated_at.isoformat(),
        }
        for m in machines
    ]


@app.get("/api/runtime-capabilities")
def list_runtime_capabilities():
    """List RuntimeCapabilities."""
    store = _get_store()
    capabilities = store.list_runtime_capabilities()
    store.close()
    return [
        {
            "id": c.id,
            "runtime_machine_id": c.runtime_machine_id,
            "provider": c.provider,
            "command_path": c.command_path,
            "version": c.version,
            "models": c.models,
            "status": c.status.value,
            "health_error": c.health_error,
            "default_args": c.default_args,
            "metadata": c.metadata,
            "last_checked_at": c.last_checked_at.isoformat()
            if c.last_checked_at
            else None,
        }
        for c in capabilities
    ]


@app.get("/api/runtime-leases")
def list_runtime_leases():
    """List RuntimeLeases."""
    store = _get_store()
    leases = store.list_runtime_leases()
    store.close()
    return [_runtime_lease_payload(lease) for lease in leases]


@app.get("/api/leader-decisions")
def list_leader_decisions():
    """List LeaderDecision records."""
    store = _get_store()
    decisions = store.list_leader_decisions()
    store.close()
    return [_leader_decision_payload(decision) for decision in decisions]


@app.post("/api/agent-profiles")
def create_agent_profile(req: AgentProfileCreateRequest):
    """Create an AgentProfile."""
    store = _get_store()
    profile = store.create_agent_profile(
        name=req.name,
        description=req.description,
        instructions=req.instructions,
        preferred_capabilities=req.preferred_capabilities,
        runtime_policy=req.runtime_policy,
        max_concurrent_taskruns=req.max_concurrent_taskruns,
    )
    payload = _agent_profile_payload(store, profile)
    store.close()
    return payload


@app.get("/api/agent-profiles")
def list_agent_profiles():
    """List AgentProfiles with bound Skill names."""
    store = _get_store()
    profiles = store.list_agent_profiles()
    payload = [_agent_profile_payload(store, profile) for profile in profiles]
    store.close()
    return payload


@app.get("/api/agent-profiles/{agent_profile_id}")
def get_agent_profile(agent_profile_id: str):
    """Inspect one AgentProfile."""
    store = _get_store()
    profile = store.get_agent_profile(agent_profile_id)
    if profile is None:
        store.close()
        raise HTTPException(status_code=404, detail="agent profile not found")
    payload = _agent_profile_payload(store, profile)
    store.close()
    return payload


@app.post("/api/agent-profiles/{agent_profile_id}/skills/{skill_id_or_name}")
def bind_skill_to_agent_profile(agent_profile_id: str, skill_id_or_name: str):
    """Bind a Skill to an AgentProfile."""
    store = _get_store()
    try:
        skill = store.bind_skill_to_agent_profile(agent_profile_id, skill_id_or_name)
    except KeyError as exc:
        store.close()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = _skill_payload(skill)
    store.close()
    return payload


@app.post("/api/skills")
def create_skill(req: SkillCreateRequest):
    """Create a Skill."""
    store = _get_store()
    skill = store.create_skill(
        name=req.name,
        description=req.description,
        when_to_use=req.when_to_use,
        prompt_snippet=req.prompt_snippet,
        tools_allowed=req.tools_allowed,
        test_command=req.test_command,
        source_path=req.source_path,
        version=req.version,
    )
    payload = _skill_payload(skill)
    store.close()
    return payload


@app.get("/api/skills")
def list_skills():
    """List Skills."""
    store = _get_store()
    skills = store.list_skills()
    store.close()
    return [_skill_payload(skill) for skill in skills]


@app.get("/api/skills/{skill_id}")
def get_skill(skill_id: str):
    """Inspect one Skill by id."""
    store = _get_store()
    skill = store.get_skill(skill_id)
    if skill is None:
        store.close()
        raise HTTPException(status_code=404, detail="skill not found")
    payload = _skill_payload(skill)
    store.close()
    return payload


@app.get("/api/benchmark-runs")
def list_benchmark_runs():
    """List BenchmarkRuns."""
    store = _get_store()
    runs = store.list_benchmark_runs()
    store.close()
    return [_benchmark_run_payload(run) for run in runs]


@app.get("/api/benchmark-runs/{benchmark_run_id}")
def get_benchmark_run(benchmark_run_id: str):
    """Inspect one BenchmarkRun."""
    store = _get_store()
    run = store.get_benchmark_run(benchmark_run_id)
    if run is None:
        store.close()
        raise HTTPException(status_code=404, detail="benchmark run not found")
    payload = _benchmark_run_payload(run)
    store.close()
    return payload


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
