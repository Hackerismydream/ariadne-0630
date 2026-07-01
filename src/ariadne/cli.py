"""CLI entry point for Ariadne.

Minimal commands for Phase 1: issue/agent/squad CRUD + daemon control.
Per docs/plan/tasks/core-003.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from ariadne.backends import get_backend
from ariadne.daemon import Daemon
from ariadne.models import AssigneeType
from ariadne.store import Store

app = typer.Typer(help="Ariadne: local multi-agent orchestration platform.")

# Shared store instance
_db_path = os.environ.get("ARIADNE_DB", "ariadne.db")


def _get_store() -> Store:
    return Store(os.environ.get("ARIADNE_DB", _db_path))


# ---------------------------------------------------------------------------
# Runtime commands
# ---------------------------------------------------------------------------


@app.command()
def runtime_list():
    """List registered RuntimeMachines."""
    store = _get_store()
    machines = store.list_runtime_machines()
    if not machines:
        typer.echo("No runtime machines.")
        store.close()
        return
    for machine in machines:
        heartbeat = (
            machine.last_heartbeat_at.isoformat()
            if machine.last_heartbeat_at
            else "never"
        )
        typer.echo(
            f"  {machine.id}  [{machine.status.value}]  "
            f"root={machine.workspace_root}  heartbeat={heartbeat}"
        )
    store.close()


@app.command()
def capability_list(
    runtime_machine_id: str | None = typer.Option(None, "--runtime-machine-id"),
):
    """List RuntimeCapabilities."""
    store = _get_store()
    capabilities = store.list_runtime_capabilities(runtime_machine_id)
    if not capabilities:
        typer.echo("No runtime capabilities.")
        store.close()
        return
    for capability in capabilities:
        health = f"  error={capability.health_error}" if capability.health_error else ""
        typer.echo(
            f"  {capability.id}  {capability.provider}  "
            f"[{capability.status.value}]  runtime={capability.runtime_machine_id}"
            f"{health}"
        )
    store.close()


@app.command()
def runtime_lease_list(
    taskrun_id: str | None = typer.Option(None, "--taskrun-id"),
):
    """List RuntimeLeases."""
    store = _get_store()
    leases = store.list_runtime_leases(taskrun_id)
    if not leases:
        typer.echo("No runtime leases.")
        store.close()
        return
    for lease in leases:
        typer.echo(
            f"  {lease.id}  [{lease.status.value}]  taskrun={lease.taskrun_id}  "
            f"runtime={lease.runtime_machine_id}  capability={lease.runtime_capability_id}"
        )
    store.close()


@app.command()
def leader_decision_list(
    issue_id: str | None = typer.Argument(None),
):
    """List LeaderDecision records."""
    store = _get_store()
    decisions = store.list_leader_decisions(issue_id)
    if not decisions:
        typer.echo("No leader decisions.")
        store.close()
        return
    for decision in decisions:
        typer.echo(
            f"  {decision.id}  [{decision.outcome.value}]  issue={decision.issue_id}  "
            f"created_taskruns={decision.created_taskrun_ids}  reason={decision.reason}"
        )
    store.close()


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
def issue_timeline(
    issue_id: str = typer.Argument(...),
):
    """Show IssueTimeline events for an issue."""
    store = _get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        typer.echo(f"Issue not found: {issue_id}", err=True)
        raise typer.Exit(1)
    events = store.get_issue_timeline(issue_id)
    if not events:
        typer.echo("No issue timeline events.")
        store.close()
        return
    typer.echo(f"IssueTimeline for {issue_id}:")
    for event in events:
        target = f" taskrun={event.taskrun_id}" if event.taskrun_id else ""
        typer.echo(
            f"  {event.created_at.isoformat()}  "
            f"[{event.event_type}] actor={event.actor_type}{target}"
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
def taskrun_create(
    issue_id: str = typer.Argument(...),
    handoff_prompt: str = typer.Option("", "--handoff", "-h", help="Handoff prompt for the agent"),
):
    """Enqueue a TaskRun for an issue's assignee."""
    store = _get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        typer.echo(f"Issue not found: {issue_id}", err=True)
        raise typer.Exit(1)
    taskrun = store.enqueue_taskrun(
        issue.id,
        issue.assignee_id,
        handoff_prompt=handoff_prompt or None,
    )
    typer.echo(f"Created taskrun: {taskrun.id} (status={taskrun.status.value})")
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
def taskrun_list():
    """List all TaskRuns."""
    store = _get_store()
    taskruns = store.list_taskruns()
    if not taskruns:
        typer.echo("No taskruns.")
        return
    for taskrun in taskruns:
        typer.echo(
            f"  {taskrun.id}  [{taskrun.status.value}]  "
            f"issue={taskrun.issue_id}  agent_profile={taskrun.agent_profile_id}  "
            f"attempt={taskrun.attempt}"
        )
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


@app.command()
def taskrun_timeline(
    taskrun_id: str = typer.Argument(...),
):
    """Show timeline events for a TaskRun's trace_id."""
    store = _get_store()
    taskrun = store.get_taskrun(taskrun_id)
    if taskrun is None:
        typer.echo(f"TaskRun not found: {taskrun_id}", err=True)
        raise typer.Exit(1)
    if not taskrun.trace_id:
        typer.echo("No trace_id for this TaskRun.")
        store.close()
        return
    events = store.get_timeline(taskrun.trace_id)
    if not events:
        typer.echo("No events recorded.")
        store.close()
        return
    typer.echo(f"Timeline for TaskRun trace {taskrun.trace_id}:")
    for e in events:
        details_str = f"  {e['details']}" if e["details"] else ""
        typer.echo(
            f"  {e['created_at']}  [{e['event']}]  "
            f"taskrun={e['task_id']}{details_str}"
        )
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


@app.command()
def agent_profile_create(
    name: str = typer.Option(..., "--name", "-n"),
    description: str = typer.Option("", "--description"),
    instructions: str = typer.Option("", "--instructions"),
    capability: list[str] = typer.Option([], "--capability", "-c"),
    runtime_policy: str = typer.Option("{}", "--runtime-policy"),
    max_concurrent_taskruns: int = typer.Option(1, "--max-concurrent-taskruns"),
):
    """Create a durable AgentProfile and compatibility Agent."""
    try:
        policy = json.loads(runtime_policy)
    except json.JSONDecodeError as exc:
        typer.echo(f"Invalid runtime policy JSON: {exc}", err=True)
        raise typer.Exit(1) from exc
    store = _get_store()
    profile = store.create_agent_profile(
        name=name,
        description=description,
        instructions=instructions,
        preferred_capabilities=capability or ["dry-run"],
        runtime_policy=policy,
        max_concurrent_taskruns=max_concurrent_taskruns,
    )
    typer.echo(f"Created agent profile: {profile.id} (name={profile.name})")
    store.close()


@app.command()
def agent_profile_list():
    """List AgentProfiles with bound Skills."""
    store = _get_store()
    profiles = store.list_agent_profiles()
    if not profiles:
        typer.echo("No agent profiles.")
        store.close()
        return
    for profile in profiles:
        skills = [skill.name for skill in store.list_skills_for_agent_profile(profile.id)]
        typer.echo(
            f"  {profile.id}  {profile.name}  "
            f"capabilities={profile.preferred_capabilities}  skills={skills}"
        )
    store.close()


@app.command()
def skill_create(
    name: str = typer.Option(..., "--name", "-n"),
    description: str = typer.Option("", "--description"),
    when_to_use: str = typer.Option("", "--when-to-use"),
    prompt_snippet: str = typer.Option("", "--prompt-snippet"),
    tool: list[str] = typer.Option([], "--tool"),
    test_command: str = typer.Option("", "--test-command"),
    source_path: str = typer.Option("", "--source-path"),
    version: str = typer.Option("", "--version"),
):
    """Create a first-class Skill."""
    store = _get_store()
    skill = store.create_skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        prompt_snippet=prompt_snippet,
        tools_allowed=tool,
        test_command=test_command or None,
        source_path=source_path or None,
        version=version,
    )
    typer.echo(f"Created skill: {skill.id} (name={skill.name})")
    store.close()


@app.command()
def skill_list():
    """List first-class Skills."""
    store = _get_store()
    skills = store.list_skills()
    if not skills:
        typer.echo("No skills.")
        store.close()
        return
    for skill in skills:
        typer.echo(
            f"  {skill.id}  {skill.name}  tools={skill.tools_allowed}  "
            f"version={skill.version}"
        )
    store.close()


@app.command()
def agent_profile_bind_skill(
    agent_profile_id: str = typer.Argument(...),
    skill_id_or_name: str = typer.Argument(...),
):
    """Bind a Skill to an AgentProfile."""
    store = _get_store()
    try:
        skill = store.bind_skill_to_agent_profile(agent_profile_id, skill_id_or_name)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        store.close()
        raise typer.Exit(1) from exc
    typer.echo(f"Bound skill {skill.id} to agent profile {agent_profile_id}")
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


@app.command()
def benchmark_list():
    """List persisted BenchmarkRuns."""
    store = _get_store()
    runs = store.list_benchmark_runs()
    if not runs:
        typer.echo("No benchmark runs.")
        store.close()
        return
    for run in runs:
        success = run.summary.get("success")
        typer.echo(
            f"  {run.id}  [{run.status}]  {run.suite_name}/{run.case_name}  "
            f"issue={run.issue_id}  success={success}"
        )
    store.close()


@app.command()
def demo_v1(
    output_dir: str = typer.Option(".ariadne-demo-v1", "--output-dir"),
    reset: bool = typer.Option(False, "--reset"),
):
    """Run the five-minute local Managed Agent Team Runtime v1 demo."""
    from ariadne.eval import BenchmarkTask, run_benchmark
    from ariadne.models import DelegationDecision, LeaderDecision, LeaderDecisionOutcome
    from ariadne.orchestrator import Orchestrator

    demo_dir = Path(output_dir)
    demo_dir.mkdir(parents=True, exist_ok=True)
    db_path = demo_dir / "ariadne-v1.db"
    if reset and db_path.exists():
        db_path.unlink()

    store = Store(str(db_path))
    try:
        leader = store.create_agent_profile(
            name="Demo Leader",
            instructions="Coordinate the local demo and close only after member facts exist.",
            preferred_capabilities=["dry-run"],
            runtime_policy={"allow_real_execution": False},
        )
        coder = store.create_agent_profile(
            name="Demo Coder",
            instructions="Execute dry-run implementation work for the demo.",
            preferred_capabilities=["dry-run"],
            runtime_policy={"allow_real_execution": False},
        )
        skill = store.create_skill(
            name=f"demo-dry-run-skill-{len(store.list_skills()) + 1}",
            description="Dry-run demo skill",
            when_to_use="Use for the clean-checkout demo.",
            prompt_snippet="Report dry-run facts without touching external providers.",
            tools_allowed=["dry-run"],
            test_command="uv run pytest -q",
            source_path="demo-v1",
            version="1",
        )
        store.bind_skill_to_agent_profile(coder.id, skill.id)
        squad = store.create_squad(
            "Demo Runtime Squad",
            leader.id,
            instructions="Delegate once, then mark done after member completion.",
        )
        store.add_squad_member(squad.id, coder.id, role="coder")
        issue = store.create_issue(
            "Demo managed-agent runtime",
            "Create observable dry-run runtime facts for v1.",
            AssigneeType.SQUAD,
            squad.id,
        )
        store.enqueue_taskrun(issue.id, leader.id, squad_id=squad.id)

        call_count = [0]

        def decide(briefing, issue, completed_results=None):
            call_count[0] += 1
            if not completed_results:
                entry = briefing.roster[0]
                return DelegationDecision(
                    target_agent_id=entry.agent_id,
                    backend="dry-run",
                    handoff_prompt="Run the local v1 demo path in dry-run mode.",
                    reason="demo delegation",
                    skill_refs=entry.skills,
                )
            return LeaderDecision(
                outcome=LeaderDecisionOutcome.DONE,
                reason="demo member task completed with observable facts",
            )

        daemon = Daemon(
            store=store,
            backend_factory=get_backend,
            runtime_id="demo-v1",
            poll_interval=0.001,
            orchestrator=Orchestrator(store=store, llm_decide=decide),
            target_repo_path=str(demo_dir),
        )
        daemon.start(max_iterations=10)

        benchmark = run_benchmark(
            store,
            [
                BenchmarkTask(
                    title="Demo Benchmark",
                    description="BenchmarkRun from demo product facts.",
                    backend="dry-run",
                    expected_success=True,
                    suite_name="demo-v1",
                )
            ],
        )

        issue = store.get_issue(issue.id)
        taskruns = store.list_taskruns()
        leases = store.list_runtime_leases()
        decisions = store.list_leader_decisions()
        benchmark_runs = store.list_benchmark_runs()

        typer.echo("Ariadne Managed Agent Team Runtime v1 demo complete")
        typer.echo(f"DB: {db_path}")
        typer.echo(f"Issue: {issue.id} status={issue.status.value}")
        typer.echo(f"RuntimeMachines: {len(store.list_runtime_machines())}")
        typer.echo(f"RuntimeCapabilities: {len(store.list_runtime_capabilities())}")
        typer.echo(f"TaskRuns: {len(taskruns)}")
        typer.echo(f"RuntimeLeases: {len(leases)}")
        typer.echo(f"LeaderDecisions: {len(decisions)}")
        typer.echo(f"BenchmarkRuns: {len(benchmark_runs)}")
        typer.echo(f"Benchmark success: {benchmark.success_count}/{benchmark.total_tasks}")
        typer.echo("States: dry-run=completed, live-execution=skipped, blocked=0, failed=0")
        typer.echo("")
        typer.echo("Inspect with:")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne runtime-list")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne capability-list")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne taskrun-list")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne runtime-lease-list")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne leader-decision-list {issue.id}")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne issue-timeline {issue.id}")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne benchmark-list")
        typer.echo(f"  ARIADNE_DB={db_path} uv run ariadne api-serve")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Squad run command
# ---------------------------------------------------------------------------


@app.command()
def squad_run(
    target_repo: str = typer.Option(..., "--target-repo", help="Target repo path"),
    confirm_execution: bool = typer.Option(False, "--confirm-execution", help="Enable real backend execution"),
    title: str = typer.Option("Multi-step feature implementation", "--title"),
    description: str = typer.Option("Add a string_utils module with capitalize and reverse functions, plus tests", "--description"),
    poll_interval: float = typer.Option(2.0, "--poll-interval"),
    max_iterations: int = typer.Option(20, "--max-iterations"),
):
    """Create a squad with leader + 2 members and run the full orchestration loop."""
    if confirm_execution:
        os.environ["ARIADNE_ENABLE_EXTERNAL_EXECUTION"] = "1"

    from ariadne.daemon import Daemon
    from ariadne.llm_decide import make_llm_decide
    from ariadne.orchestrator import Orchestrator

    store = _get_store()

    # Create agents
    leader = store.create_agent("Leader", "Coordinate the squad", ["dry-run"], ["planning"])
    coder = store.create_agent("Coder", "Write code", ["codex"], ["python"])
    tester = store.create_agent("Tester", "Write tests", ["claude-code"], ["testing"])

    # Create squad
    squad = store.create_squad("Dev Squad", leader.id, instructions="Implement features step by step")
    store.add_squad_member(squad.id, coder.id, role="coder")
    store.add_squad_member(squad.id, tester.id, role="tester")

    # Create issue + leader task
    issue = store.create_issue(title, description, AssigneeType.SQUAD, squad.id)
    store.enqueue_task(issue.id, leader.id, squad_id=squad.id)

    typer.echo(f"Squad: {squad.id} (leader={leader.name}, members=[{coder.name}, {tester.name}])")
    typer.echo(f"Issue: {issue.id} — {title}")
    typer.echo(f"Target repo: {target_repo}")
    if confirm_execution:
        typer.echo("⚠️  real execution ENABLED")
    typer.echo("Starting orchestration loop...")

    # Wire orchestrator
    decide_fn = make_llm_decide()
    orc = Orchestrator(store=store, llm_decide=decide_fn)
    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        poll_interval=poll_interval,
        orchestrator=orc,
        target_repo_path=target_repo,
    )
    daemon.start(max_iterations=max_iterations)
    store.close()

    # Print timeline

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(_db_path)
    conn.row_factory = _sqlite3.Row
    trace = conn.execute("SELECT trace_id FROM task WHERE issue_id = ? LIMIT 1", (issue.id,)).fetchone()
    if trace:
        events = conn.execute("SELECT * FROM activity_log WHERE trace_id = ? ORDER BY created_at", (trace["trace_id"],)).fetchall()
        typer.echo(f"\n=== Timeline (trace={trace['trace_id']}) ===")
        for e in events:
            typer.echo(f"  {e['created_at']}  [{e['event']}]  task={e['task_id']}")
    conn.close()


@app.command()
def api_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8766, "--port"),
):
    """Start the FastAPI dashboard server."""
    try:
        import uvicorn
    except ImportError:
        typer.echo("uvicorn not installed. Run: uv add uvicorn", err=True)
        raise typer.Exit(1)

    from ariadne.api import app as api_app

    typer.echo(f"Starting dashboard at http://{host}:{port}")
    uvicorn.run(api_app, host=host, port=port)


if __name__ == "__main__":
    app()
