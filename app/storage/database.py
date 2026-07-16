import json
import sqlite3
import time

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
                CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs(state, available_at, id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.PATH, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

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
                    external_user_id=excluded.external_user_id,
                    external_user_name=excluded.external_user_name,
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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM dialogs WHERE channel = ? AND external_chat_id = ?",
                (channel, external_chat_id),
            ).fetchone()
            return row is not None

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
                "UPDATE jobs SET state='completed', updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

    def fail_job(self, *, job_id: int, attempts: int, error: str, max_attempts: int) -> None:
        state = "failed" if attempts >= max_attempts else "retry"
        delay = min(2 ** attempts, 300) if state == "retry" else 0
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state=?, available_at=?, last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state, time.time() + delay, error[:1000], job_id),
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
