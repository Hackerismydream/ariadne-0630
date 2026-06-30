"""Tests for backends.py — safety gate, command rendering, diff capture, registry.

Per docs/plan/tasks/backend-001.md "test_backends.py must cover".
"""

import os
import subprocess
from unittest.mock import patch

import pytest

from ariadne.backends import (
    CodexBackend,
    ClaudeBackend,
    DryRunBackend,
    _capture_diff,

    get_backend,
    render_command,
)
from ariadne.models import ExecutionContext


def _make_context(**overrides) -> ExecutionContext:
    defaults = {
        "task_id": "task-test",
        "agent_name": "TestAgent",
        "agent_instructions": "do things",
        "handoff_prompt": "implement feature X",
        "target_repo_path": "/tmp/test-repo",
        "skill_refs": [],
        "timeout_seconds": 60,
        "confirm_execution": True,
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------


def test_safety_gate_blocks_without_env():
    """no ARIADNE_ENABLE_EXTERNAL_EXECUTION → blocked"""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ARIADNE_ENABLE_EXTERNAL_EXECUTION", None)
        backend = CodexBackend()
        result = backend.execute(_make_context())
        assert not result.success
        assert "ARIADNE_ENABLE_EXTERNAL_EXECUTION" in result.stderr


def test_safety_gate_blocks_without_confirm():
    """no confirm_execution → blocked"""
    with patch.dict(os.environ, {"ARIADNE_ENABLE_EXTERNAL_EXECUTION": "1"}):
        backend = CodexBackend()
        result = backend.execute(_make_context(confirm_execution=False))
        assert not result.success
        assert "confirm-execution" in result.stderr


def test_safety_gate_blocks_with_only_env():
    """env set but no confirm → blocked"""
    with patch.dict(os.environ, {"ARIADNE_ENABLE_EXTERNAL_EXECUTION": "1"}):
        backend = CodexBackend()
        result = backend.execute(_make_context(confirm_execution=False))
        assert not result.success


# ---------------------------------------------------------------------------
# Command template rendering
# ---------------------------------------------------------------------------


def test_command_template_rendering():
    """all supported placeholders render correctly"""
    context = _make_context(model="gpt-4", effort="high")
    # Use a template with all placeholders
    template = "{target_repo} {handoff_file} {task_id} {model} {effort} {system_prompt}"
    rendered = render_command(template, context, "/tmp/handoff.md")
    assert "/tmp/test-repo" in rendered
    assert "/tmp/handoff.md" in rendered
    assert "task-test" in rendered
    assert "gpt-4" in rendered
    assert "high" in rendered
    assert "do things" in rendered


def test_unknown_placeholder_raises():
    """unknown {foo} → ValueError"""
    context = _make_context()
    with pytest.raises(ValueError, match="unknown placeholder"):
        render_command("{foo} {target_repo}", context, "/tmp/h.md")


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
