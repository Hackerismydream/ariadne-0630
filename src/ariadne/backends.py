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


def _is_git_repo(path: str) -> bool:
    """Check if path is inside a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


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

    @staticmethod
    def parse_output(stdout: str) -> tuple[str, dict | None]:
        """Override in subclasses to parse structured output. Default: passthrough."""
        return stdout, None

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

        # Worktree isolation: if target_repo is a git repo, create a worktree
        exec_path = context.target_repo_path
        worktree_path = None
        if _is_git_repo(context.target_repo_path):
            trace = context.trace_id or context.task_id
            worktree_path = os.path.join(
                context.target_repo_path, ".ariadne-worktrees", trace
            )
            try:
                subprocess.run(
                    ["git", "worktree", "add", worktree_path, "-b", f"ariadne/{trace}"],
                    cwd=context.target_repo_path,
                    capture_output=True, text=True, timeout=10,
                )
                exec_path = worktree_path
            except Exception:
                worktree_path = None

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

            if on_progress:
                on_progress(ProgressUpdate(
                    task_id=context.task_id,
                    summary=f"starting {self.name} execution",
                    step=1, total=0,
                    timestamp=datetime.now(timezone.utc),
                ))

            # Execute with Popen for streaming
            try:
                proc = subprocess.Popen(
                    command, cwd=exec_path, shell=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                stdout_lines = []
                try:
                    for line in proc.stdout:
                        stdout_lines.append(line)
                        if on_progress:
                            on_progress(ProgressUpdate(
                                task_id=context.task_id,
                                summary=line.strip()[:200],
                                step=0, total=0,
                                timestamp=datetime.now(timezone.utc),
                            ))
                    proc.wait(timeout=context.timeout_seconds)
                    exit_code = proc.returncode
                    stdout = "".join(stdout_lines)
                    stderr = proc.stderr.read() if proc.stderr else ""
                except subprocess.TimeoutExpired:
                    proc.kill()
                    duration = time.monotonic() - started
                    return ExecutionResult(
                        backend_name=self.name, success=False, exit_code=-1,
                        stdout="", stderr=f"execution timed out after {context.timeout_seconds}s",
                        diff=None, changed_files=[],
                        failure_reason=FailureReason.TIMEOUT,
                        duration_seconds=duration, command=command,
                    )
            except OSError as e:
                return _blocked_result(context, self.name, f"failed to spawn process: {e}")

            duration = time.monotonic() - started

            # Parse structured output (ClaudeBackend overrides parse_output)
            parsed_stdout, metadata = self.parse_output(stdout)

            # Capture diff from execution path
            diff, changed_files = _capture_diff(exec_path)

            if on_progress:
                on_progress(ProgressUpdate(
                    task_id=context.task_id,
                    summary=f"execution finished (exit_code={exit_code})",
                    step=0, total=0,
                    timestamp=datetime.now(timezone.utc),
                ))

            success = exit_code == 0
            return ExecutionResult(
                backend_name=self.name, success=success, exit_code=exit_code,
                stdout=parsed_stdout, stderr=stderr,
                diff=diff, changed_files=changed_files,
                failure_reason=None if success else FailureReason.AGENT_ERROR,
                duration_seconds=duration, command=command, metadata=metadata,
            )
        finally:
            try:
                os.unlink(handoff_file.name)
            except OSError:
                pass
            if worktree_path:
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", worktree_path, "--force"],
                        cwd=context.target_repo_path,
                        capture_output=True, text=True, timeout=10,
                    )
                    trace = context.trace_id or context.task_id
                    subprocess.run(
                        ["git", "branch", "-D", f"ariadne/{trace}"],
                        cwd=context.target_repo_path,
                        capture_output=True, text=True, timeout=10,
                    )
                except Exception:
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
    """Claude Code CLI backend. Parses --output-format json into structured fields."""

    name = "claude-code"
    template_env_var = "ARIADNE_CLAUDE_COMMAND_TEMPLATE"
    default_template = "claude --print --output-format json < {handoff_file}"
    executable_name = "claude"

    @staticmethod
    def parse_output(stdout: str) -> tuple[str, dict | None]:
        """Parse Claude's JSON output. Returns (result_text, metadata_dict).

        If stdout is valid JSON with a 'result' field, extract it.
        Otherwise return (raw_stdout, None) — graceful fallback.
        """
        import json as _json
        try:
            data = _json.loads(stdout.strip())
            if isinstance(data, dict) and "result" in data:
                metadata = {k: v for k, v in data.items() if k != "result"}
                return data["result"], metadata
        except (_json.JSONDecodeError, ValueError):
            pass
        return stdout, None


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
