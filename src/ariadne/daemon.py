"""Daemon: poll-claim-execute loop that turns the state machine into a running system.

Per docs/plan/tasks/core-003.md and docs/architecture/task-state-machine.md.
Synchronous loop — no threads, no asyncio. Sufficient for local single-user.
"""

from __future__ import annotations

import logging
import platform
import shutil
import socket
import time
from datetime import datetime, timezone
from typing import Callable

from ariadne.backends import ExecutionBackend, available_backends
from ariadne.models import (
    ExecutionContext,
    ExecutionResult,
    FailureReason,
    ProgressUpdate,
    RuntimeCapabilityStatus,
    Task,
    TaskStatus,
)
from ariadne.store import MaxAttemptsExhausted, Store

logger = logging.getLogger(__name__)


class Daemon:
    """Polls for queued tasks, claims them, executes via backend, reports result."""

    def __init__(
        self,
        store: Store,
        backend_factory: Callable[[str], ExecutionBackend],
        runtime_id: str = "local",
        poll_interval: float = 3.0,
        heartbeat_interval: float = 15.0,
        stale_claim_timeout: float = 60.0,
        orchestrator=None,
        target_repo_path: str = ".",
    ):
        self.store = store
        self.backend_factory = backend_factory
        self.runtime_id = runtime_id
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.stale_claim_timeout = stale_claim_timeout
        self.orchestrator = orchestrator
        self.target_repo_path = target_repo_path
        self._running = False
        self._last_heartbeat: datetime | None = None
        self._runtime_registered = False

    def start(self, max_iterations: int | None = None) -> None:
        """Run the poll loop. Stops on KeyboardInterrupt or max_iterations."""
        self._running = True
        iterations = 0
        try:
            self._register_runtime()
            while self._running:
                self._recover_stale_claims()
                self._send_heartbeat()

                claimed = self._poll_once()
                if claimed:
                    self._execute_task(claimed)

                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    logger.info("reached max_iterations=%d, stopping", max_iterations)
                    break

                if self._running:
                    time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("keyboard interrupt — shutting down")
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    def _register_runtime(self) -> None:
        """Register this daemon as a RuntimeMachine and probe capabilities."""
        self.store.register_runtime_machine(
            runtime_machine_id=self.runtime_id,
            name=f"{socket.gethostname()}:{self.runtime_id}",
            version="0.1.0",
            workspace_root=self.target_repo_path,
            max_concurrent_taskruns=1,
            repo_allowlist=[self.target_repo_path],
            device_info={
                "hostname": socket.gethostname(),
                "os": platform.system(),
                "arch": platform.machine(),
            },
        )
        for provider in available_backends():
            try:
                backend = self.backend_factory(provider)
                executable = getattr(backend, "executable_name", "")
                command_path = (
                    "dry-run"
                    if provider == "dry-run"
                    else shutil.which(executable) or executable or provider
                )
                is_available = backend.is_available()
                self.store.upsert_runtime_capability(
                    runtime_machine_id=self.runtime_id,
                    provider=provider,
                    command_path=command_path,
                    status=RuntimeCapabilityStatus.AVAILABLE
                    if is_available
                    else RuntimeCapabilityStatus.UNAVAILABLE,
                    health_error=None
                    if is_available
                    else f"{executable or provider} not found",
                )
            except Exception as exc:
                self.store.upsert_runtime_capability(
                    runtime_machine_id=self.runtime_id,
                    provider=provider,
                    command_path=provider,
                    status=RuntimeCapabilityStatus.UNAVAILABLE,
                    health_error=str(exc),
                )
        self._runtime_registered = True

    def _poll_once(self) -> Task | None:
        """Try to claim the oldest queued TaskRun for this runtime."""
        if not self._runtime_registered:
            self._register_runtime()
        claim = self.store.claim_taskrun_for_runtime_machine(self.runtime_id)
        if claim is not None:
            logger.info(
                "claimed taskrun %s with lease %s",
                claim.taskrun.id,
                claim.lease.id,
            )
            return claim.taskrun
        return None

    def _execute_task(self, task: Task) -> None:
        """Execute a claimed task.

        Leader tasks (squad_id set + agent is squad leader) → orchestrator.
        Member tasks → backend execution.
        """
        # Check if this is a leader task
        if task.squad_id and self._is_leader_task(task):
            self._execute_leader_task(task)
            return

        self._execute_member_task(task)

    def _is_leader_task(self, task: Task) -> bool:
        """True if this task's agent is the squad's leader."""
        squad = self.store.get_squad(task.squad_id)
        if squad is None:
            return False
        return task.agent_id == squad.leader_id

    def _execute_leader_task(self, task: Task) -> None:
        """Delegate to orchestrator for leader decision."""
        if self.orchestrator is None:
            logger.error("no orchestrator set — cannot handle leader task %s", task.id)
            self.store.start_task(task.id)
            self.store.fail_task(task.id, "no orchestrator configured", FailureReason.AGENT_ERROR)
            self._release_active_lease(task.id)
            return
        latest = self.store.get_task(task.id)
        if latest and latest.status in (TaskStatus.PREPARING, TaskStatus.CLAIMED):
            self.store.start_task(task.id)
        self.orchestrator.handle_leader_task(task)
        self._release_active_lease(task.id)

    def _execute_member_task(self, task: Task) -> None:
        """Execute a member task via backend."""
        agent = self.store.get_agent(task.agent_id)
        agent_name = agent.name if agent else "unknown"
        instructions = agent.instructions if agent else ""

        self.store.start_task(task.id)  # claimed → running
        if task.trace_id:
            self.store.log_activity(task.trace_id, task.id, "started", {"backend": agent.backends[0] if agent and agent.backends else "dry-run"})

        backend_name = "dry-run"
        if agent and agent.backends:
            backend_name = agent.backends[0]

        try:
            backend = self.backend_factory(backend_name)
        except ValueError:
            logger.warning("unknown backend '%s', falling back to dry-run", backend_name)
            backend = self.backend_factory("dry-run")

        context = ExecutionContext(
            task_id=task.id,
            agent_name=agent_name,
            agent_instructions=instructions,
            handoff_prompt=task.handoff_prompt or f"Execute task for issue {task.issue_id}",
            target_repo_path=self.target_repo_path,
            skill_refs=[],
            confirm_execution=True,
            trace_id=task.trace_id,
        )

        try:
            result = backend.execute(context, on_progress=self._on_progress)
        except TimeoutError:
            self.store.fail_task(task.id, "execution timed out", FailureReason.TIMEOUT)
            self._release_active_lease(task.id)
            self._maybe_retry(task)
            self._trigger_event_loop(task)
            return
        except Exception as e:
            self.store.fail_task(task.id, str(e), FailureReason.AGENT_ERROR)
            self._release_active_lease(task.id)
            self._maybe_retry(task)
            self._trigger_event_loop(task)
            return

        if result.success:
            self.store.complete_task(task.id, _result_to_dict(result))
            self._release_active_lease(task.id)
            if task.trace_id:
                self.store.log_activity(task.trace_id, task.id, "completed", {"backend": result.backend_name})
            logger.info("task %s completed", task.id)
        else:
            reason = result.failure_reason or FailureReason.AGENT_ERROR
            self.store.fail_task(task.id, result.stderr or "execution failed", reason)
            self._release_active_lease(task.id)
            if task.trace_id:
                self.store.log_activity(task.trace_id, task.id, "failed", {"reason": reason.value, "error": result.stderr[:200] if result.stderr else ""})
            self._maybe_retry(task)

        # Trigger event loop after member task reaches terminal state
        self._trigger_event_loop(task)

    def _trigger_event_loop(self, task: Task) -> None:
        """Notify orchestrator that a member task completed (if orchestrator is set)."""
        if self.orchestrator and task.squad_id:
            self.orchestrator.on_member_task_complete(task)

    def _release_active_lease(self, task_id: str) -> None:
        lease = self.store.get_active_runtime_lease_for_taskrun(task_id)
        if lease is not None:
            self.store.release_runtime_lease(lease.id)

    def _maybe_retry(self, task: Task) -> None:
        """Retry if attempts remain."""
        if task.attempt < task.max_attempts:
            try:
                retried = self.store.retry_task(task.id)
                logger.info("retrying task %s as %s (attempt %d)", task.id, retried.id, retried.attempt)
            except MaxAttemptsExhausted:
                logger.info("task %s: max attempts exhausted, no retry", task.id)
        else:
            logger.info("task %s: attempt %d == max_attempts %d, no retry", task.id, task.attempt, task.max_attempts)

    def _on_progress(self, update: ProgressUpdate) -> None:
        logger.info("progress: %s (step %d/%d)", update.summary, update.step, update.total)

    def _recover_stale_claims(self) -> int:
        """Move stale claimed tasks back to queued."""
        recovered = self.store.recover_stale_claims(self.stale_claim_timeout)
        if recovered:
            logger.info("recovered %d stale claims", recovered)
        return recovered

    def _send_heartbeat(self) -> None:
        """Update heartbeat timestamp in DB."""
        if not self._runtime_registered:
            self._register_runtime()
        now = datetime.now(timezone.utc)
        self._last_heartbeat = now
        self.store.heartbeat_runtime_machine(self.runtime_id)
        self.store._conn.execute(
            """CREATE TABLE IF NOT EXISTS daemon_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        self.store._conn.execute(
            "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)",
            ("last_heartbeat", now.isoformat()),
        )
        self.store._conn.commit()

    def status(self) -> dict:
        """Return daemon status dict."""
        return {
            "running": self._running,
            "runtime_id": self.runtime_id,
            "last_heartbeat": self._last_heartbeat.isoformat() if self._last_heartbeat else None,
            "poll_interval": self.poll_interval,
            "stale_claim_timeout": self.stale_claim_timeout,
        }


def _result_to_dict(result: ExecutionResult) -> dict:
    return {
        "backend_name": result.backend_name,
        "success": result.success,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "diff": result.diff,
        "changed_files": result.changed_files,
        "duration_seconds": result.duration_seconds,
        "command": result.command,
    }
