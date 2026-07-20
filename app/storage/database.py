import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import DATA_DIR


class MessageDatabase:

    PATH = DATA_DIR / "connector.db"

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dialogs (
                    id INTEGER PRIMARY KEY,
                    channel TEXT NOT NULL,
                    external_chat_id TEXT NOT NULL,
                    external_user_id TEXT,
                    external_user_name TEXT,
                    bitrix_chat_id TEXT,
                    bitrix_session_id TEXT,
                    welcome_sent INTEGER NOT NULL DEFAULT 0,
                    line_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(channel, external_chat_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    dialog_id INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    external_message_id TEXT,
                    bitrix_message_id TEXT,
                    text TEXT,
                    media_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(dialog_id) REFERENCES dialogs(id)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS user_profiles (
                    external_user_id TEXT PRIMARY KEY,
                    age INTEGER,
                    weight TEXT,
                    height TEXT,
                    sex TEXT,
                    full_name TEXT,
                    phone TEXT,
                    organization_or_inn TEXT,
                    osmotr_date TEXT,
                    med_id TEXT,
                    user_state TEXT,
                    chel_id TEXT,
                    client_synced_at TEXT,
                    source_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS user_test_results (
                    external_user_id TEXT PRIMARY KEY,
                    med_id TEXT NOT NULL,
                    results TEXT NOT NULL,
                    source_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_dialog_id INTEGER NOT NULL,
                    source_message_id TEXT UNIQUE,
                    external_chat_id TEXT NOT NULL,
                    external_user_id TEXT,
                    external_user_name TEXT,
                    bitrix_chat_id TEXT,
                    bitrix_session_id TEXT,
                    completed_by TEXT,
                    message_count INTEGER NOT NULL,
                    first_message_at TEXT,
                    last_message_at TEXT,
                    transcript_text TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS messages_external_idx
                    ON messages(dialog_id, direction, external_message_id);
                CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs(state, available_at, id);
                CREATE INDEX IF NOT EXISTS chat_archives_user_idx
                    ON chat_archives(external_user_id, completed_at);
                """
            )
            dialog_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(dialogs)").fetchall()
            }
            if "welcome_sent" not in dialog_columns:
                connection.execute(
                    "ALTER TABLE dialogs ADD COLUMN welcome_sent INTEGER NOT NULL DEFAULT 0"
                )
                # Jobs completed by older versions sent the welcome after
                # recording the synthetic max-start message.
                connection.execute(
                    """
                    UPDATE dialogs
                    SET welcome_sent = 1
                    WHERE EXISTS (
                        SELECT 1 FROM messages
                        WHERE messages.dialog_id = dialogs.id
                          AND messages.external_message_id = 'max-start-' || dialogs.external_chat_id
                    )
                    """
                )

            profile_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(user_profiles)").fetchall()
            }
            for column_name, column_type in (
                ("full_name", "TEXT"),
                ("phone", "TEXT"),
                ("organization_or_inn", "TEXT"),
                ("osmotr_date", "TEXT"),
                ("med_id", "TEXT"),
                ("user_state", "TEXT"),
                ("chel_id", "TEXT"),
                ("client_synced_at", "TEXT"),
            ):
                if column_name not in profile_columns:
                    connection.execute(
                        f"ALTER TABLE user_profiles ADD COLUMN {column_name} {column_type}"
                    )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.PATH, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def upsert_dialog(
        self,
        *,
        channel: str,
        external_chat_id: str,
        line_id: int,
        external_user_id: str | None = None,
        external_user_name: str | None = None,
        bitrix_chat_id: str | None = None,
        bitrix_session_id: str | None = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dialogs (
                    channel, external_chat_id, external_user_id, external_user_name,
                    bitrix_chat_id, bitrix_session_id, line_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, external_chat_id) DO UPDATE SET
                    external_user_id=COALESCE(excluded.external_user_id, dialogs.external_user_id),
                    external_user_name=COALESCE(excluded.external_user_name, dialogs.external_user_name),
                    bitrix_chat_id=COALESCE(excluded.bitrix_chat_id, dialogs.bitrix_chat_id),
                    bitrix_session_id=COALESCE(excluded.bitrix_session_id, dialogs.bitrix_session_id),
                    line_id=excluded.line_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    channel,
                    external_chat_id,
                    external_user_id,
                    external_user_name,
                    bitrix_chat_id,
                    bitrix_session_id,
                    line_id,
                ),
            )
            row = connection.execute(
                "SELECT id FROM dialogs WHERE channel = ? AND external_chat_id = ?",
                (channel, external_chat_id),
            ).fetchone()
            return int(row["id"])

    def get_dialog(self, *, channel: str, external_chat_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dialogs WHERE channel = ? AND external_chat_id = ?",
                (channel, external_chat_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def mark_welcome_sent(self, dialog_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE dialogs SET welcome_sent=1, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                (dialog_id,),
            )

    def save_message(
        self,
        *,
        dialog_id: int,
        direction: str,
        text: str | None,
        external_message_id: str | None = None,
        bitrix_message_id: str | None = None,
        media: list | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    dialog_id, direction, text, external_message_id, bitrix_message_id, media_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    dialog_id,
                    direction,
                    text,
                    external_message_id,
                    bitrix_message_id,
                    json.dumps(media or [], ensure_ascii=False),
                ),
            )

    def has_dialog(self, *, channel: str, external_chat_id: str) -> bool:
        return self.get_dialog(channel=channel, external_chat_id=external_chat_id) is not None

    def get_user_profile(self, external_user_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE external_user_id = ?",
                (external_user_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_user_profile(
        self,
        *,
        external_user_id: str,
        age: int | str | None = None,
        weight: str | None = None,
        height: str | None = None,
        sex: str | None = None,
        full_name: str | None = None,
        phone: str | None = None,
        organization_or_inn: str | None = None,
        osmotr_date: str | None = None,
        med_id: str | None = None,
        user_state: str | None = None,
        chel_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_profiles (
                    external_user_id, age, weight, height, sex, full_name, phone,
                    organization_or_inn, osmotr_date, med_id, user_state, chel_id,
                    client_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(external_user_id) DO UPDATE SET
                    age=excluded.age,
                    weight=excluded.weight,
                    height=excluded.height,
                    sex=excluded.sex,
                    full_name=excluded.full_name,
                    phone=excluded.phone,
                    organization_or_inn=excluded.organization_or_inn,
                    osmotr_date=excluded.osmotr_date,
                    med_id=excluded.med_id,
                    user_state=excluded.user_state,
                    chel_id=excluded.chel_id,
                    client_synced_at=CURRENT_TIMESTAMP,
                    source_synced_at=CURRENT_TIMESTAMP
                """,
                (
                    external_user_id,
                    age,
                    weight,
                    height,
                    sex,
                    full_name,
                    phone,
                    organization_or_inn,
                    osmotr_date,
                    med_id,
                    user_state,
                    chel_id,
                ),
            )

    def get_user_test_results(self, external_user_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_test_results WHERE external_user_id = ?",
                (external_user_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_user_test_results(
        self,
        *,
        external_user_id: str,
        med_id: str,
        results: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_test_results (external_user_id, med_id, results)
                VALUES (?, ?, ?)
                ON CONFLICT(external_user_id) DO UPDATE SET
                    med_id=excluded.med_id,
                    results=excluded.results,
                    source_synced_at=CURRENT_TIMESTAMP
                """,
                (external_user_id, med_id, results),
            )

    @staticmethod
    def _format_transcript(dialog: dict, messages: list[dict]) -> str:
        roles = {
            "max_to_bitrix": "Клиент",
            "bitrix_to_max": "Менеджер",
            "internal_to_bitrix": "Система",
        }
        lines = [
            "ЗАВЕРШЕННЫЙ ДИАЛОГ",
            f"Дата завершения: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Пользователь: {dialog.get('external_user_name') or 'не указано'}",
            f"MAX ID: {dialog.get('external_user_id') or 'не указано'}",
            f"Чат: {dialog.get('external_chat_id')}",
            "",
        ]
        for message in messages:
            role = roles.get(message["direction"], message["direction"])
            text = (message.get("text") or "").strip() or "[без текста]"
            media = json.loads(message.get("media_json") or "[]")
            if media:
                descriptions = [
                    " ".join(
                        str(part)
                        for part in (item.get("type"), item.get("name"))
                        if part
                    )
                    for item in media
                ]
                text += f"\n[Вложения: {', '.join(filter(None, descriptions))}]"
            lines.append(f"[{message['created_at']}] {role}: {text}")
        return "\n".join(lines)

    def archive_dialog_messages(
        self,
        *,
        dialog_id: int,
        completed_by: str | None = None,
        source_message_id: str | None = None,
    ) -> dict | None:
        """Atomically archive a dialog transcript and clear its working messages."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if source_message_id:
                existing = connection.execute(
                    "SELECT * FROM chat_archives WHERE source_message_id = ?",
                    (source_message_id,),
                ).fetchone()
                if existing is not None:
                    return dict(existing)
            dialog_row = connection.execute(
                "SELECT * FROM dialogs WHERE id = ?", (dialog_id,)
            ).fetchone()
            if dialog_row is None:
                return None
            message_rows = connection.execute(
                "SELECT * FROM messages WHERE dialog_id = ? ORDER BY id",
                (dialog_id,),
            ).fetchall()
            if not message_rows:
                return None

            dialog = dict(dialog_row)
            messages = [dict(row) for row in message_rows]
            transcript = self._format_transcript(dialog, messages)
            cursor = connection.execute(
                """
                INSERT INTO chat_archives (
                    original_dialog_id, source_message_id, external_chat_id, external_user_id,
                    external_user_name, bitrix_chat_id, bitrix_session_id,
                    completed_by, message_count, first_message_at, last_message_at,
                    transcript_text, messages_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dialog_id,
                    source_message_id,
                    dialog["external_chat_id"],
                    dialog.get("external_user_id"),
                    dialog.get("external_user_name"),
                    dialog.get("bitrix_chat_id"),
                    dialog.get("bitrix_session_id"),
                    completed_by,
                    len(messages),
                    messages[0]["created_at"],
                    messages[-1]["created_at"],
                    transcript,
                    json.dumps(messages, ensure_ascii=False),
                ),
            )
            archive_id = int(cursor.lastrowid)
            connection.execute("DELETE FROM messages WHERE dialog_id = ?", (dialog_id,))
            return {
                "id": archive_id,
                "message_count": len(messages),
                "transcript_text": transcript,
            }

    def get_chat_archive(self, archive_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_archives WHERE id = ?", (archive_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def enqueue(self, *, job_type: str, payload: dict, dedupe_key: str) -> bool:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (job_type, payload_json, dedupe_key, available_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_type, json.dumps(payload, ensure_ascii=False), dedupe_key, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def claim_job(self) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN ('pending', 'retry') AND available_at <= ?
                ORDER BY id
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            connection.execute(
                """
                UPDATE jobs
                SET state='processing', attempts=attempts + 1, updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["id"],),
            )
            connection.commit()
            job = dict(row)
            job["attempts"] += 1
            job["payload"] = json.loads(job.pop("payload_json"))
            return job

    def complete_job(self, job_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state='completed', payload_json='{}', updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (job_id,),
            )

    def fail_job(self, *, job_id: int, attempts: int, error: str, max_attempts: int) -> None:
        state = "failed" if attempts >= max_attempts else "retry"
        delay = min(2 ** attempts, 300) if state == "retry" else 0
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state=?,
                    available_at=?,
                    last_error=?,
                    payload_json=CASE WHEN ? = 'failed' THEN '{}' ELSE payload_json END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state, time.time() + delay, error[:1000], state, job_id),
            )

    def recover_processing_jobs(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET state='retry', available_at=?, updated_at=CURRENT_TIMESTAMP WHERE state='processing'",
                (time.time(),),
            )

    def queue_status(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            ).fetchall()
            return {row["state"]: row["count"] for row in rows}
