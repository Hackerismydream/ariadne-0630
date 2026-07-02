export type IssueStatus =
  | "backlog"
  | "todo"
  | "in_progress"
  | "done"
  | "failed"
  | "cancelled";

export type TaskStatus =
  | "queued"
  | "preparing"
  | "claimed"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type StatusTone = "primary" | "secondary" | "muted" | "error";

export type IssueSummary = {
  id: string;
  title: string;
  description: string;
  status: IssueStatus;
  assignee_type: "agent" | "squad";
  assignee_id: string;
  taskrun_count: number;
  active_taskrun_count: number;
  latest_event_at: string | null;
  created_at: string;
};

export type TaskRun = {
  id: string;
  issue_id: string;
  agent_profile_id: string;
  squad_id: string | null;
  status: TaskStatus;
  attempt: number;
  max_attempts: number;
  parent_taskrun_id: string | null;
  failure_reason: string | null;
  trace_id: string | null;
  duration_seconds: number | null;
  diff: string | null;
  changed_files: string[];
  result: Record<string, unknown>;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type IssueDetail = IssueSummary & {
  taskruns: TaskRun[];
  diff: string | null;
  changed_files: string[];
};

export type RunResult = {
  mode: "default" | "squad";
  backend: "dry-run" | "codex" | "claude-code";
  detached: boolean;
  completed: boolean;
  runtime_id: string;
  target_repo: string;
  issue_id: string | null;
  squad_id: string | null;
  iterations: number;
  task_results: Array<{
    title: string;
    issue_id: string;
    taskrun_id: string;
    agent_id: string;
    agent_name: string;
    status: TaskStatus;
    duration_seconds: number | null;
    diff: string | null;
    changed_files: string[];
    stdout: string;
    error: string | null;
  }>;
};

export type CancelIssueResult = {
  issue: IssueSummary;
  cancelled_taskrun_ids: string[];
};

export type ActivityStreamEvent = {
  type: "activity";
  id: string;
  created_at: string;
  trace_id?: string;
  task_id: string | null;
  event: string;
  details?: Record<string, unknown> | null;
  message_type: string | null;
  tool_name: string | null;
  content: string | null;
};

export type IssueTimelineStreamEvent = {
  type: "issue_timeline";
  id: string;
  created_at: string;
  issue_id: string;
  event_type: string;
  actor_type?: string;
  actor_id?: string | null;
  taskrun_id?: string | null;
  runtime_lease_id?: string | null;
  leader_decision_id?: string | null;
  comment_id?: string | null;
  payload: Record<string, unknown>;
};
