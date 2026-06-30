"""Daemon: poll-claim-execute loop that turns the state machine into a running system.

Per docs/plan/tasks/core-003.md and docs/architecture/task-state-machine.md.
Synchronous loop — no threads, no asyncio. Sufficient for local single-user.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from ariadne.backends import ExecutionBackend
from ariadne.models import (
    ExecutionContext,
    ExecutionResult,
    FailureReason,
    ProgressUpdate,
    Task,
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
    ):
        self.store = store
        self.backend_factory = backend_factory
        self.runtime_id = runtime_id
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.stale_claim_timeout = stale_claim_timeout
        self._running = False
        self._last_heartbeat: datetime | None = None

    def start(self, max_iterations: int | None = None) -> None:
        """Run the poll loop. Stops on KeyboardInterrupt or max_iterations."""
        self._running = True
        iterations = 0
        try:
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

    def _poll_once(self) -> Task | None:
        """Try to claim the oldest queued task for any agent. Returns task or None."""
        agents = self.store.list_agents()
        for agent in agents:
            task = self.store.claim_task(agent.id, self.runtime_id)
            if task is not None:
                logger.info("claimed task %s for agent %s", task.id, agent.name)
                return task
        return None

    def _execute_task(self, task: Task) -> None:
        """Execute a claimed task: start → backend.execute → complete/fail."""
        agent = self.store.get_agent(task.agent_id)
        agent_name = agent.name if agent else "unknown"
        instructions = agent.instructions if agent else ""

        self.store.start_task(task.id)  # claimed → running

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
            handoff_prompt=f"Execute task for issue {task.issue_id}",
            target_repo_path=".",
            skill_refs=[],
        )

        try:
            result = backend.execute(context, on_progress=self._on_progress)
        except TimeoutError:
            self.store.fail_task(task.id, "execution timed out", FailureReason.TIMEOUT)
            self._maybe_retry(task)
            return
        except Exception as e:
            self.store.fail_task(task.id, str(e), FailureReason.AGENT_ERROR)
            self._maybe_retry(task)
            return

        if result.success:
            self.store.complete_task(task.id, _result_to_dict(result))
            logger.info("task %s completed", task.id)
        else:
            reason = result.failure_reason or FailureReason.AGENT_ERROR
            self.store.fail_task(task.id, result.stderr or "execution failed", reason)
            self._maybe_retry(task)

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
        now = datetime.now(timezone.utc)
        self._last_heartbeat = now
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
        "test_result": result.test_result,
        "duration_seconds": result.duration_seconds,
        "command": result.command,
    }
