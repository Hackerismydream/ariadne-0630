import { issueStatusDisplay, taskStatusDisplay } from "../lib/format";
import type { IssueStatus, TaskStatus } from "../lib/types";

export function IssueStatusBadge({ status }: { status: IssueStatus }) {
  const display = issueStatusDisplay(status);
  return (
    <span className={`status-${display.tone}`} aria-label={`Issue status ${status}`}>
      {display.label}
      {display.cursor ? <span className="blink">{display.cursor}</span> : null}
    </span>
  );
}

export function TaskStatusBadge({ status }: { status: TaskStatus }) {
  const display = taskStatusDisplay(status);
  return (
    <span className={`status-${display.tone}`} aria-label={`Task status ${status}`}>
      {display.label}
      {display.cursor ? <span className="blink">{display.cursor}</span> : null}
    </span>
  );
}
