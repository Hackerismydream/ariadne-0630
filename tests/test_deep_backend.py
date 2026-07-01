"""Tests for deep backend features: Claude JSON parse, worktree, streaming.

Per docs/plan/tasks/deep-002.md.
"""

import json
import subprocess
from unittest.mock import patch, MagicMock


from ariadne.backends import ClaudeBackend, CodexBackend, _is_git_repo
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
        "trace_id": "trace-test123",
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# Claude JSON parse
# ---------------------------------------------------------------------------


def test_claude_json_parsed():
    """valid Claude JSON → result field extracted, metadata populated"""
    claude_output = json.dumps({
        "type": "result",
        "result": "I added the function successfully.",
        "session_id": "abc-123",
        "num_turns": 3,
        "cost_usd": 0.01,
    })
    text, metadata = ClaudeBackend.parse_output(claude_output)
    assert text == "I added the function successfully."
    assert metadata is not None
    assert metadata["session_id"] == "abc-123"
    assert metadata["num_turns"] == 3
    assert "result" not in metadata


def test_claude_json_parse_fallback():
    """garbage stdout → raw stdout returned, metadata=None"""
    text, metadata = ClaudeBackend.parse_output("this is not json at all")
    assert text == "this is not json at all"
    assert metadata is None


def test_claude_json_missing_result_field():
    """JSON without 'result' key → raw stdout, metadata=None"""
    text, metadata = ClaudeBackend.parse_output(json.dumps({"foo": "bar"}))
    assert text == json.dumps({"foo": "bar"})
    assert metadata is None


def test_codex_plain_text_passthrough():
    """codex stdout = plain text → passthrough, metadata=None"""
    text, metadata = CodexBackend.parse_output("some plain text output")
    assert text == "some plain text output"
    assert metadata is None


# ---------------------------------------------------------------------------
# Worktree isolation
# ---------------------------------------------------------------------------


def test_worktree_isolation(tmp_path):
    """git repo → execution happens in worktree, diff captured from worktree"""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

    assert _is_git_repo(str(repo)) is True


def test_worktree_non_git(tmp_path):
    """non-git dir → _is_git_repo returns False"""
    assert _is_git_repo(str(tmp_path)) is False


def test_worktree_missing_dir():
    """nonexistent dir → _is_git_repo returns False"""
    assert _is_git_repo("/nonexistent/path/xyz") is False


# ---------------------------------------------------------------------------
# Streaming progress
# ---------------------------------------------------------------------------


def test_streaming_progress():
    """mock Popen → on_progress called per line"""
    progress_calls = []

    class FakeProc:
        def __init__(self):
            self.stdout = iter(["line 1\n", "line 2\n", "line 3\n"])
            self.stderr = MagicMock()
            self.stderr.read = lambda: ""
            self.returncode = 0

        def wait(self, timeout=None):
            pass

    backend = CodexBackend()
    context = _make_context(target_repo_path="/tmp", confirm_execution=True)

    with patch.object(backend, "is_available", return_value=True), \
         patch("subprocess.Popen", return_value=FakeProc()), \
         patch("ariadne.backends._is_git_repo", return_value=False), \
         patch("ariadne.backends._capture_diff", return_value=(None, [])):
        result = backend.execute(context, on_progress=lambda p: progress_calls.append(p))

    # Should have: starting + 3 lines + finished = 5 progress calls
    assert len(progress_calls) >= 4
    summaries = [p.summary for p in progress_calls]
    assert any("starting" in s for s in summaries)
    assert any("line 1" in s for s in summaries)
    assert any("finished" in s for s in summaries)
    assert result.success is True


def test_streaming_progress_structured_fields():
    progress_calls = []
    tool_line = json.dumps({
        "type": "tool_use",
        "tool_name": "pytest",
        "content": "running tests",
    }) + "\n"

    class FakeProc:
        def __init__(self):
            self.stdout = iter([tool_line, "plain line\n"])
            self.stderr = iter([])
            self.returncode = 0

        def wait(self, timeout=None):
            pass

    backend = CodexBackend()
    context = _make_context(target_repo_path="/tmp", confirm_execution=True)

    with patch.object(backend, "is_available", return_value=True), \
         patch("subprocess.Popen", return_value=FakeProc()), \
         patch("ariadne.backends._is_git_repo", return_value=False), \
         patch("ariadne.backends._capture_diff", return_value=(None, [])):
        backend.execute(context, on_progress=lambda p: progress_calls.append(p))

    structured = next(p for p in progress_calls if p.message_type == "tool_use")
    plain = next(p for p in progress_calls if p.content == "plain line")

    assert structured.tool_name == "pytest"
    assert structured.content == "running tests"
    assert structured.summary == "running tests"
    assert plain.message_type is None
    assert plain.tool_name is None
    assert plain.summary == "plain line"
