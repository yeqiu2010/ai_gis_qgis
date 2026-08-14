"""SQLite-backed conversation and audit storage."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import DEFAULT_DB_PATH
from .migrations import run_migrations


@dataclass(frozen=True)
class SessionRecord:
    id: str
    title: str | None
    started_at: float
    message_count: int
    model: str | None = None


class SessionDB:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema_path = Path(__file__).with_name("schema.sql")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            run_migrations(connection, self.schema_path)

    def create_session(
        self,
        *,
        title: str | None = None,
        model: str | None = None,
        source: str = "qgis",
        qgis_project_path: str | None = None,
        layer_count: int = 0,
    ) -> SessionRecord:
        session_id = str(uuid.uuid4())
        started_at = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sessions (
                    id, source, title, model, started_at, qgis_project_path, layer_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    source,
                    title or "新会话",
                    model,
                    started_at,
                    qgis_project_path,
                    layer_count,
                ),
            )
        return SessionRecord(session_id, title or "新会话", started_at, 0, model)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, started_at, message_count, model
                FROM sessions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        event_type: str | None = None,
        finish_reason: str | None = None,
        stage_name: str | None = None,
        stage_artifact: dict[str, Any] | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_name: str | None = None,
    ) -> int:
        timestamp = time.time()
        artifact_json = json.dumps(stage_artifact, ensure_ascii=False) if stage_artifact else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, timestamp, finish_reason,
                    stage_name, stage_artifact, event_type, run_id,
                    tool_call_id, tool_calls, tool_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    timestamp,
                    finish_reason,
                    stage_name,
                    artifact_json,
                    event_type,
                    run_id,
                    tool_call_id,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    tool_name,
                ),
            )
            connection.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a message id")
        return int(cursor.lastrowid)

    def get_messages(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.id, m.role, m.content, m.timestamp, m.event_type,
                    m.finish_reason, m.run_id,
                    r.duration_ms, r.input_tokens, r.output_tokens,
                    r.total_tokens, r.llm_calls, r.usage_estimated,
                    r.status AS metrics_status
                FROM messages AS m
                LEFT JOIN run_metrics AS r ON r.run_id = m.run_id
                WHERE m.session_id = ?
                  AND COALESCE(m.event_type, '') NOT IN ('tool_call', 'tool_result')
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def start_run_metrics(
        self,
        run_id: str,
        session_id: str,
        *,
        started_at: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO run_metrics(run_id, session_id, started_at, status)
                VALUES (?, ?, ?, 'running')
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, session_id, started_at),
            )

    def update_run_metrics(
        self,
        run_id: str,
        *,
        duration_ms: int,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        llm_calls: int,
        usage_estimated: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE run_metrics
                SET duration_ms = ?, input_tokens = ?, output_tokens = ?,
                    total_tokens = ?, llm_calls = ?, usage_estimated = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    max(0, int(duration_ms)),
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0, int(total_tokens)),
                    max(0, int(llm_calls)),
                    1 if usage_estimated else 0,
                    run_id,
                ),
            )

    def finish_run_metrics(
        self,
        run_id: str,
        *,
        ended_at: float,
        duration_ms: int,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        llm_calls: int,
        usage_estimated: bool,
        status: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT session_id, status FROM run_metrics WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                UPDATE run_metrics
                SET ended_at = ?, duration_ms = ?, input_tokens = ?,
                    output_tokens = ?, total_tokens = ?, llm_calls = ?,
                    usage_estimated = ?, status = ?
                WHERE run_id = ?
                """,
                (
                    ended_at,
                    max(0, int(duration_ms)),
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0, int(total_tokens)),
                    max(0, int(llm_calls)),
                    1 if usage_estimated else 0,
                    status,
                    run_id,
                ),
            )
            if row["status"] == "running":
                connection.execute(
                    """
                    UPDATE sessions
                    SET input_tokens = COALESCE(input_tokens, 0) + ?,
                        output_tokens = COALESCE(output_tokens, 0) + ?
                    WHERE id = ?
                    """,
                    (
                        max(0, int(input_tokens)),
                        max(0, int(output_tokens)),
                        row["session_id"],
                    ),
                )

    def get_conversation_messages(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return model-visible history, preserving structured tool messages."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, timestamp, event_type, finish_reason,
                       tool_call_id, tool_calls, tool_name
                FROM messages
                WHERE session_id = ?
                  AND role IN ('user', 'assistant', 'tool')
                  AND COALESCE(event_type, '') NOT IN ('confirm_request', 'process')
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return self._decode_conversation_rows(reversed(rows))

    def get_context_messages(
        self,
        session_id: str,
        *,
        historical_limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return compact cross-task history plus the complete active task chain.

        Full tool exchanges remain in the audit store. A new user message is a
        task boundary: older completed tool calls are recalled through compact
        memory/session search, while every model-visible message from the latest
        user request onward is retained for tool-call and confirmation recovery.
        """
        with self._connect() as connection:
            latest_user = connection.execute(
                """
                SELECT MAX(id) AS id
                FROM messages
                WHERE session_id = ? AND role = 'user'
                """,
                (session_id,),
            ).fetchone()
            latest_user_id = int(latest_user["id"] or 0) if latest_user else 0
            if latest_user_id <= 0:
                return []
            historical = connection.execute(
                """
                SELECT id, role, content, timestamp, event_type, finish_reason,
                       tool_call_id, tool_calls, tool_name
                FROM messages
                WHERE session_id = ?
                  AND id < ?
                  AND role IN ('user', 'assistant')
                  AND COALESCE(event_type, '') NOT IN (
                      'confirm_request', 'process', 'tool_call', 'tool_result'
                  )
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, latest_user_id, max(0, int(historical_limit))),
            ).fetchall()
            active = connection.execute(
                """
                SELECT id, role, content, timestamp, event_type, finish_reason,
                       tool_call_id, tool_calls, tool_name
                FROM messages
                WHERE session_id = ?
                  AND id >= ?
                  AND role IN ('user', 'assistant', 'tool')
                  AND COALESCE(event_type, '') NOT IN ('confirm_request', 'process')
                ORDER BY id
                """,
                (session_id, latest_user_id),
            ).fetchall()
        rows = [*reversed(historical), *active]
        return self._decode_conversation_rows(rows)

    @staticmethod
    def _decode_conversation_rows(rows) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            raw_calls = item.get("tool_calls")
            if raw_calls:
                try:
                    item["tool_calls"] = json.loads(raw_calls)
                except (json.JSONDecodeError, TypeError):
                    item["tool_calls"] = []
            else:
                item["tool_calls"] = []
            result.append(item)
        return result

    def search_messages(
        self, query: str, *, session_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        fts_query = self._fts_literal_query(query)
        with self._connect() as connection:
            if session_id:
                rows = connection.execute(
                    """
                    SELECT m.id, m.session_id, m.role, m.content, m.timestamp, rank
                    FROM messages_fts fts
                    JOIN messages m ON m.id = fts.rowid
                    WHERE messages_fts MATCH ? AND fts.session_id = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, session_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT m.id, m.session_id, m.role, m.content, m.timestamp, rank
                    FROM messages_fts fts
                    JOIN messages m ON m.id = fts.rowid
                    WHERE messages_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _fts_literal_query(query: str) -> str:
        """Treat user/model search text as terms, not raw FTS5 syntax."""
        terms = [term for term in query.split() if term]
        if not terms:
            return '""'
        return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

    def get_recent_tool_calls(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tool_name, arguments, result, success, error_message, timestamp
                FROM tool_call_log
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        values = []
        for row in reversed(rows):
            item = dict(row)
            for key in ("arguments", "result"):
                try:
                    item[key] = json.loads(item[key] or "{}")
                except json.JSONDecodeError:
                    item[key] = item[key] or {}
            values.append(item)
        return values

    def get_recent_stage_artifacts(self, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stage_name, content, stage_artifact, timestamp
                FROM messages
                WHERE session_id = ?
                  AND event_type = 'stage_artifact'
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        values = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["stage_artifact"] = json.loads(item["stage_artifact"] or "{}")
            except json.JSONDecodeError:
                item["stage_artifact"] = {}
            values.append(item)
        return values

    def get_state(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO state_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def delete_state(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM state_meta WHERE key = ?", (key,))

    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        duration_ms: int,
        message_id: int | None = None,
    ) -> int:
        success = 1 if result.get("success", True) else 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO tool_call_log (
                    session_id, message_id, tool_name, arguments, result,
                    duration_ms, success, error_message, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    duration_ms,
                    success,
                    result.get("error"),
                    time.time(),
                ),
            )
            connection.execute(
                "UPDATE sessions SET tool_call_count = tool_call_count + 1 WHERE id = ?",
                (session_id,),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a tool call id")
        record_id = int(cursor.lastrowid)
        if not success:
            self.log_failure(
                session_id,
                source_type="tool_call",
                source_name=tool_name,
                source_record_id=record_id,
                stage_name=str(arguments.get("stage_name") or "") or None,
                error_message=str(result.get("error") or "工具调用失败"),
                generated_code=self._failure_generated_code(tool_name, arguments),
                context={"arguments": arguments, "result": result},
                preflight_failed=bool(result.get("preflight_failed")),
            )
        return record_id

    @staticmethod
    def _failure_generated_code(tool_name: str, arguments: dict[str, Any]) -> str | None:
        if tool_name == "execute_gis_code":
            return str(arguments.get("code") or "") or None
        if tool_name == "record_pipeline_stage":
            artifact = arguments.get("artifact")
            if isinstance(artifact, dict):
                return str(artifact.get("code") or "") or None
        return None

    def log_code_execution(
        self,
        session_id: str,
        *,
        code: str,
        workspace_dir: str,
        expected_outputs: list[dict[str, Any]],
        outputs: list[dict[str, Any]],
        stdout: str,
        stderr: str,
        exit_code: int | None,
        duration_ms: int,
        success: bool,
        error_message: str | None = None,
        tool_call_id: int | None = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO code_execution_log (
                    session_id, tool_call_id, code, workspace_dir, expected_outputs,
                    outputs, stdout, stderr, exit_code, duration_ms, success,
                    error_message, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    tool_call_id,
                    code,
                    workspace_dir,
                    json.dumps(expected_outputs, ensure_ascii=False),
                    json.dumps(outputs, ensure_ascii=False),
                    stdout,
                    stderr,
                    exit_code,
                    duration_ms,
                    1 if success else 0,
                    error_message,
                    time.time(),
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a code execution id")
        record_id = int(cursor.lastrowid)
        if not success:
            self.log_failure(
                session_id,
                source_type="code_execution",
                source_name="execute_gis_code",
                source_record_id=record_id,
                error_message=error_message or stderr or "代码执行失败",
                generated_code=code,
                context={
                    "workspace_dir": workspace_dir,
                    "expected_outputs": expected_outputs,
                    "outputs": outputs,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                },
            )
        return record_id

    def log_failure(
        self,
        session_id: str,
        *,
        source_type: str,
        error_message: str,
        source_name: str | None = None,
        source_record_id: int | None = None,
        stage_name: str | None = None,
        generated_code: str | None = None,
        context: dict[str, Any] | None = None,
        attempt: int | None = None,
        preflight_failed: bool = False,
    ) -> int:
        from ..backend.failure_analysis import classify_failure

        classification = classify_failure(error_message, preflight_failed=preflight_failed)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO failure_log (
                    session_id, source_type, source_name, source_record_id, stage_name,
                    error_code, error_message, cause, retryable, attempt,
                    generated_code, context_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    source_type,
                    source_name,
                    source_record_id,
                    stage_name,
                    classification["error_code"],
                    error_message,
                    classification["cause"],
                    1 if classification["retryable"] else 0,
                    attempt,
                    generated_code,
                    json.dumps(context or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a failure log id")
        return int(cursor.lastrowid)

    def get_failure_records(
        self, *, session_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM failure_log"
        parameters: list[Any] = []
        if session_id:
            query += " WHERE session_id = ?"
            parameters.append(session_id)
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(max(1, limit))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        records = []
        for row in reversed(rows):
            record = dict(row)
            try:
                record["context"] = json.loads(record.pop("context_json") or "{}")
            except json.JSONDecodeError:
                record["context"] = {}
                record.pop("context_json", None)
            records.append(record)
        return records

    def export_failure_records(
        self, output_path: Path | str, *, session_id: str | None = None
    ) -> Path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        records = self.get_failure_records(session_id=session_id, limit=1_000_000)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def create_task(
        self,
        session_id: str,
        objective: str,
        *,
        status: str = "draft",
        task_id: str | None = None,
    ) -> str:
        task_id = task_id or str(uuid.uuid4())
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO task_runs(
                    id, session_id, objective, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, objective, status, now, now),
            )
            connection.execute(
                """
                INSERT INTO state_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"{session_id}:active_task", task_id),
            )
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (task_id,)
            ).fetchone()
        return self._decode_task_row(row) if row else None

    def get_active_task(self, session_id: str) -> dict[str, Any] | None:
        task_id = self.get_state(f"{session_id}:active_task")
        if task_id:
            task = self.get_task(task_id)
            if task is not None:
                return task
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_runs
                WHERE session_id = ? AND status IN (
                    'draft', 'running', 'waiting_for_user', 'waiting_confirmation'
                )
                ORDER BY updated_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        task = self._decode_task_row(row)
        self.set_state(f"{session_id}:active_task", str(task["id"]))
        return task

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        objective: str | None = None,
        summary: str | None = None,
        finalization: dict[str, Any] | None = None,
        increment_plan_version: bool = False,
    ) -> None:
        assignments = ["updated_at = ?"]
        parameters: list[Any] = [time.time()]
        if status is not None:
            assignments.append("status = ?")
            parameters.append(status)
            if status in {"completed", "failed", "cancelled"}:
                assignments.append("completed_at = ?")
                parameters.append(time.time())
        if objective is not None:
            assignments.append("objective = ?")
            parameters.append(objective)
        if summary is not None:
            assignments.append("summary = ?")
            parameters.append(summary)
        if finalization is not None:
            assignments.append("finalization_json = ?")
            parameters.append(json.dumps(finalization, ensure_ascii=False))
        if increment_plan_version:
            assignments.append("plan_version = plan_version + 1")
        parameters.append(task_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE task_runs SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )

    def replace_plan_steps(self, task_id: str, steps: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM plan_steps WHERE task_id = ?", (task_id,))
            for position, step in enumerate(steps, start=1):
                connection.execute(
                    """
                    INSERT INTO plan_steps(
                        id, task_id, position, skill_name, instruction, dependencies,
                        status, inputs, outputs, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(step["id"]),
                        task_id,
                        int(step.get("position") or position),
                        str(step.get("skill_name") or "") or None,
                        str(step.get("instruction") or ""),
                        json.dumps(step.get("dependencies") or [], ensure_ascii=False),
                        str(step.get("status") or "pending"),
                        json.dumps(step.get("inputs") or {}, ensure_ascii=False),
                        json.dumps(step.get("outputs") or {}, ensure_ascii=False),
                        str(step.get("error") or "") or None,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE task_runs
                SET status = 'running', plan_version = plan_version + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
            )

    def get_plan_steps(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_steps WHERE task_id = ? ORDER BY position, id",
                (task_id,),
            ).fetchall()
        return [self._decode_plan_step(row) for row in rows]

    def get_plan_step(self, task_id: str, step_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM plan_steps WHERE task_id = ? AND id = ?",
                (task_id, step_id),
            ).fetchone()
        return self._decode_plan_step(row) if row else None

    def update_plan_step(
        self,
        task_id: str,
        step_id: str,
        *,
        status: str | None = None,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        instruction: str | None = None,
        skill_name: str | None = None,
    ) -> bool:
        assignments = ["updated_at = ?"]
        parameters: list[Any] = [time.time()]
        for column, value in (
            ("status", status),
            ("instruction", instruction),
            ("skill_name", skill_name),
            ("error", error),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        if inputs is not None:
            assignments.append("inputs = ?")
            parameters.append(json.dumps(inputs, ensure_ascii=False))
        if outputs is not None:
            assignments.append("outputs = ?")
            parameters.append(json.dumps(outputs, ensure_ascii=False))
        parameters.extend([task_id, step_id])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE plan_steps SET {', '.join(assignments)} "
                "WHERE task_id = ? AND id = ?",
                parameters,
            )
            connection.execute(
                "UPDATE task_runs SET updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )
        return cursor.rowcount > 0

    def start_skill_invocation(
        self,
        task_id: str,
        skill_name: str,
        *,
        step_id: str | None = None,
        execution_mode: str = "main_loop",
        arguments: dict[str, Any] | None = None,
    ) -> str:
        invocation_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO skill_invocations(
                    id, task_id, step_id, skill_name, execution_mode, status,
                    arguments, result, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, '{}', ?)
                """,
                (
                    invocation_id,
                    task_id,
                    step_id,
                    skill_name,
                    execution_mode,
                    json.dumps(arguments or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
        return invocation_id

    def finish_skill_invocation(
        self,
        invocation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE skill_invocations
                SET status = ?, result = ?, ended_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    time.time(),
                    invocation_id,
                ),
            )

    def list_skill_invocations(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_invocations WHERE task_id = ? ORDER BY started_at",
                (task_id,),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["arguments"] = self._decode_json(item.get("arguments"), {})
            item["result"] = self._decode_json(item.get("result"), {})
            values.append(item)
        return values

    def register_artifact(
        self,
        task_id: str,
        artifact_type: str,
        *,
        step_id: str | None = None,
        name: str | None = None,
        uri: str | None = None,
        payload: dict[str, Any] | None = None,
        producer: str | None = None,
        verified: bool = False,
        artifact_id: str | None = None,
    ) -> str:
        artifact_id = artifact_id or str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, task_id, step_id, artifact_type, name, uri, payload,
                    producer, verified, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    step_id,
                    artifact_type,
                    name,
                    uri,
                    json.dumps(payload or {}, ensure_ascii=False),
                    producer,
                    1 if verified else 0,
                    time.time(),
                ),
            )
        return artifact_id

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._decode_json(item.get("payload"), {})
            item["verified"] = bool(item.get("verified"))
            values.append(item)
        return values

    def get_task_state(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        return {
            **task,
            "steps": self.get_plan_steps(task_id),
            "artifacts": self.list_artifacts(task_id),
            "skill_invocations": self.list_skill_invocations(task_id),
        }

    @staticmethod
    def _decode_json(value: Any, default: Any) -> Any:
        try:
            return json.loads(value) if value else default
        except (json.JSONDecodeError, TypeError):
            return default

    @classmethod
    def _decode_task_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["finalization"] = cls._decode_json(item.pop("finalization_json", None), {})
        return item

    @classmethod
    def _decode_plan_step(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["dependencies"] = cls._decode_json(item.get("dependencies"), [])
        item["inputs"] = cls._decode_json(item.get("inputs"), {})
        item["outputs"] = cls._decode_json(item.get("outputs"), {})
        return item

    def log_stage_artifact(
        self,
        session_id: str,
        *,
        stage_name: str,
        artifact: dict[str, Any],
        summary: str | None = None,
    ) -> int:
        content = summary or artifact.get("summary") or f"Pipeline stage completed: {stage_name}"
        return self.save_message(
            session_id,
            "system",
            str(content),
            event_type="stage_artifact",
            stage_name=stage_name,
            stage_artifact=artifact,
        )

    def list_pipeline_stage_names(self, session_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stage_name
                FROM messages
                WHERE session_id = ?
                  AND event_type = 'stage_artifact'
                  AND stage_name IS NOT NULL
                  AND stage_name != ''
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        return [str(row["stage_name"]) for row in rows]

    def log_layer_snapshot(
        self,
        session_id: str,
        *,
        layer_id: str,
        layer_name: str,
        action: str,
        source_path: str | None = None,
        crs: str | None = None,
        feature_count: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO layer_snapshots (
                    session_id, layer_id, layer_name, source_path, crs,
                    feature_count, action, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    layer_id,
                    layer_name,
                    source_path,
                    crs,
                    feature_count,
                    action,
                    time.time(),
                ),
            )

    # ------------------------------------------------------------------
    # Evidence-backed learning store. Candidates are intentionally kept
    # separate from active Recipes so successful execution never mutates
    # runtime knowledge without review.

    def record_task_outcome(
        self,
        task_id: str,
        *,
        status: str,
        completion_score: float | None = None,
        feedback_score: float | None = None,
        user_feedback: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        now = time.time()
        outcome_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM task_outcomes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing:
                outcome_id = str(existing["id"])
                connection.execute(
                    """
                    UPDATE task_outcomes
                    SET status = ?, completion_score = COALESCE(?, completion_score),
                        feedback_score = COALESCE(?, feedback_score),
                        user_feedback = COALESCE(?, user_feedback), metrics_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        completion_score,
                        feedback_score,
                        user_feedback,
                        json.dumps(metrics or {}, ensure_ascii=False),
                        now,
                        outcome_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO task_outcomes (
                        id, task_id, status, completion_score, feedback_score,
                        user_feedback, metrics_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outcome_id,
                        task_id,
                        status,
                        completion_score,
                        feedback_score,
                        user_feedback,
                        json.dumps(metrics or {}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        return outcome_id

    def save_knowledge_candidate(
        self,
        *,
        candidate_type: str,
        target_name: str,
        payload: dict[str, Any],
        evidence: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
        source_task_id: str | None = None,
        created_by: str = "agent",
        status: str = "candidate",
    ) -> str:
        candidate_id = str(uuid.uuid4())
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_candidates (
                    id, candidate_type, target_name, status, payload_json,
                    evidence_json, evaluation_json, source_task_id, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    candidate_type,
                    target_name,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(evidence or {}, ensure_ascii=False),
                    json.dumps(evaluation or {}, ensure_ascii=False),
                    source_task_id,
                    created_by,
                    now,
                    now,
                ),
            )
        return candidate_id

    def list_knowledge_candidates(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM knowledge_candidates"
        values: list[Any] = []
        if status:
            query += " WHERE status = ?"
            values.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._decode_learning_row(dict(row)) for row in rows]

    def search_recipes(
        self, query: str = "", *, status: str = "active", limit: int = 20
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM solution_recipes WHERE status = ?"
        values: list[Any] = [status]
        if query.strip():
            sql += " AND (name LIKE ? OR description LIKE ? OR intent LIKE ?)"
            pattern = f"%{query.strip()}%"
            values.extend([pattern, pattern, pattern])
        sql += " ORDER BY pinned DESC, use_count DESC, updated_at DESC LIMIT ?"
        values.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [self._decode_learning_row(dict(row)) for row in rows]

    def get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM solution_recipes WHERE id = ?", (recipe_id,)
            ).fetchone()
        return self._decode_learning_row(dict(row)) if row else None

    def set_recipe_status(self, recipe_id: str, status: str) -> None:
        if status not in {"active", "stale", "archived"}:
            raise ValueError(f"Unsupported Recipe status: {status}")
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE solution_recipes
                SET status = ?, updated_at = ?, archived_at = ?
                WHERE id = ?
                """,
                (status, now, now if status == "archived" else None, recipe_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Recipe not found: {recipe_id}")

    def promote_candidate(self, candidate_id: str) -> str:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM knowledge_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Knowledge candidate not found: {candidate_id}")
            item = self._decode_learning_row(dict(row))
            if item["candidate_type"] != "recipe":
                raise ValueError("Only recipe candidates can be promoted by this operation")
            recipe = item.get("payload") or {}
            recipe_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO solution_recipes (
                    id, name, description, intent, status, version, schema_json,
                    source_task_id, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?)
                """,
                (
                    recipe_id,
                    str(recipe.get("name") or item["target_name"]),
                    str(recipe.get("description") or ""),
                    str(recipe.get("intent") or item["target_name"]),
                    json.dumps(recipe, ensure_ascii=False),
                    item.get("source_task_id"),
                    item.get("created_by") or "agent",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_versions (
                    id, knowledge_type, knowledge_id, version, payload_json, created_at
                ) VALUES (?, 'recipe', ?, 1, ?, ?)
                """,
                (str(uuid.uuid4()), recipe_id, json.dumps(recipe, ensure_ascii=False), now),
            )
            connection.execute(
                "UPDATE knowledge_candidates SET status = 'validated', updated_at = ? WHERE id = ?",
                (now, candidate_id),
            )
        return recipe_id

    def curator_dry_run(self) -> list[dict[str, Any]]:
        proposals = []
        for candidate in self.list_knowledge_candidates(limit=200):
            if candidate.get("status") not in {"observed", "candidate"}:
                continue
            evidence = candidate.get("evidence") or {}
            evaluation = candidate.get("evaluation") or {}
            success_count = int(evidence.get("success_count") or 0)
            eligible = success_count >= 2 and bool(evaluation.get("passed"))
            proposals.append(
                {
                    "candidate_id": candidate["id"],
                    "target_name": candidate["target_name"],
                    "action": "promote" if eligible else "retain_candidate",
                    "reasons": (
                        ["至少两次成功证据", "回归评测通过"]
                        if eligible
                        else ["需要至少两次成功证据且回归评测通过"]
                    ),
                }
            )
        return proposals

    def record_skill_usage(self, skill_name: str, *, action: str) -> None:
        now = time.time()
        view_increment = 1 if action == "view" else 0
        use_increment = 1 if action == "use" else 0
        patch_increment = 1 if action == "patch" else 0
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO skill_usage (
                    skill_name, view_count, use_count, patch_count,
                    last_viewed_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    view_count = view_count + excluded.view_count,
                    use_count = use_count + excluded.use_count,
                    patch_count = patch_count + excluded.patch_count,
                    last_viewed_at = COALESCE(excluded.last_viewed_at, last_viewed_at),
                    last_used_at = COALESCE(excluded.last_used_at, last_used_at)
                """,
                (
                    skill_name,
                    view_increment,
                    use_increment,
                    patch_increment,
                    now if view_increment else None,
                    now if use_increment else None,
                ),
            )

    @staticmethod
    def _decode_learning_row(item: dict[str, Any]) -> dict[str, Any]:
        for raw_key, decoded_key in (
            ("schema_json", "schema"),
            ("payload_json", "payload"),
            ("evidence_json", "evidence"),
            ("evaluation_json", "evaluation"),
            ("metrics_json", "metrics"),
        ):
            if raw_key in item:
                raw = item.pop(raw_key)
                try:
                    item[decoded_key] = json.loads(raw or "{}")
                except (json.JSONDecodeError, TypeError):
                    item[decoded_key] = {}
        return item
