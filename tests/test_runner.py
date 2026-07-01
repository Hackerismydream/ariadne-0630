from typer.testing import CliRunner

from ariadne.cli import app as cli_app
from ariadne.models import IssueStatus, TaskStatus
from ariadne.orchestrator import deterministic_decide
from ariadne.runner import run_intent
from ariadne.store import Store


def test_run_default_creates_independent_issues_and_agents(tmp_path):
    store = Store(str(tmp_path / "run.db"))
    try:
        result = run_intent(
            store,
            ["write hello", "write add"],
            backend="dry-run",
            target_repo=str(tmp_path),
            max_iterations=10,
        )

        assert result.mode == "default"
        assert result.completed is True
        assert len(result.task_results) == 2
        assert {task.status for task in result.task_results} == {"completed"}

        issues = store.list_issues()
        assert len(issues) == 2
        assert len({issue.id for issue in issues}) == 2
        assert len({issue.assignee_id for issue in issues}) == 2
        assert all(issue.assignee_type.value == "agent" for issue in issues)
    finally:
        store.close()


def test_run_named_agent_resolves_or_creates_without_uuid(tmp_path):
    store = Store(str(tmp_path / "run.db"))
    try:
        result = run_intent(
            store,
            ["first task", "second task"],
            backend="dry-run",
            agent_name="Lambda",
            target_repo=str(tmp_path),
            max_concurrent=2,
            max_iterations=10,
        )

        assert result.completed is True
        agents = [agent for agent in store.list_agents() if agent.name == "Lambda"]
        assert len(agents) == 1
        assert {issue.assignee_id for issue in store.list_issues()} == {agents[0].id}
    finally:
        store.close()


def test_run_default_marks_completed_issue_done(tmp_path):
    store = Store(str(tmp_path / "run.db"))
    try:
        result = run_intent(
            store,
            ["write hello"],
            backend="dry-run",
            target_repo=str(tmp_path),
            max_iterations=10,
        )

        assert result.completed is True
        assert result.issue_id is not None
        issue = store.get_issue(result.issue_id)
        assert issue is not None
        assert issue.status == IssueStatus.DONE
    finally:
        store.close()


def test_run_detach_creates_taskrun_without_starting_daemon(tmp_path):
    store = Store(str(tmp_path / "run.db"))
    try:
        result = run_intent(
            store,
            ["queued only"],
            backend="dry-run",
            target_repo=str(tmp_path),
            detach=True,
        )

        assert result.detached is True
        assert result.completed is False
        assert result.task_results[0].status == "queued"
        taskrun = store.get_taskrun(result.task_results[0].taskrun_id)
        assert taskrun is not None
        assert taskrun.status == TaskStatus.QUEUED
        assert store.list_runtime_machines() == []
    finally:
        store.close()


def test_run_squad_reuses_orchestrator_and_marks_issue_done(tmp_path):
    store = Store(str(tmp_path / "run.db"))
    try:
        result = run_intent(
            store,
            ["refactor this module"],
            backend="dry-run",
            squad=True,
            target_repo=str(tmp_path),
            max_iterations=10,
            llm_decide=deterministic_decide,
        )

        assert result.mode == "squad"
        assert result.completed is True
        assert result.issue_id is not None
        issue = store.get_issue(result.issue_id)
        assert issue is not None
        assert issue.status == IssueStatus.DONE
        decisions = store.list_leader_decisions(result.issue_id)
        assert [decision.outcome.value for decision in decisions] == ["action", "done"]
        assert len(store.list_taskruns_for_issue(result.issue_id)) == 3
    finally:
        store.close()


def test_cli_run_command_prints_completed_results(tmp_path):
    db_path = tmp_path / "run.db"
    runner = CliRunner()

    result = runner.invoke(
        cli_app,
        [
            "run",
            "write hello",
            "write add",
            "--backend",
            "dry-run",
            "--target-repo",
            str(tmp_path),
        ],
        env={"ARIADNE_DB": str(db_path)},
    )

    assert result.exit_code == 0
    assert "Ariadne run complete mode=default" in result.stdout
    assert result.stdout.count("[completed]") == 2
