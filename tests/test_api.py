"""Tests for FastAPI API + dashboard.

Per docs/plan/tasks/deep-004.md.
"""

import pytest
from fastapi.testclient import TestClient

from ariadne.api import app
from ariadne.models import AssigneeType
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
