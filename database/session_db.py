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
    ) -> int:
        timestamp = time.time()
        artifact_json = json.dumps(stage_artifact, ensure_ascii=False) if stage_artifact else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, timestamp, finish_reason,
                    stage_name, stage_artifact, event_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                SELECT id, role, content, timestamp, event_type, finish_reason
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_conversation_messages(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return user/assistant history, applying the limit after role filtering."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, timestamp, event_type, finish_reason
                FROM messages
                WHERE session_id = ? AND role IN ('user', 'assistant')
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

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
