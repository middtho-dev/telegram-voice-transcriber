from __future__ import annotations

import csv
import html
import json
import re
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
                    reply_to_message_id INTEGER,
                    reply_to_text TEXT,
                    reply_to_sender TEXT,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (business_connection_id, message_id)
                )
                """
            )
            self._ensure_column(connection, "messages", "reply_to_message_id", "INTEGER")
            self._ensure_column(connection, "messages", "reply_to_text", "TEXT")
            self._ensure_column(connection, "messages", "reply_to_sender", "TEXT")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    normalized_question TEXT NOT NULL UNIQUE,
                    question_text TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    reply_to_message_id INTEGER,
                    hits INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if column_name not in {str(column["name"]) for column in columns}:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

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
        reply = message.get("reply_to_message") or {}
        reply_sender = reply.get("from") or {}
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
                    reply_to_message_id,
                    reply_to_text,
                    reply_to_sender,
                    raw_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_connection_id, message_id) DO UPDATE SET
                    raw_json = excluded.raw_json,
                    text = excluded.text,
                    caption = excluded.caption,
                    reply_to_message_id = excluded.reply_to_message_id,
                    reply_to_text = excluded.reply_to_text,
                    reply_to_sender = excluded.reply_to_sender
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
                    reply.get("message_id"),
                    reply.get("text") or reply.get("caption") or _reply_media_label(reply),
                    _message_sender_name(reply_sender),
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
            learned = connection.execute("SELECT COUNT(*) AS count FROM learned_replies").fetchone()

        return {
            "messages": int(messages["count"]),
            "attachments": int(attachments["count"]),
            "chats": int(chats["count"]),
            "learned_replies": int(learned["count"]),
        }

    def learn_support_reply(
        self,
        *,
        question_text: str,
        answer_text: str,
        source_chat_id: int,
        source_message_id: int,
        reply_to_message_id: int,
    ) -> bool:
        normalized_question = _normalize_learning_text(question_text)
        answer_text = answer_text.strip()
        if len(normalized_question) < 8 or len(answer_text) < 8:
            return False

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO learned_replies (
                    normalized_question,
                    question_text,
                    answer_text,
                    source_chat_id,
                    source_message_id,
                    reply_to_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_question) DO UPDATE SET
                    answer_text = excluded.answer_text,
                    source_chat_id = excluded.source_chat_id,
                    source_message_id = excluded.source_message_id,
                    reply_to_message_id = excluded.reply_to_message_id,
                    hits = learned_replies.hits + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_question,
                    question_text.strip(),
                    answer_text,
                    source_chat_id,
                    source_message_id,
                    reply_to_message_id,
                    now,
                    now,
                ),
            )
        return True

    def find_learned_reply(self, text: str, min_score: float = 0.72) -> str | None:
        normalized = _normalize_learning_text(text)
        if len(normalized) < 8:
            return None

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT normalized_question, answer_text
                FROM learned_replies
                ORDER BY updated_at DESC
                LIMIT 500
                """
            ).fetchall()

        best_score = 0.0
        best_answer: str | None = None
        for row in rows:
            candidate = str(row["normalized_question"])
            score = max(
                SequenceMatcher(None, normalized, candidate).ratio(),
                _token_overlap_score(normalized, candidate),
            )
            if score > best_score:
                best_score = score
                best_answer = str(row["answer_text"])

        if best_score >= min_score:
            return best_answer
        return None

    def create_export_zip(
        self,
        *,
        period_name: str = "all",
        since_timestamp: int | None = None,
    ) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        export_dir = self.exports_dir / f"export_{timestamp}"
        export_dir.mkdir(parents=True, exist_ok=True)

        db_copy = export_dir / "messages.sqlite3"
        self._backup_database(db_copy)

        rows = self._message_rows(since_timestamp)
        attachments = self._attachment_rows(since_timestamp)
        self._write_csv(export_dir / "messages.csv", rows)
        self._write_jsonl(export_dir / "messages.jsonl", rows)
        self._write_html(export_dir / "index.html", rows, attachments, period_name)

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

    def _message_rows(self, since_timestamp: int | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = ()
        if since_timestamp is not None:
            where = "WHERE m.date >= ?"
            params = (since_timestamp,)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    m.*,
                    GROUP_CONCAT(a.local_path, '; ') AS attachment_paths
                FROM messages m
                LEFT JOIN attachments a ON a.message_db_id = m.id
                {where}
                GROUP BY m.id
                ORDER BY m.date ASC, m.id ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def _attachment_rows(self, since_timestamp: int | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = ()
        if since_timestamp is not None:
            where = "WHERE m.date >= ?"
            params = (since_timestamp,)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    a.*,
                    m.chat_id,
                    m.message_id,
                    m.date
                FROM attachments a
                JOIN messages m ON m.id = a.message_db_id
                {where}
                ORDER BY m.date ASC, m.id ASC, a.id ASC
                """,
                params,
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

    def _write_html(
        self,
        path: Path,
        rows: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        period_name: str,
    ) -> None:
        attachments_by_message: dict[int, list[dict[str, Any]]] = {}
        for attachment in attachments:
            attachments_by_message.setdefault(int(attachment["message_db_id"]), []).append(attachment)

        chats: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            chats.setdefault(int(row["chat_id"]), []).append(row)

        chat_tabs: list[str] = []
        chat_sections: list[str] = []
        for index, (chat_id, messages) in enumerate(chats.items()):
            chat_name = _chat_name(messages[0])
            active_class = " active" if index == 0 else ""
            section_id = f"chat-{chat_id}"
            chat_tabs.append(
                f'<button class="tab{active_class}" data-chat="{section_id}">'
                f"{html.escape(chat_name)} <span>{len(messages)}</span></button>"
            )
            chat_sections.append(
                f'<section id="{section_id}" class="chat{active_class}">'
                f"<h2>{html.escape(chat_name)}</h2>"
                f"{''.join(self._message_html(message, attachments_by_message.get(int(message['id']), [])) for message in messages)}"
                "</section>"
            )

        if not rows:
            chat_sections.append('<section class="chat active"><h2>Нет сообщений за выбранный период</h2></section>')

        document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Business Archive</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b0f14;
      --panel: #111821;
      --panel-2: #151f2b;
      --text: #e6edf3;
      --muted: #8b9bad;
      --line: #253142;
      --accent: #38bdf8;
      --accent-2: #22c55e;
      --shadow: rgba(0, 0, 0, 0.35);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 2;
      padding: 18px 22px;
      background: rgba(17, 24, 33, 0.92);
      backdrop-filter: blur(18px);
      border-bottom: 1px solid var(--line);
    }}
    h1, h2, p {{ margin: 0; }}
    header p {{ color: var(--muted); margin-top: 4px; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(220px, 300px) minmax(0, 1fr);
      min-height: calc(100vh - 77px);
    }}
    nav {{
      border-right: 1px solid var(--line);
      padding: 14px;
      background: #0f151d;
      overflow: auto;
    }}
    .tab {{
      width: 100%;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      margin-bottom: 6px;
      border: 1px solid transparent;
      border-radius: 8px;
      background: transparent;
      color: var(--text);
      text-align: left;
      cursor: pointer;
    }}
    .tab.active {{
      background: var(--panel-2);
      border-color: var(--line);
      color: var(--accent);
      box-shadow: 0 10px 30px var(--shadow);
    }}
    main {{ padding: 20px; overflow: auto; }}
    .chat {{ display: none; max-width: 980px; margin: 0 auto; }}
    .chat.active {{ display: block; }}
    .chat h2 {{ margin-bottom: 16px; font-size: 22px; }}
    .message {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 12px;
      box-shadow: 0 12px 36px var(--shadow);
    }}
    .meta {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .content {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    .reply {{
      display: grid;
      grid-template-columns: 3px minmax(0, 1fr);
      gap: 10px;
      margin: 0 0 10px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(56, 189, 248, 0.08);
      color: var(--text);
      text-decoration: none;
    }}
    .reply-line {{
      display: block;
      border-radius: 99px;
      background: var(--accent);
    }}
    .reply strong {{ display: block; color: var(--accent); font-size: 13px; }}
    .reply em {{
      display: block;
      color: var(--muted);
      font-style: normal;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .message:target {{
      border-color: var(--accent-2);
      box-shadow: 0 0 0 1px var(--accent-2), 0 12px 36px var(--shadow);
    }}
    .attachments {{ display: grid; gap: 10px; margin-top: 12px; }}
    .attachment {{
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0f1722;
    }}
    audio, video, img {{ width: 100%; max-width: 720px; display: block; }}
    img {{ height: auto; border-radius: 6px; }}
    a {{ color: var(--accent); }}
    @media (max-width: 760px) {{
      .layout {{ grid-template-columns: 1fr; }}
      nav {{ border-right: 0; border-bottom: 1px solid var(--line); max-height: 220px; }}
      main {{ padding: 14px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Telegram Business Archive</h1>
    <p>Период: {html.escape(period_name)}. Сообщений: {len(rows)}. Сформировано: {html.escape(datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))}</p>
  </header>
  <div class="layout">
    <nav>{''.join(chat_tabs)}</nav>
    <main>{''.join(chat_sections)}</main>
  </div>
  <script>
    document.querySelectorAll('.tab').forEach((tab) => {{
      tab.addEventListener('click', () => {{
        document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
        document.querySelectorAll('.chat').forEach((item) => item.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.chat).classList.add('active');
      }});
    }});
  </script>
</body>
</html>
"""
        path.write_text(document, encoding="utf-8")

    def _message_html(self, message: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
        sender = _sender_name(message)
        message_time = _format_telegram_time(message.get("date"))
        body = message.get("text") or message.get("caption") or ""
        if not body and message["message_type"] != "unknown":
            body = f"[{message['message_type']}]"

        attachment_html = "".join(self._attachment_html(attachment) for attachment in attachments)
        reply_html = self._reply_html(message)
        return (
            f'<article class="message" id="message-{message["message_id"]}">'
            f'<div class="meta"><span>{html.escape(sender)}</span><span>{html.escape(message_time)}</span></div>'
            f"{reply_html}"
            f'<div class="content">{html.escape(str(body))}</div>'
            f'<div class="attachments">{attachment_html}</div>'
            "</article>"
        )

    @staticmethod
    def _reply_html(message: dict[str, Any]) -> str:
        reply_to_message_id = message.get("reply_to_message_id")
        if not reply_to_message_id:
            return ""

        sender = html.escape(str(message.get("reply_to_sender") or "сообщение"))
        text = html.escape(str(message.get("reply_to_text") or "без текста"))
        return (
            f'<a class="reply" href="#message-{reply_to_message_id}">'
            '<span class="reply-line"></span>'
            '<span>'
            f'<strong>Ответ на {sender}</strong>'
            f'<em>{text}</em>'
            "</span>"
            "</a>"
        )

    def _attachment_html(self, attachment: dict[str, Any]) -> str:
        local_path = Path(str(attachment["local_path"]))
        try:
            relative = Path("attachments") / local_path.relative_to(self.attachments_dir)
        except ValueError:
            relative = Path("attachments") / local_path.name

        href = html.escape(relative.as_posix())
        kind = str(attachment["file_kind"])
        name = html.escape(str(attachment.get("file_name") or local_path.name))

        if kind in {"voice", "audio"}:
            media = f'<audio controls preload="metadata" src="{href}"></audio>'
        elif kind in {"video", "video_note", "animation"}:
            media = f'<video controls preload="metadata" src="{href}"></video>'
        elif kind in {"photo", "sticker"}:
            media = f'<img src="{href}" alt="{name}">'
        else:
            media = ""

        return (
            '<div class="attachment">'
            f"<strong>{html.escape(kind)}</strong>: "
            f'<a href="{href}" download>{name}</a>'
            f"{media}"
            "</div>"
        )


def _chat_name(message: dict[str, Any]) -> str:
    if message.get("chat_title"):
        return str(message["chat_title"])
    sender = _sender_name(message)
    if sender != "Unknown":
        return sender
    return f"Chat {message['chat_id']}"


def _sender_name(message: dict[str, Any]) -> str:
    return _message_sender_name(
        {
            "first_name": message.get("from_first_name"),
            "last_name": message.get("from_last_name"),
            "username": message.get("from_username"),
            "id": message.get("from_user_id"),
        }
    )


def _message_sender_name(sender: dict[str, Any]) -> str:
    parts = [
        str(sender.get("first_name") or "").strip(),
        str(sender.get("last_name") or "").strip(),
    ]
    name = " ".join(part for part in parts if part)
    if name:
        return name
    if sender.get("username"):
        return f"@{sender['username']}"
    if sender.get("id"):
        return str(sender["id"])
    return "Unknown"


def _reply_media_label(message: dict[str, Any]) -> str:
    message_type = detect_message_type(message)
    if message_type == "unknown":
        return ""
    return f"[{message_type}]"


def _normalize_learning_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", text.casefold())).strip()
    replacements = {
        "впн": "vpn",
        "айфон": "iphone",
        "айфоне": "iphone",
        "айфона": "iphone",
        "андроид": "android",
        "макбук": "macbook",
        "виндовс": "windows",
        "винда": "windows",
    }
    for source, target in replacements.items():
        normalized = re.sub(rf"\b{source}\b", target, normalized)
    return normalized


def _token_overlap_score(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _format_telegram_time(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value), UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return str(value)


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
