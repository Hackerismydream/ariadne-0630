import type {
  ActivityStreamEvent,
  IssueStatus,
  IssueTimelineStreamEvent,
  StatusTone,
  TaskRun,
  TaskStatus,
} from "./types";

export type StatusDisplay = {
  label: string;
  tone: StatusTone;
  cursor: string;
};

const ISSUE_STATUS: Record<IssueStatus, StatusDisplay> = {
  backlog: { label: "[BACKLOG]", tone: "muted", cursor: "" },
  todo: { label: "[TODO]", tone: "secondary", cursor: "" },
  in_progress: { label: "[IN-PROG]", tone: "primary", cursor: "_" },
  done: { label: "[DONE]", tone: "primary", cursor: "" },
  failed: { label: "[ERR]", tone: "error", cursor: "" },
  cancelled: { label: "[CANCEL]", tone: "muted", cursor: "" },
};

const TASK_STATUS: Record<TaskStatus, StatusDisplay> = {
  queued: { label: "[QUEUED]", tone: "muted", cursor: "" },
  preparing: { label: "[PREP]", tone: "secondary", cursor: "" },
  claimed: { label: "[CLAIMED]", tone: "secondary", cursor: "" },
  running: { label: "[RUNNING]", tone: "primary", cursor: "_" },
  completed: { label: "[OK]", tone: "primary", cursor: "" },
  failed: { label: "[ERR]", tone: "error", cursor: "" },
  cancelled: { label: "[CANCELLED]", tone: "muted", cursor: "" },
};

export function issueStatusDisplay(status: IssueStatus): StatusDisplay {
  return ISSUE_STATUS[status] ?? { label: `[${status.toUpperCase()}]`, tone: "muted", cursor: "" };
}

export function taskStatusDisplay(status: TaskStatus): StatusDisplay {
  return TASK_STATUS[status] ?? { label: `[${status.toUpperCase()}]`, tone: "muted", cursor: "" };
}

export function asciiProgress(done: number, total: number, width = 20): string {
  if (total <= 0) {
    return `[${".".repeat(width)}] 0%`;
  }
  const ratio = Math.max(0, Math.min(1, done / total));
  const filled = Math.round(ratio * width);
  const percent = Math.round(ratio * 100);
  return `[${"|".repeat(filled)}${".".repeat(width - filled)}] ${percent}%`;
}

export function eventToTranscriptLine(
  event: ActivityStreamEvent | IssueTimelineStreamEvent,
): string {
  if (event.type === "activity") {
    return activityLine(event);
  }
  return issueTimelineLine(event);
}

function activityLine(event: ActivityStreamEvent): string {
  if (event.event === "backend_heartbeat") {
    return backendHeartbeatLine(event);
  }
  if (event.message_type === "tool_use") {
    return `$ ${event.tool_name ?? "tool"} ${event.content ?? ""}`.trim();
  }
  if (event.message_type === "thinking") {
    return `~ ${event.content ?? event.event}`;
  }
  if (event.message_type === "tool_result") {
    return `  ${event.content ?? event.event}`;
  }
  return `> ${event.event.toUpperCase()}`;
}

export function retryTreeLines(taskruns: TaskRun[]): string[] {
  const sorted = [...taskruns].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const byId = new Map(sorted.map((taskrun) => [taskrun.id, taskrun]));
  const children = new Map<string, TaskRun[]>();
  for (const taskrun of sorted) {
    if (!taskrun.parent_taskrun_id || !byId.has(taskrun.parent_taskrun_id)) {
      continue;
    }
    children.set(taskrun.parent_taskrun_id, [
      ...(children.get(taskrun.parent_taskrun_id) ?? []),
      taskrun,
    ]);
  }

  const roots = sorted.filter(
    (taskrun) => !taskrun.parent_taskrun_id || !byId.has(taskrun.parent_taskrun_id),
  );
  const lines: string[] = [];
  const visit = (taskrun: TaskRun, prefix: string, connector: string) => {
    lines.push(`${prefix}${connector}${taskrunSummaryLine(taskrun)}`);
    const childTaskruns = children.get(taskrun.id) ?? [];
    childTaskruns.forEach((child, index) => {
      const isLast = index === childTaskruns.length - 1;
      visit(child, `${prefix}${connector ? (isLast ? "  " : "│ ") : ""}`, isLast ? "└─ " : "├─ ");
    });
  };
  roots.forEach((root) => visit(root, "", ""));
  return lines;
}

export function taskrunSummaryLine(taskrun: TaskRun): string {
  const parts = [
    taskrun.id,
    `attempt ${taskrun.attempt}/${taskrun.max_attempts}`,
    taskStatusDisplay(taskrun.status).label,
    `elapsed=${formatSeconds(taskrun.duration_seconds)}`,
  ];
  if (taskrun.failure_reason) {
    parts.push(`failure_reason=${taskrun.failure_reason}`);
  }
  return parts.join(" ");
}

export function taskrunDiffExplanation(taskrun: TaskRun): string | null {
  if (taskrun.diff || taskrun.changed_files.length > 0) {
    return null;
  }
  if (taskrun.status !== "failed") {
    return "no diff captured yet";
  }
  if (taskrun.failure_reason === "timeout") {
    const timeoutSeconds = taskrunTimeoutSeconds(taskrun);
    return timeoutSeconds
      ? `provider timed out after ${timeoutSeconds}s`
      : "provider timed out before diff capture";
  }
  return "no diff captured because execution failed";
}

function backendHeartbeatLine(event: ActivityStreamEvent): string {
  const details = event.details ?? {};
  const elapsed = numericDetail(details, "elapsed_seconds");
  const timeout = numericDetail(details, "timeout_seconds");
  const backend = stringDetail(details, "backend") ?? "backend";
  const progress = elapsed !== null && timeout !== null
    ? ` ${asciiProgress(elapsed, timeout)}`
    : "";
  const budget = elapsed !== null && timeout !== null
    ? ` elapsed ${elapsed}s / ${timeout}s`
    : "";
  return `> ${backend.toUpperCase()} HEARTBEAT${budget}${progress}`;
}

function issueTimelineLine(event: IssueTimelineStreamEvent): string {
  const suffix = Object.entries(event.payload ?? {})
    .map(([key, value]) => `${key}=${formatPayloadValue(value)}`)
    .join(" ");
  return `> ${event.event_type.toUpperCase()}${suffix ? ` ${suffix}` : ""}`;
}

function formatPayloadValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  const json = JSON.stringify(value);
  return json.length > 180 ? `${json.slice(0, 177)}...` : json;
}

function formatSeconds(seconds: number | null): string {
  if (seconds === null) {
    return "n/a";
  }
  return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(2)}s`;
}

function taskrunTimeoutSeconds(taskrun: TaskRun): number | null {
  const typedTaskrun = taskrun as TaskRun & { timeout_seconds?: number | null };
  if (typeof typedTaskrun.timeout_seconds === "number") {
    return typedTaskrun.timeout_seconds;
  }
  const stderr = typeof taskrun.result.stderr === "string" ? taskrun.result.stderr : null;
  for (const text of [taskrun.error, stderr]) {
    if (!text) {
      continue;
    }
    const match = /timed out after (\d+(?:\.\d+)?)s/.exec(text);
    if (match) {
      return Number(match[1]);
    }
  }
  return null;
}

function numericDetail(details: Record<string, unknown>, key: string): number | null {
  const value = details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringDetail(details: Record<string, unknown>, key: string): string | null {
  const value = details[key];
  return typeof value === "string" ? value : null;
}
