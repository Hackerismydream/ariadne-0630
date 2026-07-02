"""Tests for FastAPI API + dashboard.

Per docs/plan/tasks/deep-004.md.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from ariadne import api
from ariadne.api import app
from ariadne.models import AssigneeType, IssueStatus
from ariadne.runner import RunResult, RunTaskResult
from ariadne.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("ariadne.api._db_path", db)
    s = Store(db)
    # Seed data
    agent = s.create_agent("TestAgent", "do things", ["dry-run"], ["python"])
    issue = s.create_issue("Test Issue", "description", AssigneeType.AGENT, agent.id)
    task = s.enqueue_task(issue.id, agent.id)
    s.claim_task(agent.id, "rt-1")
    s.start_task(task.id)
    s.complete_task(task.id, {"output": "done"})
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(app)


def test_dashboard_returns_html(client):
    """GET / returns HTML"""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Ariadne" in res.text


def test_list_issues(client):
    """GET /api/issues returns issue list"""
    res = client.get("/api/issues")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert data[0]["title"] == "Test Issue"
    assert data[0]["status"] == "backlog"


def test_list_tasks(client):
    """GET /api/tasks returns task list with trace_id"""
    res = client.get("/api/tasks")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert "trace_id" in data[0]
    assert data[0]["trace_id"] is not None
    assert data[0]["status"] == "completed"


def test_task_timeline(client):
    """GET /api/tasks/{id}/timeline returns activity events"""
    # Get a task id
    tasks = client.get("/api/tasks").json()
    task_id = tasks[0]["id"]

    res = client.get(f"/api/tasks/{task_id}/timeline")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert "event" in data[0]
    assert "created_at" in data[0]


def test_task_timeline_not_found(client):
    """GET /api/tasks/nonexistent/timeline → 404"""
    res = client.get("/api/tasks/nonexistent/timeline")
    assert res.status_code == 404


def test_list_agents(client):
    """GET /api/agents returns agent list"""
    res = client.get("/api/agents")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert data[0]["name"] == "TestAgent"
    assert data[0]["backends"] == ["dry-run"]


def test_api_uses_ariadne_db_environment(tmp_path, monkeypatch):
    """ARIADNE_DB selects the dashboard database."""
    db = str(tmp_path / "env.db")
    monkeypatch.setenv("ARIADNE_DB", db)
    s = Store(db)
    try:
        agent = s.create_agent("EnvAgent", "", ["dry-run"], [])
        s.create_issue("Env Issue", "", AssigneeType.AGENT, agent.id)
    finally:
        s.close()

    res = TestClient(app).get("/api/issues")

    assert res.status_code == 200
    assert res.json()[0]["title"] == "Env Issue"


def test_create_issue_runs_intent_and_returns_run_result(tmp_path, monkeypatch):
    db = str(tmp_path / "run-api.db")
    monkeypatch.setattr("ariadne.api._db_path", db)

    res = TestClient(app).post(
        "/api/issues",
        json={
            "title": "Write hello",
            "description": "Create a hello helper",
            "backend": "dry-run",
            "mode": "direct",
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["mode"] == "default"
    assert data["backend"] == "dry-run"
    assert data["detached"] is False
    assert data["completed"] is True
    assert len(data["task_results"]) == 1
    assert data["issue_id"] == data["task_results"][0]["issue_id"]
    assert data["task_results"][0]["status"] == "completed"
    assert "diff" in data["task_results"][0]
    assert "changed_files" in data["task_results"][0]


def test_post_issue_real_backend_detaches_without_blocking(monkeypatch):
    captured = {}

    def fake_run_intent(*args, **kwargs):
        captured.update(kwargs)
        return RunResult(
            mode="default",
            detached=kwargs["detach"],
            completed=False,
            runtime_id="ariadne-run",
            target_repo=kwargs["target_repo"],
            issue_id="issue-detached",
            task_results=[
                RunTaskResult(
                    title="Use codex",
                    issue_id="issue-detached",
                    taskrun_id="taskrun-detached",
                    agent_id="agent-codex",
                    agent_name="Codex",
                    status="queued",
                )
            ],
        )

    monkeypatch.setattr("ariadne.api.run_intent", fake_run_intent)

    res = TestClient(app).post(
        "/api/issues",
        json={
            "title": "Use codex",
            "description": "Queue real provider work",
            "backend": "codex",
            "mode": "direct",
        },
    )

    assert res.status_code == 202
    assert captured["detach"] is True
    data = res.json()
    assert data["backend"] == "codex"
    assert data["detached"] is True
    assert data["completed"] is False
    assert data["issue_id"] == "issue-detached"
    assert data["task_results"][0]["status"] == "queued"


def test_post_issue_real_backend_queues_taskrun_for_daemon(tmp_path, monkeypatch):
    db = str(tmp_path / "real-detach-api.db")
    monkeypatch.setattr("ariadne.api._db_path", db)

    res = TestClient(app).post(
        "/api/issues",
        json={
            "title": "Use codex",
            "description": "Queue real provider work",
            "backend": "codex",
            "mode": "direct",
            "target_repo": str(tmp_path),
        },
    )

    assert res.status_code == 202
    data = res.json()
    assert data["backend"] == "codex"
    assert data["detached"] is True
    assert data["completed"] is False
    assert data["task_results"][0]["status"] == "queued"

    store = Store(db)
    try:
        taskruns = store.list_taskruns_for_issue(data["issue_id"])
        assert len(taskruns) == 1
        assert taskruns[0].status.value == "queued"
        assert store.list_runtime_machines() == []
    finally:
        store.close()


def test_issue_detail_aggregates_issue_taskruns_and_diff(client):
    issue_id = client.get("/api/issues").json()[0]["id"]

    res = client.get(f"/api/issues/{issue_id}")

    assert res.status_code == 200
    data = res.json()
    assert data["id"] == issue_id
    assert data["taskruns"]
    assert data["taskruns"][0]["issue_id"] == issue_id
    assert data["diff"] is None
    assert data["changed_files"] == []


def test_patch_issue_status_and_assignee(client, store):
    assignee = store.create_agent("PatchAgent", "", ["dry-run"], [])

    issue_id = client.get("/api/issues").json()[0]["id"]
    res = client.patch(
        f"/api/issues/{issue_id}",
        json={
            "status": "todo",
            "assignee_type": "agent",
            "assignee_id": assignee.id,
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == IssueStatus.TODO.value
    assert data["assignee_type"] == "agent"
    assert data["assignee_id"] == assignee.id


def test_patch_issue_status_failed(client):
    issue_id = client.get("/api/issues").json()[0]["id"]

    res = client.patch(
        f"/api/issues/{issue_id}",
        json={"status": "failed"},
    )

    assert res.status_code == 200
    assert res.json()["status"] == IssueStatus.FAILED.value


def test_issue_taskruns_endpoint_returns_execution_records(client):
    issue_id = client.get("/api/issues").json()[0]["id"]

    res = client.get(f"/api/issues/{issue_id}/taskruns")

    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["issue_id"] == issue_id
    assert data[0]["status"] == "completed"
    assert "duration_seconds" in data[0]


def test_events_sse_streams_issue_timeline_events(client):
    with client.stream("GET", "/api/events?limit=1&poll_interval=0") as res:
        body = next(res.iter_text())

    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    assert "event: issue_timeline" in body
    assert '"event_type":"issue_created"' in body


def test_events_stream_is_async_to_avoid_threadpool_starvation():
    stream = api._event_stream(limit=1, poll_interval=0)

    assert inspect.isasyncgen(stream)


def test_cors_allows_localhost_nextjs(tmp_path, monkeypatch):
    db = str(tmp_path / "cors.db")
    monkeypatch.setattr("ariadne.api._db_path", db)

    res = TestClient(app).options(
        "/api/issues",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:3000"
