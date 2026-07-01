"""FastAPI control plane for Ariadne.

Optional layer — provides REST API + single-page HTML dashboard.
Per docs/architecture/dashboard-layout.md.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ariadne.store import Store

app = FastAPI(title="Ariadne Dashboard")

_db_path = "ariadne.db"


def _get_store() -> Store:
    return Store(_db_path)


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
