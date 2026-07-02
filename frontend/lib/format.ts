import type {
  ActivityStreamEvent,
  IssueStatus,
  IssueTimelineStreamEvent,
  StatusTone,
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
