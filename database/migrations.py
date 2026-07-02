"""SQLite migration helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .constants import SCHEMA_VERSION


def run_migrations(connection: sqlite3.Connection, schema_path: Path) -> None:
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    _create_messages_fts(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _create_messages_fts(connection: sqlite3.Connection) -> None:
    tokenizer_candidates = ("trigram", "unicode61")
    last_error: sqlite3.Error | None = None

    for tokenizer in tokenizer_candidates:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                "USING fts5(content, role UNINDEXED, session_id UNINDEXED, "
                f"tokenize='{tokenizer}')"
            )
            _create_messages_fts_triggers(connection)
            return
        except sqlite3.Error as exc:
            last_error = exc
            connection.execute("DROP TABLE IF EXISTS messages_fts")

    if last_error is not None:
        raise last_error


def _create_messages_fts_triggers(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_insert
        AFTER INSERT ON messages
        WHEN new.content IS NOT NULL BEGIN
            INSERT INTO messages_fts(rowid, content, role, session_id)
            VALUES (new.id, new.content, new.role, new.session_id);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_fts_delete
        AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
        END;

        CREATE TRIGGER IF NOT EXISTS messages_fts_update
        AFTER UPDATE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
            INSERT INTO messages_fts(rowid, content, role, session_id)
            VALUES (new.id, COALESCE(new.content, ''), new.role, new.session_id);
        END;
        """
    )
