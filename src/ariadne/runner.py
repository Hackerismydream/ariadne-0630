"""Intent-level run orchestration shared by CLI and future API routes."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ariadne.backends import ExecutionBackend, get_backend
from ariadne.daemon import Daemon
from ariadne.llm_decide import make_llm_decide
from ariadne.models import (
    Agent,
    AssigneeType,
    DelegationDecision,
    Issue,
    IssueStatus,
    LeaderDecision,
    Squad,
    SquadBriefing,
    TaskRun,
    TaskStatus,
)
from ariadne.orchestrator import Orchestrator
from ariadne.store import Store

BackendFactory = Callable[[str], ExecutionBackend]
LeaderDecide = Callable[
    [SquadBriefing, Issue, list[dict] | None],
    DelegationDecision | LeaderDecision | None,
]

TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


@dataclass(frozen=True)
class RunTaskResult:
    title: str
    issue_id: str
    taskrun_id: str
    agent_id: str
    agent_name: str
    status: str
    duration_seconds: float | None = None
    diff: str | None = None
    changed_files: list[str] = field(default_factory=list)
    stdout: str = ""
    error: str | None = None


@dataclass(frozen=True)
class RunResult:
    mode: str
    detached: bool
    completed: bool
    runtime_id: str
    target_repo: str
    task_results: list[RunTaskResult]
    issue_id: str | None = None
    squad_id: str | None = None
    iterations: int = 0


def run_intent(
    store: Store,
    tasks: list[str],
    *,
    task_titles: list[str] | None = None,
    backend: str = "dry-run",
    squad: bool = False,
    squad_name: str = "Ariadne Run Squad",
    agent_name: str | None = None,
    target_repo: str = ".",
    max_concurrent: int | None = None,
    write_workspace: bool = False,
    detach: bool = False,
    timeout_seconds: int = 600,
    poll_interval: float = 0.001,
    max_iterations: int | None = None,
    runtime_id: str = "ariadne-run",
    backend_factory: BackendFactory = get_backend,
    llm_decide: LeaderDecide | None = None,
) -> RunResult:
    """Run explicit tasks or one squad-led task through existing runtime pieces."""
    clean_tasks = [task.strip() for task in tasks if task.strip()]
    if not clean_tasks:
        raise ValueError("at least one task description is required")
    clean_titles = _clean_task_titles(task_titles, len(clean_tasks))
    target_repo_path = str(Path(target_repo).expanduser().resolve())
    concurrency = max_concurrent or _default_max_concurrent(len(clean_tasks))

    if squad:
        return _run_squad_intent(
            store,
            clean_tasks,
            issue_title=clean_titles[0] if clean_titles else None,
            backend=backend,
            squad_name=squad_name,
            agent_name=agent_name,
            target_repo=target_repo_path,
            max_concurrent=concurrency,
            write_workspace=write_workspace,
            detach=detach,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            max_iterations=max_iterations or 20,
            runtime_id=runtime_id,
            backend_factory=backend_factory,
            llm_decide=llm_decide,
        )

    return _run_explicit_tasks(
        store,
        clean_tasks,
        task_titles=clean_titles,
        backend=backend,
        agent_name=agent_name,
        target_repo=target_repo_path,
        max_concurrent=concurrency,
        write_workspace=write_workspace,
        detach=detach,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        max_iterations=max_iterations or max(10, len(clean_tasks) * 4),
        runtime_id=runtime_id,
        backend_factory=backend_factory,
    )


def _run_explicit_tasks(
    store: Store,
    tasks: list[str],
    *,
    task_titles: list[str] | None,
    backend: str,
    agent_name: str | None,
    target_repo: str,
    max_concurrent: int,
    write_workspace: bool,
    detach: bool,
    timeout_seconds: int,
    poll_interval: float,
    max_iterations: int,
    runtime_id: str,
    backend_factory: BackendFactory,
) -> RunResult:
    taskruns: list[TaskRun] = []
    for index, task_text in enumerate(tasks, start=1):
        issue_title = task_titles[index - 1] if task_titles else task_text
        if agent_name:
            agent = _resolve_or_create_agent(
                store,
                agent_name,
                backend=backend,
                max_concurrent=max_concurrent,
            )
        else:
            agent = _create_agent_profile(
                store,
                f"Run Agent {index}",
                backend=backend,
                max_concurrent=1,
            )
        issue = store.create_issue(
            issue_title,
            task_text,
            AssigneeType.AGENT,
            agent.id,
        )
        taskruns.append(
            store.enqueue_taskrun(
                issue.id,
                agent.id,
                handoff_prompt=task_text,
                timeout_seconds=timeout_seconds,
                target_repo_path=target_repo,
            )
        )

    if detach:
        return _result_for_taskruns(
            store,
            mode="default",
            detached=True,
            completed=False,
            runtime_id=runtime_id,
            target_repo=target_repo,
            taskruns=taskruns,
            issue_id=_single_issue_id(taskruns),
        )

    daemon = _build_daemon(
        store,
        runtime_id=runtime_id,
        target_repo=target_repo,
        max_concurrent=max_concurrent,
        write_workspace=write_workspace,
        poll_interval=poll_interval,
        backend_factory=backend_factory,
    )
    iterations = _drive_daemon_until(
        daemon,
        done=lambda: _all_terminal(store, [taskrun.id for taskrun in taskruns]),
        max_iterations=max_iterations,
    )
    _mark_completed_issues_done(store, taskruns)
    return _result_for_taskruns(
        store,
        mode="default",
        detached=False,
        completed=_all_terminal(store, [taskrun.id for taskrun in taskruns]),
        runtime_id=runtime_id,
        target_repo=target_repo,
        taskruns=taskruns,
        issue_id=_single_issue_id(taskruns),
        iterations=iterations,
    )


def _run_squad_intent(
    store: Store,
    tasks: list[str],
    *,
    issue_title: str | None,
    backend: str,
    squad_name: str,
    agent_name: str | None,
    target_repo: str,
    max_concurrent: int,
    write_workspace: bool,
    detach: bool,
    timeout_seconds: int,
    poll_interval: float,
    max_iterations: int,
    runtime_id: str,
    backend_factory: BackendFactory,
    llm_decide: LeaderDecide | None,
) -> RunResult:
    task_text = "\n\n".join(tasks)
    squad_model = _resolve_or_create_squad(
        store,
        squad_name,
        backend=backend,
        member_name=agent_name,
        max_concurrent=max_concurrent,
    )
    issue = store.create_issue(
        issue_title or _title_for(task_text),
        task_text,
        AssigneeType.SQUAD,
        squad_model.id,
    )
    leader_task = store.enqueue_taskrun(
        issue.id,
        squad_model.leader_id,
        squad_id=squad_model.id,
        handoff_prompt=task_text,
        timeout_seconds=timeout_seconds,
        target_repo_path=target_repo,
    )

    if detach:
        return _result_for_taskruns(
            store,
            mode="squad",
            detached=True,
            completed=False,
            runtime_id=runtime_id,
            target_repo=target_repo,
            taskruns=[leader_task],
            issue_id=issue.id,
            squad_id=squad_model.id,
        )

    orchestrator = Orchestrator(
        store=store,
        llm_decide=llm_decide or make_llm_decide(),
    )
    daemon = _build_daemon(
        store,
        runtime_id=runtime_id,
        target_repo=target_repo,
        max_concurrent=max_concurrent,
        write_workspace=write_workspace,
        poll_interval=poll_interval,
        backend_factory=backend_factory,
        orchestrator=orchestrator,
    )
    iterations = _drive_daemon_until(
        daemon,
        done=lambda: _issue_done(store, issue.id),
        max_iterations=max_iterations,
    )
    return _result_for_taskruns(
        store,
        mode="squad",
        detached=False,
        completed=_issue_done(store, issue.id),
        runtime_id=runtime_id,
        target_repo=target_repo,
        taskruns=store.list_taskruns_for_issue(issue.id),
        issue_id=issue.id,
        squad_id=squad_model.id,
        iterations=iterations,
    )


def _resolve_or_create_agent(
    store: Store,
    name: str,
    *,
    backend: str,
    max_concurrent: int,
) -> Agent:
    agent = store.get_agent_by_name(name)
    if agent is not None:
        return agent
    return _create_agent_profile(
        store,
        name,
        backend=backend,
        max_concurrent=max_concurrent,
    )


def _create_agent_profile(
    store: Store,
    name: str,
    *,
    backend: str,
    max_concurrent: int,
) -> Agent:
    profile = store.create_agent_profile(
        name=name,
        instructions=f"Execute Ariadne run tasks with the {backend} backend.",
        preferred_capabilities=[backend],
        runtime_policy={"allow_real_execution": backend != "dry-run"},
        max_concurrent_taskruns=max(1, max_concurrent),
    )
    agent = store.get_agent(profile.id)
    if agent is None:
        raise KeyError(f"agent profile was created but agent is missing: {profile.id}")
    return agent


def _resolve_or_create_squad(
    store: Store,
    name: str,
    *,
    backend: str,
    member_name: str | None,
    max_concurrent: int,
) -> Squad:
    squad = store.get_squad_by_name(name)
    if squad is None:
        leader = _resolve_or_create_agent(
            store,
            f"{name} Leader",
            backend="dry-run",
            max_concurrent=1,
        )
        member = _resolve_or_create_agent(
            store,
            member_name or f"{name} Member",
            backend=backend,
            max_concurrent=max_concurrent,
        )
        squad = store.create_squad(
            name,
            leader.id,
            instructions="Delegate Ariadne run work to the best available member.",
        )
        store.add_squad_member(squad.id, member.id, role="coder")
        return squad

    if not store.get_squad_members(squad.id):
        member = _resolve_or_create_agent(
            store,
            member_name or f"{name} Member",
            backend=backend,
            max_concurrent=max_concurrent,
        )
        store.add_squad_member(squad.id, member.id, role="coder")
    return squad


def _build_daemon(
    store: Store,
    *,
    runtime_id: str,
    target_repo: str,
    max_concurrent: int,
    write_workspace: bool,
    poll_interval: float,
    backend_factory: BackendFactory,
    orchestrator: Orchestrator | None = None,
) -> Daemon:
    return Daemon(
        store=store,
        backend_factory=backend_factory,
        runtime_id=runtime_id,
        poll_interval=poll_interval,
        orchestrator=orchestrator,
        target_repo_path=target_repo,
        write_workspace=write_workspace,
        max_concurrent_taskruns=max_concurrent,
    )


def _drive_daemon_until(
    daemon: Daemon,
    *,
    done: Callable[[], bool],
    max_iterations: int,
) -> int:
    iterations = 0
    while not done() and iterations < max_iterations:
        daemon.start(max_iterations=1)
        iterations += 1
    return iterations


def _result_for_taskruns(
    store: Store,
    *,
    mode: str,
    detached: bool,
    completed: bool,
    runtime_id: str,
    target_repo: str,
    taskruns: list[TaskRun],
    issue_id: str | None = None,
    squad_id: str | None = None,
    iterations: int = 0,
) -> RunResult:
    return RunResult(
        mode=mode,
        detached=detached,
        completed=completed,
        runtime_id=runtime_id,
        target_repo=target_repo,
        issue_id=issue_id,
        squad_id=squad_id,
        task_results=[
            _summarize_taskrun(store, taskrun.id, fallback_title=taskrun.handoff_prompt)
            for taskrun in taskruns
        ],
        iterations=iterations,
    )


def _summarize_taskrun(
    store: Store,
    taskrun_id: str,
    *,
    fallback_title: str | None,
) -> RunTaskResult:
    taskrun = store.get_taskrun(taskrun_id)
    if taskrun is None:
        raise KeyError(f"taskrun not found: {taskrun_id}")
    agent = store.get_agent(taskrun.agent_id)
    result = taskrun.result or {}
    return RunTaskResult(
        title=fallback_title or taskrun.handoff_prompt or taskrun.issue_id,
        issue_id=taskrun.issue_id,
        taskrun_id=taskrun.id,
        agent_id=taskrun.agent_id,
        agent_name=agent.name if agent else taskrun.agent_id,
        status=taskrun.status.value,
        duration_seconds=_duration_seconds(taskrun),
        diff=result.get("diff"),
        changed_files=list(result.get("changed_files") or []),
        stdout=str(result.get("stdout") or ""),
        error=taskrun.error,
    )


def _all_terminal(store: Store, taskrun_ids: list[str]) -> bool:
    for taskrun_id in taskrun_ids:
        taskrun = store.get_taskrun(taskrun_id)
        if taskrun is None or taskrun.status not in TERMINAL_STATUSES:
            return False
    return True


def _mark_completed_issues_done(store: Store, taskruns: list[TaskRun]) -> None:
    seen_issue_ids: set[str] = set()
    for original in taskruns:
        taskrun = store.get_taskrun(original.id)
        if taskrun is None or taskrun.status != TaskStatus.COMPLETED:
            continue
        if taskrun.issue_id in seen_issue_ids:
            continue
        issue = store.get_issue(taskrun.issue_id)
        if issue is not None and issue.status != IssueStatus.DONE:
            store.update_issue_status(issue.id, IssueStatus.DONE)
        seen_issue_ids.add(taskrun.issue_id)


def _issue_done(store: Store, issue_id: str) -> bool:
    issue = store.get_issue(issue_id)
    return issue is not None and issue.status == IssueStatus.DONE


def _duration_seconds(taskrun: TaskRun) -> float | None:
    result = taskrun.result or {}
    result_duration = result.get("duration_seconds")
    if isinstance(result_duration, int | float):
        return float(result_duration)
    if taskrun.started_at and taskrun.completed_at:
        return (taskrun.completed_at - taskrun.started_at).total_seconds()
    return None


def _default_max_concurrent(task_count: int) -> int:
    return max(1, min(os.cpu_count() or 1, task_count))


def _clean_task_titles(task_titles: list[str] | None, task_count: int) -> list[str] | None:
    if task_titles is None:
        return None
    clean_titles = [title.strip() for title in task_titles]
    if len(clean_titles) != task_count:
        raise ValueError("task_titles length must match tasks length")
    return clean_titles


def _single_issue_id(taskruns: list[TaskRun]) -> str | None:
    return taskruns[0].issue_id if len(taskruns) == 1 else None


def _title_for(task_text: str) -> str:
    normalized = " ".join(task_text.split())
    return normalized[:80] if normalized else "Ariadne run squad task"
