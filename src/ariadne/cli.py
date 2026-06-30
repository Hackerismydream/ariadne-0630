"""CLI entry point for Ariadne.

Minimal commands for Phase 1: issue/agent/squad CRUD + daemon control.
Per docs/plan/tasks/core-003.md.
"""

from __future__ import annotations

import os

import typer

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne.models import AssigneeType
from ariadne.store import Store

app = typer.Typer(help="Ariadne: local multi-agent orchestration platform.")

# Shared store instance
_db_path = "ariadne.db"


def _get_store() -> Store:
    return Store(_db_path)


# ---------------------------------------------------------------------------
# Issue commands
# ---------------------------------------------------------------------------


@app.command()
def issue_create(
    title: str = typer.Option(..., "--title", "-t"),
    assignee_type: str = typer.Option("agent", "--assignee-type"),
    assignee_id: str = typer.Option(..., "--assignee-id"),
    description: str = typer.Option("", "--description", "-d"),
):
    """Create a new issue."""
    store = _get_store()
    issue = store.create_issue(
        title=title,
        description=description,
        assignee_type=AssigneeType(assignee_type),
        assignee_id=assignee_id,
    )
    typer.echo(f"Created issue: {issue.id} (status={issue.status.value})")
    store.close()


@app.command()
def issue_list():
    """List all issues."""
    store = _get_store()
    issues = store.list_issues()
    if not issues:
        typer.echo("No issues.")
        return
    for issue in issues:
        typer.echo(
            f"  {issue.id}  [{issue.status.value}]  {issue.title}  "
            f"→ {issue.assignee_type.value}:{issue.assignee_id}"
        )
    store.close()


@app.command()
def task_create(
    issue_id: str = typer.Argument(...),
    handoff_prompt: str = typer.Option("", "--handoff", "-h", help="Handoff prompt for the agent"),
):
    """Enqueue a task for an issue's assignee."""
    store = _get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        typer.echo(f"Issue not found: {issue_id}", err=True)
        raise typer.Exit(1)
    task = store.enqueue_task(issue.id, issue.assignee_id, handoff_prompt=handoff_prompt or None)
    typer.echo(f"Created task: {task.id} (status={task.status.value})")
    store.close()


@app.command()
def task_list():
    """List all tasks."""
    store = _get_store()
    rows = store._conn.execute(
        "SELECT id, issue_id, agent_id, status, attempt FROM task ORDER BY created_at"
    ).fetchall()
    if not rows:
        typer.echo("No tasks.")
        return
    for r in rows:
        typer.echo(f"  {r['id']}  [{r['status']}]  issue={r['issue_id']}  attempt={r['attempt']}")
    store.close()


@app.command()
def task_timeline(
    task_id: str = typer.Argument(...),
):
    """Show timeline of events for a task's trace_id."""
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        typer.echo(f"Task not found: {task_id}", err=True)
        raise typer.Exit(1)
    if not task.trace_id:
        typer.echo("No trace_id for this task.")
        store.close()
        return
    events = store.get_timeline(task.trace_id)
    if not events:
        typer.echo("No events recorded.")
        store.close()
        return
    typer.echo(f"Timeline for trace {task.trace_id}:")
    for e in events:
        details_str = f"  {e['details']}" if e["details"] else ""
        typer.echo(f"  {e['created_at']}  [{e['event']}]  task={e['task_id']}{details_str}")
    store.close()


# ---------------------------------------------------------------------------
# Agent commands
# ---------------------------------------------------------------------------


@app.command()
def agent_create(
    name: str = typer.Option(..., "--name", "-n"),
    backend: str = typer.Option("dry-run", "--backend", "-b"),
    instructions: str = typer.Option("", "--instructions"),
    skills: str = typer.Option("", "--skills", help="Comma-separated skill names"),
):
    """Create a new agent profile."""
    store = _get_store()
    skill_list = [s.strip() for s in skills.split(",") if s.strip()] if skills else []
    agent = store.create_agent(
        name=name,
        instructions=instructions,
        backends=[backend],
        skills=skill_list,
    )
    typer.echo(f"Created agent: {agent.id} (name={agent.name}, backends={agent.backends})")
    store.close()


@app.command()
def agent_list():
    """List all agents."""
    store = _get_store()
    agents = store.list_agents()
    if not agents:
        typer.echo("No agents.")
        return
    for agent in agents:
        typer.echo(
            f"  {agent.id}  {agent.name}  backends={agent.backends}  skills={agent.skills}"
        )
    store.close()


# ---------------------------------------------------------------------------
# Squad commands
# ---------------------------------------------------------------------------


@app.command()
def squad_create(
    name: str = typer.Option(..., "--name", "-n"),
    leader_id: str = typer.Option(..., "--leader-id"),
    instructions: str = typer.Option("", "--instructions"),
):
    """Create a new squad."""
    store = _get_store()
    squad = store.create_squad(name=name, leader_id=leader_id, instructions=instructions)
    typer.echo(f"Created squad: {squad.id} (name={squad.name}, leader={squad.leader_id})")
    store.close()


@app.command()
def squad_add_member(
    squad_id: str = typer.Argument(...),
    member_id: str = typer.Argument(...),
    role: str = typer.Option("coder", "--role"),
):
    """Add an agent to a squad."""
    store = _get_store()
    sm = store.add_squad_member(squad_id, member_id, role=role)
    typer.echo(f"Added {sm.member_id} to squad {sm.squad_id} as {sm.role}")
    store.close()


# ---------------------------------------------------------------------------
# Daemon commands
# ---------------------------------------------------------------------------


@app.command()
def daemon_start(
    max_iterations: int = typer.Option(None, "--max-iterations"),
    poll_interval: float = typer.Option(3.0, "--poll-interval"),
    confirm_execution: bool = typer.Option(False, "--confirm-execution", help="Enable real backend execution"),
    target_repo: str = typer.Option(".", "--target-repo", help="Target repo path for execution"),
):
    """Start the daemon poll loop."""
    if confirm_execution:
        os.environ["ARIADNE_ENABLE_EXTERNAL_EXECUTION"] = "1"
    store = _get_store()
    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        poll_interval=poll_interval,
        target_repo_path=target_repo,
    )
    typer.echo(f"Starting daemon (runtime={daemon.runtime_id}, poll={poll_interval}s)")
    if confirm_execution:
        typer.echo("  ⚠️  real execution ENABLED")
    if max_iterations:
        typer.echo(f"  max_iterations={max_iterations}")
    daemon.start(max_iterations=max_iterations)
    store.close()
    typer.echo("Daemon stopped.")


@app.command()
def daemon_status():
    """Show daemon status."""
    store = _get_store()
    try:
        row = store._conn.execute(
            "SELECT value FROM daemon_state WHERE key = 'last_heartbeat'"
        ).fetchone()
        if row:
            typer.echo(f"Last heartbeat: {row['value']}")
        else:
            typer.echo("No heartbeat recorded (daemon may not have started)")
    except Exception:
        typer.echo("No daemon state found (daemon may not have started)")

    for status in ("queued", "claimed", "running", "completed", "failed", "cancelled"):
        count = store._conn.execute(
            "SELECT COUNT(*) as c FROM task WHERE status = ?", (status,)
        ).fetchone()["c"]
        if count:
            typer.echo(f"  tasks {status}: {count}")
    store.close()


# ---------------------------------------------------------------------------
# Benchmark commands
# ---------------------------------------------------------------------------


@app.command()
def benchmark_run(
    iterations: int = typer.Option(5, "--iterations", "-n"),
    backend: str = typer.Option("dry-run", "--backend", "-b"),
):
    """Run benchmark tasks and print report."""
    import json

    from ariadne.eval import BenchmarkTask, report_to_dict, run_benchmark

    store = _get_store()
    tasks = [
        BenchmarkTask(
            title=f"Benchmark task {i+1}",
            description=f"Automated benchmark iteration {i+1} via {backend}",
            backend=backend,
            expected_success=True,
        )
        for i in range(iterations)
    ]
    report = run_benchmark(store, tasks)
    typer.echo(json.dumps(report_to_dict(report), indent=2))
    store.close()


if __name__ == "__main__":
    app()
