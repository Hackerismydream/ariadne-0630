import test from "node:test";
import assert from "node:assert/strict";

import {
  asciiProgress,
  eventToTranscriptLine,
  issueStatusDisplay,
  taskStatusDisplay,
} from "../lib/format";
import { BACKEND_OPTIONS } from "../lib/api";

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
});

test("uses backend names registered by the Python runtime", () => {
  assert.deepEqual(BACKEND_OPTIONS, ["dry-run", "codex", "claude-code"]);
  assert.equal(BACKEND_OPTIONS.includes("claude" as never), false);
});
