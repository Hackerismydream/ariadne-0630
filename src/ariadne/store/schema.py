"""SQLite schema used by StoreBase."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_machine (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('online', 'offline', 'draining', 'disabled')),
    version TEXT NOT NULL DEFAULT '',
    device_info TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at TEXT,
    max_concurrent_taskruns INTEGER NOT NULL DEFAULT 4,
    workspace_root TEXT NOT NULL DEFAULT '',
    repo_allowlist TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_capability (
    id TEXT PRIMARY KEY,
    runtime_machine_id TEXT NOT NULL REFERENCES runtime_machine(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    command_path TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    models_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL
        CHECK (status IN ('available', 'unavailable', 'degraded', 'disabled')),
    health_error TEXT,
    default_args_json TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    last_checked_at TEXT,
    UNIQUE(runtime_machine_id, provider, command_path)
);

CREATE TABLE IF NOT EXISTS runtime_lease (
    id TEXT PRIMARY KEY,
    taskrun_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    runtime_machine_id TEXT NOT NULL REFERENCES runtime_machine(id),
    runtime_capability_id TEXT NOT NULL REFERENCES runtime_capability(id),
    status TEXT NOT NULL
        CHECK (status IN ('active', 'released', 'expired', 'revoked')),
    lease_token TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    released_at TEXT,
    expires_at TEXT NOT NULL,
    revoke_reason TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_lease_one_active
    ON runtime_lease(taskrun_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS issue (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'backlog'
        CHECK (status IN ('backlog', 'todo', 'in_progress', 'done', 'cancelled')),
    assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
    assignee_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_timeline_event (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    taskrun_id TEXT REFERENCES task(id),
    runtime_lease_id TEXT REFERENCES runtime_lease(id),
    leader_decision_id TEXT,
    comment_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issue_timeline_issue_time
    ON issue_timeline_event(issue_id, created_at);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    squad_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'preparing', 'claimed', 'running', 'completed', 'failed', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    parent_task_id TEXT REFERENCES task(id) ON DELETE SET NULL,
    failure_reason TEXT
        CHECK (failure_reason IS NULL OR failure_reason IN
               ('agent_error', 'timeout', 'runtime_offline', 'runtime_recovery',
                'manual', 'policy_blocked', 'provider_error', 'test_failure',
                'routing_failure', 'llm_parse_failure')),
    dispatched_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    runtime_id TEXT,
    handoff_prompt TEXT,
    trace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_claim
    ON task(status, created_at) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS leader_decision (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    squad_id TEXT NOT NULL REFERENCES squad(id) ON DELETE CASCADE,
    leader_task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('action', 'no_action', 'failed', 'done')),
    reason TEXT NOT NULL DEFAULT '',
    delegation_payload_json TEXT NOT NULL DEFAULT '{}',
    created_taskrun_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leader_decision_issue_time
    ON leader_decision(issue_id, created_at);

CREATE TABLE IF NOT EXISTS benchmark_run (
    id TEXT PRIMARY KEY,
    suite_name TEXT NOT NULL,
    case_name TEXT NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    runtime_policy_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    artifact_dir TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_benchmark_run_suite_case
    ON benchmark_run(suite_name, case_name, started_at);

CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    task_id TEXT,
    event TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity_log(trace_id);

CREATE TABLE IF NOT EXISTS agent_profile (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    preferred_capabilities_json TEXT NOT NULL DEFAULT '[]',
    runtime_policy_json TEXT NOT NULL DEFAULT '{}',
    max_concurrent_taskruns INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    when_to_use TEXT NOT NULL DEFAULT '',
    prompt_snippet TEXT NOT NULL DEFAULT '',
    tools_allowed_json TEXT NOT NULL DEFAULT '[]',
    test_command TEXT,
    source_path TEXT,
    version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profile_skill (
    agent_profile_id TEXT NOT NULL REFERENCES agent_profile(id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_profile_id, skill_id)
);

CREATE TABLE IF NOT EXISTS agent (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    backends TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS squad (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    leader_id TEXT NOT NULL REFERENCES agent(id),
    instructions TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS squad_member (
    id TEXT PRIMARY KEY,
    squad_id TEXT NOT NULL REFERENCES squad(id) ON DELETE CASCADE,
    member_type TEXT NOT NULL DEFAULT 'agent',
    member_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    UNIQUE(squad_id, member_type, member_id)
);
"""
