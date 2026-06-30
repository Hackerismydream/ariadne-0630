"""Execution backend protocol and DryRunBackend.

Real backends (CodexBackend, ClaudeBackend) are added in Phase 3.
For now, DryRunBackend lets the daemon loop work end-to-end without
external dependencies.

Protocol per docs/architecture/harness-backend.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

from ariadne.models import (
    ExecutionContext,
    ExecutionResult,
    ProgressUpdate,
)


class ExecutionBackend(Protocol):
    """Uniform interface for coding-agent harnesses."""

    name: str

    def is_available(self) -> bool: ...

    def execute(
        self,
        context: ExecutionContext,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> ExecutionResult: ...


class DryRunBackend:
    """No-op backend for testing. Records an execution result without subprocess."""

    name = "dry-run"

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        context: ExecutionContext,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> ExecutionResult:
        if on_progress:
            on_progress(
                ProgressUpdate(
                    task_id=context.task_id,
                    summary="dry-run: simulated execution",
                    step=1,
                    total=1,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        return ExecutionResult(
            backend_name=self.name,
            success=True,
            exit_code=0,
            stdout=f"[dry-run] would execute: {context.handoff_prompt[:200]}",
            stderr="",
            diff=None,
            changed_files=[],
            test_result=None,
            failure_reason=None,
            duration_seconds=0.0,
            command="dry-run (no command)",
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, ExecutionBackend] = {
    "dry-run": DryRunBackend(),
}


def get_backend(name: str) -> ExecutionBackend:
    """Return the backend for a name. Raises ValueError for unknown names."""
    if name not in _BACKENDS:
        raise ValueError(f"unknown backend: {name}")
    return _BACKENDS[name]


def available_backends() -> list[str]:
    return list(_BACKENDS.keys())
