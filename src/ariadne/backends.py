"""Execution backend protocol, DryRunBackend, CodexBackend, ClaudeBackend.

Real backends spawn coding-agent CLIs as subprocesses with safety gates,
command template rendering, diff capture, and timeout handling.

Protocol per docs/architecture/harness-backend.md.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from ariadne.models import (
    ExecutionContext,
    ExecutionResult,
    FailureReason,
    ProgressUpdate,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ExecutionBackend(Protocol):
    """Uniform interface for coding-agent harnesses."""

    name: str

    def is_available(self) -> bool: ...

    def execute(
        self,
        context: ExecutionContext,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> ExecutionResult: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUPPORTED_PLACEHOLDERS = {
    "target_repo",
    "handoff_file",
    "task_id",
    "model",
    "effort",
    "system_prompt",
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def render_command(template: str, context: ExecutionContext, handoff_file: str) -> str:
    """Render a command template. Fail fast on unknown placeholder."""
    values = {
        "target_repo": shlex.quote(context.target_repo_path),
        "handoff_file": shlex.quote(handoff_file),
        "task_id": shlex.quote(context.task_id),
        "model": shlex.quote(context.model or ""),
        "effort": shlex.quote(context.effort or ""),
        "system_prompt": shlex.quote(context.agent_instructions or ""),
    }

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in _SUPPORTED_PLACEHOLDERS:
            raise ValueError(f"unknown placeholder: {{{key}}}")
        return values[key]

    return _PLACEHOLDER_RE.sub(replace, template)


def _capture_diff(repo_path: str) -> tuple[str | None, list[str]]:
    """Capture git diff and changed files after execution.

    Returns (diff_text, changed_files). Non-git dir → (None, []).
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return None, []

    try:
        diff_result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, []

    if diff_result.returncode != 0:
        return None, []

    diff_text = diff_result.stdout.strip() or None
    changed_files = [
        line.strip().split(" ", 1)[-1].strip()
        for line in status_result.stdout.strip().split("\n")
        if line.strip()
    ]

    return diff_text, changed_files


def _blocked_result(
    context: ExecutionContext, backend_name: str, message: str, command: str = ""
) -> ExecutionResult:
    return ExecutionResult(
        backend_name=backend_name,
        success=False,
        exit_code=-1,
        stdout="",
        stderr=message,
        diff=None,
        changed_files=[],
        failure_reason=FailureReason.AGENT_ERROR,
        duration_seconds=0.0,
        command=command,
    )


# ---------------------------------------------------------------------------
# DryRunBackend
# ---------------------------------------------------------------------------


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
            failure_reason=None,
            duration_seconds=0.0,
            command="dry-run (no command)",
        )


# ---------------------------------------------------------------------------
# ShellBackend (base for real backends)
# ---------------------------------------------------------------------------


class _ShellBackend:
    """Base for backends that spawn a CLI subprocess.

    Subclasses define: name, template_env_var, default_template,
    executable_name.
    """

    name: str = ""
    template_env_var: str = ""
    default_template: str = ""
    executable_name: str = ""

    def is_available(self) -> bool:
        return shutil.which(self.executable_name) is not None

    def _command_template(self) -> str:
        return os.environ.get(self.template_env_var, self.default_template)

    def execute(
        self,
        context: ExecutionContext,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()

        # Safety gate: dual confirmation
        if os.environ.get("ARIADNE_ENABLE_EXTERNAL_EXECUTION") != "1":
            return _blocked_result(
                context, self.name,
                "External execution blocked: ARIADNE_ENABLE_EXTERNAL_EXECUTION must be 1.",
            )
        if not context.confirm_execution:
            return _blocked_result(
                context, self.name,
                "External execution blocked: --confirm-execution is required.",
            )

        # Check CLI availability
        if not self.is_available():
            return _blocked_result(
                context, self.name,
                f"External execution blocked: `{self.executable_name}` command is unavailable.",
            )

        # Write handoff to temp file
        handoff_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, prefix=f"ariadne-handoff-{context.task_id}-"
        )
        handoff_file.write(context.handoff_prompt)
        handoff_file.close()

        command = ""
        try:
            try:
                command = render_command(self._command_template(), context, handoff_file.name)
            except ValueError as e:
                return _blocked_result(context, self.name, f"command template error: {e}")

            # Progress: starting
            if on_progress:
                on_progress(ProgressUpdate(
                    task_id=context.task_id,
                    summary=f"starting {self.name} execution",
                    step=1, total=2,
                    timestamp=datetime.now(timezone.utc),
                ))

            # Execute
            try:
                result = subprocess.run(
                    command,
                    cwd=context.target_repo_path,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=context.timeout_seconds,
                )
                exit_code = result.returncode
                stdout = result.stdout
                stderr = result.stderr
            except subprocess.TimeoutExpired:
                duration = time.monotonic() - started
                return ExecutionResult(
                    backend_name=self.name,
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=f"execution timed out after {context.timeout_seconds}s",
                    diff=None,
                    changed_files=[],
                    failure_reason=FailureReason.TIMEOUT,
                    duration_seconds=duration,
                    command=command,
                )

            duration = time.monotonic() - started

            # Capture diff
            diff, changed_files = _capture_diff(context.target_repo_path)

            # Progress: finished
            if on_progress:
                on_progress(ProgressUpdate(
                    task_id=context.task_id,
                    summary=f"execution finished (exit_code={exit_code})",
                    step=2, total=2,
                    timestamp=datetime.now(timezone.utc),
                ))

            success = exit_code == 0
            return ExecutionResult(
                backend_name=self.name,
                success=success,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                diff=diff,
                changed_files=changed_files,
                failure_reason=None if success else FailureReason.AGENT_ERROR,
                duration_seconds=duration,
                command=command,
            )
        finally:
            # Always cleanup handoff file, even on exception
            try:
                os.unlink(handoff_file.name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CodexBackend
# ---------------------------------------------------------------------------


class CodexBackend(_ShellBackend):
    """Codex CLI backend. Command: codex exec --cd {target_repo} - < {handoff_file}"""

    name = "codex"
    template_env_var = "ARIADNE_CODEX_COMMAND_TEMPLATE"
    default_template = "codex exec --cd {target_repo} - < {handoff_file}"
    executable_name = "codex"


# ---------------------------------------------------------------------------
# ClaudeBackend
# ---------------------------------------------------------------------------


class ClaudeBackend(_ShellBackend):
    """Claude Code CLI backend. Command: claude --print --output-format json < {handoff_file}"""

    name = "claude-code"
    template_env_var = "ARIADNE_CLAUDE_COMMAND_TEMPLATE"
    default_template = "claude --print --output-format json < {handoff_file}"
    executable_name = "claude"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, ExecutionBackend] = {
    "dry-run": DryRunBackend(),
    "codex": CodexBackend(),
    "claude-code": ClaudeBackend(),
}


def get_backend(name: str) -> ExecutionBackend:
    """Return the backend for a name. Raises ValueError for unknown names."""
    if name not in _BACKENDS:
        raise ValueError(f"unknown backend: {name}")
    return _BACKENDS[name]


def available_backends() -> list[str]:
    return list(_BACKENDS.keys())
