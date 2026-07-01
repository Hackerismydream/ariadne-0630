"""SQLite store facade.

The public `Store` API stays import-compatible while implementation moves into
lightweight repository and service layers.
"""

from __future__ import annotations

from ariadne.models import FailureReason, Task, TaskRun, TaskRunClaim
from ariadne.service import TaskService

from .base import (
    InvalidStateTransition,
    MaxAttemptsExhausted,
    StoreBase,
)
from .benchmark_repo import BenchmarkRepo
from .issue_repo import IssueRepo
from .runtime_repo import RuntimeRepo
from .skill_repo import SkillRepo
from .squad_repo import SquadRepo
from .task_repo import TaskRepo


class Store(BenchmarkRepo, IssueRepo, RuntimeRepo, SkillRepo, SquadRepo, TaskRepo, StoreBase):
    """Backward-compatible facade over the lightweight store layers."""

    def __init__(self, db_path: str = "ariadne.db"):
        super().__init__(db_path)
        self.task_service = TaskService(self)

    def claim_taskrun_for_runtime_machine(
        self,
        runtime_machine_id: str,
        lease_seconds: int = 60,
    ) -> TaskRunClaim | None:
        return self.task_service.claim_taskrun_for_runtime_machine(
            runtime_machine_id,
            lease_seconds=lease_seconds,
        )

    def claim_task(self, agent_id: str, runtime_id: str) -> Task | None:
        return self.task_service.claim_task(agent_id, runtime_id)

    def claim_taskrun(self, agent_profile_id: str, runtime_id: str) -> TaskRun | None:
        return self.task_service.claim_taskrun(agent_profile_id, runtime_id)

    def start_task(self, task_id: str) -> Task:
        return self.task_service.start_task(task_id)

    def start_taskrun(self, taskrun_id: str) -> TaskRun:
        return self.task_service.start_taskrun(taskrun_id)

    def complete_task(self, task_id: str, result: dict) -> Task:
        return self.task_service.complete_task(task_id, result)

    def complete_taskrun(self, taskrun_id: str, result: dict) -> TaskRun:
        return self.task_service.complete_taskrun(taskrun_id, result)

    def fail_task(self, task_id: str, error: str, reason: FailureReason) -> Task:
        return self.task_service.fail_task(task_id, error, reason)

    def fail_taskrun(
        self, taskrun_id: str, error: str, reason: FailureReason
    ) -> TaskRun:
        return self.task_service.fail_taskrun(taskrun_id, error, reason)

    def cancel_task(self, task_id: str) -> Task:
        return self.task_service.cancel_task(task_id)

    def cancel_taskrun(self, taskrun_id: str) -> TaskRun:
        return self.task_service.cancel_taskrun(taskrun_id)

    def retry_task(self, task_id: str) -> Task:
        return self.task_service.retry_task(task_id)

    def retry_taskrun(self, taskrun_id: str) -> TaskRun:
        return self.task_service.retry_taskrun(taskrun_id)

    def recover_stale_claims(self, stale_timeout_seconds: float = 60.0) -> int:
        return self.task_service.recover_stale_claims(stale_timeout_seconds)


__all__ = [
    "InvalidStateTransition",
    "MaxAttemptsExhausted",
    "Store",
]
