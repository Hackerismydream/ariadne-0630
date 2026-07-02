import test from "node:test";
import assert from "node:assert/strict";

import {
  asciiProgress,
  eventToTranscriptLine,
  hasCancellableTaskruns,
  issueStatusDisplay,
  retryTreeLines,
  taskrunDiffExplanation,
  taskStatusDisplay,
} from "../lib/format";
import { BACKEND_OPTIONS } from "../lib/api";
import type { TaskRun } from "../lib/types";

test("maps backend task and issue statuses to terminal status codes", () => {
  assert.deepEqual(taskStatusDisplay("running"), {
    label: "[RUNNING]",
    tone: "primary",
    cursor: "_",
  });
  assert.deepEqual(taskStatusDisplay("failed"), {
    label: "[ERR]",
    tone: "error",
    cursor: "",
  });
  assert.deepEqual(issueStatusDisplay("in_progress"), {
    label: "[IN-PROG]",
    tone: "primary",
    cursor: "_",
  });
  assert.deepEqual(issueStatusDisplay("failed"), {
    label: "[ERR]",
    tone: "error",
    cursor: "",
  });
});

test("renders deterministic ASCII progress bars", () => {
  assert.equal(asciiProgress(3, 5, 10), "[||||||....] 60%");
  assert.equal(asciiProgress(0, 0, 10), "[..........] 0%");
});

test("turns structured SSE events into transcript lines", () => {
  assert.equal(
    eventToTranscriptLine({
      type: "activity",
      id: "act-1",
      created_at: "2026-07-02T00:00:00Z",
      task_id: "taskrun-1",
      event: "progress_reported",
      message_type: "tool_use",
      tool_name: "apply_patch",
      content: "editing file",
    }),
    "$ apply_patch editing file",
  );
  assert.equal(
    eventToTranscriptLine({
      type: "issue_timeline",
      id: "evt-1",
      created_at: "2026-07-02T00:00:01Z",
      issue_id: "issue-1",
      event_type: "taskrun_queued",
      payload: { status: "queued", attempt: 1 },
    }),
    "> TASKRUN_QUEUED status=queued attempt=1",
  );
  assert.equal(
    eventToTranscriptLine({
      type: "issue_timeline",
      id: "evt-2",
      created_at: "2026-07-02T00:00:02Z",
      issue_id: "issue-1",
      event_type: "taskrun_completed",
      payload: { result: { changed_files: ["src/app.py"], ok: true } },
    }),
    '> TASKRUN_COMPLETED result={"changed_files":["src/app.py"],"ok":true}',
  );
  assert.match(
    eventToTranscriptLine({
      type: "issue_timeline",
      id: "evt-3",
      created_at: "2026-07-02T00:00:03Z",
      issue_id: "issue-1",
      event_type: "taskrun_completed",
      payload: {
        result: {
          stdout: "x".repeat(240),
          command: "dry-run",
        },
      },
    }),
    /\.\.\.$/,
  );
  assert.equal(
    eventToTranscriptLine({
      type: "activity",
      id: "act-2",
      created_at: "2026-07-02T00:00:04Z",
      task_id: "taskrun-1",
      event: "backend_heartbeat",
      details: {
        backend: "codex",
        elapsed_seconds: 185,
        timeout_seconds: 300,
      },
      message_type: null,
      tool_name: null,
      content: null,
    }),
    "> CODEX HEARTBEAT elapsed 185s / 300s [||||||||||||........] 62%",
  );
});

test("renders retry chains with failure reasons", () => {
  const first = makeTaskrun({
    id: "taskrun-a",
    attempt: 1,
    parent_taskrun_id: null,
    created_at: "2026-07-02T00:00:00Z",
  });
  const retry = makeTaskrun({
    id: "taskrun-b",
    attempt: 2,
    parent_taskrun_id: "taskrun-a",
    created_at: "2026-07-02T00:00:01Z",
  });

  assert.deepEqual(retryTreeLines([retry, first]), [
    "taskrun-a attempt 1/2 [ERR] elapsed=300s failure_reason=timeout",
    "└─ taskrun-b attempt 2/2 [ERR] elapsed=300s failure_reason=timeout",
  ]);
});

test("explains missing diffs for failed taskruns", () => {
  assert.equal(
    taskrunDiffExplanation(makeTaskrun({ error: "execution timed out after 300s" })),
    "provider timed out after 300s",
  );
  assert.equal(
    taskrunDiffExplanation(makeTaskrun({ failure_reason: "agent_error", error: "boom" })),
    "no diff captured because execution failed",
  );
});

test("detects taskruns that can be cancelled", () => {
  assert.equal(hasCancellableTaskruns([makeTaskrun({ status: "running" })]), true);
  assert.equal(hasCancellableTaskruns([makeTaskrun({ status: "queued" })]), true);
  assert.equal(hasCancellableTaskruns([makeTaskrun({ status: "failed" })]), false);
  assert.equal(hasCancellableTaskruns([makeTaskrun({ status: "completed" })]), false);
});

test("uses backend names registered by the Python runtime", () => {
  assert.deepEqual(BACKEND_OPTIONS, ["dry-run", "codex", "claude-code"]);
  assert.equal(BACKEND_OPTIONS.includes("claude" as never), false);
});

function makeTaskrun(overrides: Partial<TaskRun> = {}): TaskRun {
  return {
    id: "taskrun-a",
    issue_id: "issue-1",
    agent_profile_id: "agent-1",
    squad_id: null,
    status: "failed",
    attempt: 1,
    max_attempts: 2,
    parent_taskrun_id: null,
    failure_reason: "timeout",
    trace_id: "trace-1",
    duration_seconds: 300,
    diff: null,
    changed_files: [],
    result: {},
    error: "execution timed out after 300s",
    created_at: "2026-07-02T00:00:00Z",
    started_at: "2026-07-02T00:00:00Z",
    completed_at: "2026-07-02T00:05:00Z",
    ...overrides,
  };
}
