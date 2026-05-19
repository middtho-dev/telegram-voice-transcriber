from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MessageStorage:
    def __init__(self, database_path: Path, attachments_dir: Path, exports_dir: Path) -> None:
        self.database_path = database_path
        self.attachments_dir = attachments_dir
        self.exports_dir = exports_dir

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_connection_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_type TEXT,
                    chat_title TEXT,
                    from_user_id INTEGER,
                    from_username TEXT,
                    from_first_name TEXT,
                    from_last_name TEXT,
                    message_id INTEGER NOT NULL,
                    date INTEGER,
                    message_type TEXT NOT NULL,
                    text TEXT,
                    caption TEXT,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (business_connection_id, message_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_db_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    file_kind TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT,
                    file_name TEXT,
                    mime_type TEXT,
                    file_size INTEGER,
                    local_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_bool(self, key: str, default: bool) -> bool:
        value = self.get_value(key)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "y", "on"}

    def set_bool(self, key: str, value: bool) -> None:
        self.set_value(key, "true" if value else "false")

    def get_value(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_value(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def save_message(
        self,
        message: dict[str, Any],
        attachments: list[dict[str, Any]],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        message_type = detect_message_type(message)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    business_connection_id,
                    chat_id,
                    chat_type,
                    chat_title,
                    from_user_id,
                    from_username,
                    from_first_name,
                    from_last_name,
                    message_id,
                    date,
                    message_type,
                    text,
                    caption,
                    raw_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_connection_id, message_id) DO UPDATE SET
                    raw_json = excluded.raw_json,
                    text = excluded.text,
                    caption = excluded.caption
                """,
                (
                    message["business_connection_id"],
                    chat.get("id"),
                    chat.get("type"),
                    chat.get("title") or chat.get("username"),
                    sender.get("id"),
                    sender.get("username"),
                    sender.get("first_name"),
                    sender.get("last_name"),
                    message["message_id"],
                    message.get("date"),
                    message_type,
                    message.get("text"),
                    message.get("caption"),
                    json.dumps(message, ensure_ascii=False),
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM messages
                WHERE business_connection_id = ? AND message_id = ?
                """,
                (message["business_connection_id"], message["message_id"]),
            ).fetchone()
            message_db_id = int(row["id"] if row else cursor.lastrowid)
            connection.execute(
                "DELETE FROM attachments WHERE message_db_id = ?",
                (message_db_id,),
            )

            for attachment in attachments:
                connection.execute(
                    """
                    INSERT INTO attachments (
                        message_db_id,
                        file_kind,
                        file_id,
                        file_unique_id,
                        file_name,
                        mime_type,
                        file_size,
                        local_path,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_db_id,
                        attachment["kind"],
                        attachment["file_id"],
                        attachment.get("file_unique_id"),
                        attachment.get("file_name"),
                        attachment.get("mime_type"),
                        attachment.get("file_size"),
                        attachment["local_path"],
                        now,
                    ),
                )

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            messages = connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
            attachments = connection.execute("SELECT COUNT(*) AS count FROM attachments").fetchone()
            chats = connection.execute("SELECT COUNT(DISTINCT chat_id) AS count FROM messages").fetchone()

        return {
            "messages": int(messages["count"]),
            "attachments": int(attachments["count"]),
            "chats": int(chats["count"]),
        }

    def create_export_zip(self) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        export_dir = self.exports_dir / f"export_{timestamp}"
        export_dir.mkdir(parents=True, exist_ok=True)

        db_copy = export_dir / "messages.sqlite3"
        self._backup_database(db_copy)

        rows = self._message_rows()
        self._write_csv(export_dir / "messages.csv", rows)
        self._write_jsonl(export_dir / "messages.jsonl", rows)

        archive_path = self.exports_dir / f"telegram_business_messages_{timestamp}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in export_dir.rglob("*"):
                archive.write(path, path.relative_to(export_dir))
            for path in self.attachments_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, Path("attachments") / path.relative_to(self.attachments_dir))

        shutil.rmtree(export_dir, ignore_errors=True)
        return archive_path

    def _backup_database(self, target: Path) -> None:
        with self._connect() as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)

    def _message_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.*,
                    GROUP_CONCAT(a.local_path, '; ') AS attachment_paths
                FROM messages m
                LEFT JOIN attachments a ON a.message_db_id = m.id
                GROUP BY m.id
                ORDER BY m.date ASC, m.id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return

        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")


def detect_message_type(message: dict[str, Any]) -> str:
    for key in (
        "text",
        "voice",
        "audio",
        "document",
        "photo",
        "video",
        "video_note",
        "animation",
        "sticker",
        "contact",
        "location",
        "venue",
    ):
        if key in message:
            return key
    return "unknown"


def extract_attachment_specs(message: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for kind in ("voice", "audio", "document", "video", "video_note", "animation", "sticker"):
        value = message.get(kind)
        if isinstance(value, dict) and value.get("file_id"):
            specs.append(_attachment_spec(kind, value))

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = max(photos, key=lambda item: item.get("file_size", 0))
        specs.append(_attachment_spec("photo", largest))

    return specs


def _attachment_spec(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "file_id": value["file_id"],
        "file_unique_id": value.get("file_unique_id"),
        "file_name": value.get("file_name"),
        "mime_type": value.get("mime_type"),
        "file_size": value.get("file_size"),
    }
