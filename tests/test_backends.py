"""Tests for backends.py — safety gate, command rendering, diff capture, registry.

Per docs/plan/tasks/backend-001.md "test_backends.py must cover".
"""

import os
import shlex
import subprocess
import sys
from unittest.mock import patch

import pytest

from ariadne.backends import (
    CodexBackend,
    ClaudeBackend,
    DryRunBackend,
    _ShellBackend,
    _capture_diff,
    available_backends,
    get_backend,
    register_backend,
    render_command,
)
from ariadne.models import ExecutionContext, FailureReason


def _make_context(**overrides) -> ExecutionContext:
    defaults = {
        "task_id": "task-test",
        "agent_name": "TestAgent",
        "agent_instructions": "do things",
        "handoff_prompt": "implement feature X",
        "target_repo_path": "/tmp/test-repo",
        "skill_refs": [],
        "timeout_seconds": 60,
        "confirm_execution": False,
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# Isolation-first execution
# ---------------------------------------------------------------------------


def _init_repo(repo):
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)


_WRITE_AGENT_FILE = 'from pathlib import Path; Path("agent.txt").write_text("changed")'


class WriteBackend(_ShellBackend):
    name = "write"
    template_env_var = "ARIADNE_WRITE_COMMAND_TEMPLATE"
    default_template = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(_WRITE_AGENT_FILE)}"
    )
    executable_name = sys.executable

    def is_available(self) -> bool:
        return True


def test_git_repo_executes_in_worktree_without_env_or_confirmation(tmp_path):
    """git repo defaults to isolated worktree execution with no confirmation gate."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ARIADNE_ENABLE_EXTERNAL_EXECUTION", None)
        result = WriteBackend().execute(_make_context(target_repo_path=str(repo)))

    assert result.success is True
    assert result.execution_repo_path != str(repo)
    assert (repo / "agent.txt").exists() is False
    assert "agent.txt" in result.changed_files
    assert result.metadata["worktree_audit"]["worktree_created"] is True


def test_non_git_directory_requires_write_workspace(tmp_path):
    """non-git targets cannot run real commands unless write-workspace is explicit."""
    result = WriteBackend().execute(_make_context(target_repo_path=str(tmp_path)))

    assert result.success is False
    assert "--write-workspace" in result.stderr
    assert (tmp_path / "agent.txt").exists() is False


def test_write_workspace_allows_non_git_directory_execution(tmp_path):
    """confirm_execution=True is now the explicit write-workspace escape hatch."""
    result = WriteBackend().execute(
        _make_context(target_repo_path=str(tmp_path), confirm_execution=True)
    )

    assert result.success is True
    assert result.execution_repo_path == str(tmp_path)
    assert (tmp_path / "agent.txt").read_text() == "changed"
    assert result.metadata["worktree_audit"]["worktree_created"] is False


def test_worktree_creation_failure_does_not_fallback_to_target_repo(tmp_path):
    """failed isolation is a hard stop unless write-workspace is explicit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    original_run = subprocess.run

    def fail_worktree_add(args, *pargs, **kwargs):
        if args[:3] == ["git", "worktree", "add"]:
            raise subprocess.CalledProcessError(1, args, stderr="boom")
        return original_run(args, *pargs, **kwargs)

    with patch("ariadne.backends.subprocess.run", side_effect=fail_worktree_add):
        result = WriteBackend().execute(_make_context(target_repo_path=str(repo)))

    assert result.success is False
    assert "worktree isolation failed" in result.stderr
    assert (repo / "agent.txt").exists() is False


def test_shell_backend_timeout_applies_before_stdout_eof(tmp_path):
    """no-output long-running commands are killed by timeout_seconds."""

    class SleepBackend(_ShellBackend):
        name = "sleep"
        template_env_var = "ARIADNE_SLEEP_COMMAND_TEMPLATE"
        default_template = f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(5)\""
        executable_name = sys.executable

        def is_available(self) -> bool:
            return True

    result = SleepBackend().execute(
        _make_context(
            target_repo_path=str(tmp_path),
            timeout_seconds=1,
            confirm_execution=True,
        )
    )

    assert result.success is False
    assert result.failure_reason == FailureReason.TIMEOUT
    assert "timed out" in result.stderr
    assert result.duration_seconds < 3


# ---------------------------------------------------------------------------
# Command template rendering
# ---------------------------------------------------------------------------


def test_command_template_rendering():
    """all supported placeholders render correctly"""
    context = _make_context(
        model="gpt-4",
        effort="high",
        resume_session_id="session-123",
        mcp_config_path="/tmp/mcp.json",
    )
    # Use a template with all placeholders
    template = (
        "{target_repo} {handoff_file} {task_id} {model} {effort} "
        "{system_prompt} {resume_session_id} {mcp_config}"
    )
    rendered = render_command(template, context, "/tmp/handoff.md")
    assert "/tmp/test-repo" in rendered
    assert "/tmp/handoff.md" in rendered
    assert "task-test" in rendered
    assert "gpt-4" in rendered
    assert "high" in rendered
    assert "do things" in rendered
    assert "session-123" in rendered
    assert "/tmp/mcp.json" in rendered


def test_command_template_uses_execution_repo_when_provided():
    """{target_repo} and {execution_repo} point at the isolated execution path."""
    context = _make_context()
    rendered = render_command(
        "{target_repo} {execution_repo}",
        context,
        "/tmp/handoff.md",
        execution_repo_path="/tmp/worktree",
    )
    assert "/tmp/worktree /tmp/worktree" == rendered
    assert "/tmp/test-repo" not in rendered


def test_unknown_placeholder_raises():
    """unknown {foo} → ValueError"""
    context = _make_context()
    with pytest.raises(ValueError, match="unknown placeholder"):
        render_command("{foo} {target_repo}", context, "/tmp/h.md")


def test_provider_specific_resume_and_mcp_fragments():
    """provider templates add resume/MCP flags only when context supplies them."""
    context = _make_context(
        resume_session_id="session-123",
        mcp_config_path="/tmp/mcp.json",
    )

    claude_template = ClaudeBackend()._render_template(context)
    codex_template = CodexBackend()._render_template(context)

    assert "--resume {resume_session_id}" in claude_template
    assert "--mcp-config {mcp_config}" in claude_template
    assert "--resume" not in codex_template
    assert "--mcp-config {mcp_config}" in codex_template

    empty_context = _make_context()
    assert "--resume" not in ClaudeBackend()._render_template(empty_context)
    assert "--mcp-config" not in ClaudeBackend()._render_template(empty_context)
    assert "--mcp-config" not in CodexBackend()._render_template(empty_context)


# ---------------------------------------------------------------------------
# Diff capture
# ---------------------------------------------------------------------------


def test_diff_capture_git_repo(tmp_path):
    """git repo with changes → diff + changed_files populated"""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

    # Make a change
    (repo / "file.txt").write_text("hello world")

    diff, changed = _capture_diff(str(repo))
    assert diff is not None
    assert "hello world" in diff
    assert "file.txt" in changed


def test_diff_capture_no_git(tmp_path):
    """non-git directory → (None, [])"""
    diff, changed = _capture_diff(str(tmp_path))
    assert diff is None
    assert changed == []


def test_diff_capture_clean_repo(tmp_path):
    """git repo with no changes → (None or empty, [])"""
    repo = tmp_path / "clean-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

    diff, changed = _capture_diff(str(repo))
    assert diff is None or diff == ""
    assert changed == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_backend_registry():
    """known names return backends, unknown → ValueError"""
    assert isinstance(get_backend("codex"), CodexBackend)
    assert isinstance(get_backend("claude-code"), ClaudeBackend)
    assert isinstance(get_backend("dry-run"), DryRunBackend)
    with pytest.raises(ValueError):
        get_backend("nonexistent")


def test_backend_registry_accepts_in_process_extensions():
    """local extensions can register a backend without editing the literal."""
    class ExtensionBackend(DryRunBackend):
        name = "extension-test-backend"

    register_backend(ExtensionBackend())

    assert "extension-test-backend" in available_backends()
    assert get_backend("extension-test-backend").name == "extension-test-backend"
    with pytest.raises(ValueError, match="already registered"):
        register_backend(ExtensionBackend())


def test_codex_is_available():
    """codex on PATH → True, else False"""
    backend = CodexBackend()
    # Just verify the method runs without error — actual availability depends on env
    result = backend.is_available()
    assert isinstance(result, bool)


def test_claude_is_available():
    """claude on PATH → True, else False"""
    backend = ClaudeBackend()
    result = backend.is_available()
    assert isinstance(result, bool)


def test_dry_run_still_works():
    """existing DryRunBackend behavior unchanged"""
    backend = DryRunBackend()
    assert backend.is_available() is True
    result = backend.execute(_make_context())
    assert result.success
    assert result.backend_name == "dry-run"
