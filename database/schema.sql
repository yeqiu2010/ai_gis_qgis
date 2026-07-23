PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'qgis',
    title TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    qgis_project_path TEXT,
    layer_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    stage_name TEXT,
    stage_artifact TEXT,
    event_type TEXT,
    run_id TEXT
);

CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    usage_estimated INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE INDEX IF NOT EXISTS run_metrics_session_started
ON run_metrics(session_id, started_at);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tool_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT,
    result TEXT,
    duration_ms INTEGER,
    success INTEGER DEFAULT 1,
    error_message TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS layer_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    layer_id TEXT NOT NULL,
    layer_name TEXT NOT NULL,
    source_path TEXT,
    crs TEXT,
    feature_count INTEGER,
    action TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS code_execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_call_id INTEGER REFERENCES tool_call_log(id) ON DELETE SET NULL,
    code TEXT NOT NULL,
    workspace_dir TEXT NOT NULL,
    expected_outputs TEXT,
    outputs TEXT,
    stdout TEXT,
    stderr TEXT,
    exit_code INTEGER,
    duration_ms INTEGER,
    success INTEGER DEFAULT 0,
    error_message TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_name TEXT,
    source_record_id INTEGER,
    stage_name TEXT,
    error_code TEXT NOT NULL,
    error_message TEXT NOT NULL,
    cause TEXT,
    retryable INTEGER DEFAULT 0,
    attempt INTEGER,
    generated_code TEXT,
    context_json TEXT,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS failure_log_session_time
ON failure_log(session_id, timestamp);

CREATE INDEX IF NOT EXISTS failure_log_error_code
ON failure_log(error_code);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    plan_version INTEGER NOT NULL DEFAULT 1,
    summary TEXT,
    finalization_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS task_runs_session_updated
ON task_runs(session_id, updated_at);

CREATE TABLE IF NOT EXISTS plan_steps (
    id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    skill_name TEXT,
    instruction TEXT NOT NULL,
    dependencies TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    inputs TEXT NOT NULL DEFAULT '{}',
    outputs TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(task_id, id)
);

CREATE INDEX IF NOT EXISTS plan_steps_task_position
ON plan_steps(task_id, position);

CREATE TABLE IF NOT EXISTS skill_invocations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    step_id TEXT,
    skill_name TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'main_loop',
    status TEXT NOT NULL DEFAULT 'running',
    arguments TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL,
    ended_at REAL
);

CREATE INDEX IF NOT EXISTS skill_invocations_task_started
ON skill_invocations(task_id, started_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    step_id TEXT,
    artifact_type TEXT NOT NULL,
    name TEXT,
    uri TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    producer TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS artifacts_task_created
ON artifacts(task_id, created_at);
