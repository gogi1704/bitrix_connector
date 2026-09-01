import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Config, DATA_DIR


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
                CREATE TABLE IF NOT EXISTS followup_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dialog_id INTEGER NOT NULL,
                    based_on_message_id INTEGER NOT NULL,
                    action TEXT,
                    state TEXT NOT NULL DEFAULT 'planning',
                    due_at REAL,
                    draft_text TEXT,
                    reason TEXT,
                    confidence REAL,
                    last_error TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(dialog_id, based_on_message_id),
                    FOREIGN KEY(dialog_id) REFERENCES dialogs(id)
                );
                CREATE TABLE IF NOT EXISTS user_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_user_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(external_user_id, tag)
                );
                CREATE TABLE IF NOT EXISTS manager_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dialog_id INTEGER NOT NULL,
                    external_user_id TEXT,
                    external_chat_id TEXT NOT NULL,
                    external_user_name TEXT,
                    manager_id TEXT,
                    reminder_text TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    source_message_id TEXT UNIQUE,
                    state TEXT NOT NULL DEFAULT 'scheduled',
                    last_error TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(dialog_id) REFERENCES dialogs(id)
                );
                CREATE INDEX IF NOT EXISTS messages_external_idx
                    ON messages(dialog_id, direction, external_message_id);
                CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs(state, available_at, id);
                CREATE INDEX IF NOT EXISTS chat_archives_user_idx
                    ON chat_archives(external_user_id, completed_at);
                CREATE INDEX IF NOT EXISTS followup_jobs_active_idx
                    ON followup_jobs(dialog_id, state, due_at);
                CREATE INDEX IF NOT EXISTS user_tags_user_idx
                    ON user_tags(external_user_id, created_at);
                CREATE INDEX IF NOT EXISTS manager_reminders_ready_idx
                    ON manager_reminders(state, due_at, id);
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
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
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
            return int(cursor.lastrowid)

    def get_dialog_messages(self, dialog_id: int, *, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE dialog_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (dialog_id, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def create_followup_plan(self, *, dialog_id: int, based_on_message_id: int) -> int:
        """Cancel older plans and create one plan for the latest operator message."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE followup_jobs
                SET state='cancelled', reason='Появилось более новое сообщение',
                    updated_at=CURRENT_TIMESTAMP
                WHERE dialog_id = ? AND state IN ('planning', 'scheduled')
                """,
                (dialog_id,),
            )
            cursor = connection.execute(
                """
                INSERT INTO followup_jobs (dialog_id, based_on_message_id)
                VALUES (?, ?)
                ON CONFLICT(dialog_id, based_on_message_id) DO NOTHING
                """,
                (dialog_id, based_on_message_id),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT id FROM followup_jobs
                WHERE dialog_id = ? AND based_on_message_id = ?
                """,
                (dialog_id, based_on_message_id),
            ).fetchone()
            return int(row["id"])

    def cancel_pending_followups(self, dialog_id: int, *, reason: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE followup_jobs
                SET state='cancelled', reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE dialog_id = ? AND state IN ('planning', 'scheduled')
                """,
                (reason[:500], dialog_id),
            )
            return int(cursor.rowcount)

    def get_followup(self, followup_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT followup_jobs.*, dialogs.external_chat_id,
                       dialogs.external_user_id, dialogs.external_user_name
                FROM followup_jobs
                JOIN dialogs ON dialogs.id = followup_jobs.dialog_id
                WHERE followup_jobs.id = ?
                """,
                (followup_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def schedule_followup(
        self,
        *,
        followup_id: int,
        action: str,
        due_at: float | None,
        draft_text: str | None,
        reason: str,
        confidence: float,
    ) -> bool:
        state = (
            "scheduled"
            if action in {"send_now", "follow_up_30m", "next_day_09"}
            else action
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE followup_jobs
                SET action=?, state=?, due_at=?, draft_text=?, reason=?, confidence=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state='planning'
                """,
                (
                    action,
                    state,
                    due_at,
                    draft_text,
                    reason[:1000],
                    max(0.0, min(float(confidence), 1.0)),
                    followup_id,
                ),
            )
            return cursor.rowcount == 1

    def claim_followup_for_send(self, followup_id: int) -> dict | None:
        """Atomically verify that this plan is still based on the latest message."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            followup = connection.execute(
                "SELECT * FROM followup_jobs WHERE id = ?", (followup_id,)
            ).fetchone()
            if followup is None or followup["state"] != "scheduled":
                return None
            latest = connection.execute(
                """
                SELECT id, direction FROM messages
                WHERE dialog_id = ? ORDER BY id DESC LIMIT 1
                """,
                (followup["dialog_id"],),
            ).fetchone()
            if (
                latest is None
                or int(latest["id"]) != int(followup["based_on_message_id"])
                or latest["direction"] != "bitrix_to_max"
            ):
                connection.execute(
                    """
                    UPDATE followup_jobs
                    SET state='cancelled', reason='Диалог изменился до отправки',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (followup_id,),
                )
                return None
            connection.execute(
                "UPDATE followup_jobs SET state='sending', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (followup_id,),
            )
            dialog = connection.execute(
                "SELECT * FROM dialogs WHERE id = ?", (followup["dialog_id"],)
            ).fetchone()
            result = dict(followup)
            result.update({f"dialog_{key}": value for key, value in dict(dialog).items()})
            return result

    def mark_followup_sent(self, followup_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE followup_jobs
                SET state='sent', sent_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state='sending'
                """,
                (followup_id,),
            )

    def release_followup_after_error(self, followup_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE followup_jobs
                SET state='scheduled', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state='sending'
                """,
                (error[:1000], followup_id),
            )

    def mark_followup_failed(self, followup_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE followup_jobs
                SET state='failed', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state IN ('scheduled', 'sending')
                """,
                (error[:1000], followup_id),
            )

    def has_dialog(self, *, channel: str, external_chat_id: str) -> bool:
        return self.get_dialog(channel=channel, external_chat_id=external_chat_id) is not None

    def add_user_tag(
        self, *, external_user_id: str, tag: str, created_by: str | None = None
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO user_tags (external_user_id, tag, created_by)
                VALUES (?, ?, ?)
                ON CONFLICT(external_user_id, tag) DO NOTHING
                """,
                (external_user_id, tag, created_by),
            )
            return cursor.rowcount > 0

    def get_user_tags(self, external_user_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tag FROM user_tags
                WHERE external_user_id = ?
                ORDER BY created_at, id
                """,
                (external_user_id,),
            ).fetchall()
            return [str(row["tag"]) for row in rows]

    def create_manager_reminder(
        self,
        *,
        dialog_id: int,
        external_user_id: str | None,
        external_chat_id: str,
        external_user_name: str | None,
        manager_id: str | None,
        reminder_text: str,
        due_at: float,
        source_message_id: str,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO manager_reminders (
                    dialog_id, external_user_id, external_chat_id,
                    external_user_name, manager_id, reminder_text,
                    due_at, source_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_message_id) DO NOTHING
                """,
                (
                    dialog_id,
                    external_user_id,
                    external_chat_id,
                    external_user_name,
                    manager_id,
                    reminder_text,
                    due_at,
                    source_message_id,
                ),
            )
            if cursor.lastrowid:
                reminder_id = int(cursor.lastrowid)
            else:
                row = connection.execute(
                    "SELECT id FROM manager_reminders WHERE source_message_id = ?",
                    (source_message_id,),
                ).fetchone()
                reminder_id = int(row["id"])
            row = connection.execute(
                "SELECT * FROM manager_reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO jobs (job_type, payload_json, dedupe_key, available_at)
                VALUES ('manager_reminder', ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    json.dumps({"reminder_id": reminder_id}),
                    f"manager-reminder:{reminder_id}",
                    due_at,
                ),
            )
            return dict(row)

    def get_manager_reminder(self, reminder_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM manager_reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def claim_manager_reminder(self, reminder_id: int) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM manager_reminders
                WHERE id = ? AND state IN ('scheduled', 'retry') AND due_at <= ?
                """,
                (reminder_id, time.time()),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE manager_reminders
                SET state='sending', updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reminder_id,),
            )
            return dict(row)

    def mark_manager_reminder_sent(self, reminder_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE manager_reminders
                SET state='sent', sent_at=CURRENT_TIMESTAMP, last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reminder_id,),
            )

    def release_manager_reminder(self, reminder_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE manager_reminders
                SET state='retry', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id = ? AND state='sending'
                """,
                (error[:1000], reminder_id),
            )

    def mark_manager_reminder_failed(self, reminder_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE manager_reminders
                SET state='failed', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error[:1000], reminder_id),
            )

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
            connection.execute(
                """
                UPDATE followup_jobs
                SET state='cancelled', reason='Диалог завершён менеджером',
                    updated_at=CURRENT_TIMESTAMP
                WHERE dialog_id = ? AND state IN ('planning', 'scheduled')
                """,
                (dialog_id,),
            )
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

    def list_user_chat_archives(
        self,
        *,
        external_user_id: str | None,
        external_chat_id: str,
        limit: int = 10,
    ) -> list[dict]:
        with self._connect() as connection:
            if external_user_id:
                rows = connection.execute(
                    """
                    SELECT id, message_count, first_message_at, last_message_at,
                           completed_at, completed_by
                    FROM chat_archives
                    WHERE external_user_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (external_user_id, max(1, min(limit, 50))),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, message_count, first_message_at, last_message_at,
                           completed_at, completed_by
                    FROM chat_archives
                    WHERE external_chat_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (external_chat_id, max(1, min(limit, 50))),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_user_chat_archive(
        self,
        *,
        position: int,
        external_user_id: str | None,
        external_chat_id: str,
    ) -> dict | None:
        if position < 1:
            return None
        with self._connect() as connection:
            if external_user_id:
                row = connection.execute(
                    """
                    SELECT * FROM chat_archives
                    WHERE external_user_id = ?
                    ORDER BY id DESC LIMIT 1 OFFSET ?
                    """,
                    (external_user_id, position - 1),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM chat_archives
                    WHERE external_chat_id = ?
                    ORDER BY id DESC LIMIT 1 OFFSET ?
                    """,
                    (external_chat_id, position - 1),
                ).fetchone()
            return dict(row) if row is not None else None

    def list_user_chat_archives_full(
        self,
        *,
        external_user_id: str | None,
        external_chat_id: str,
    ) -> list[dict]:
        """Load every saved archive for one user for manager-requested analysis."""
        with self._connect() as connection:
            if external_user_id:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_archives
                    WHERE external_user_id = ?
                    ORDER BY id
                    """,
                    (external_user_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_archives
                    WHERE external_chat_id = ?
                    ORDER BY id
                    """,
                    (external_chat_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def enqueue(
        self,
        *,
        job_type: str,
        payload: dict,
        dedupe_key: str,
        available_at: float | None = None,
    ) -> bool:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (job_type, payload_json, dedupe_key, available_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_type,
                        json.dumps(payload, ensure_ascii=False),
                        dedupe_key,
                        time.time() if available_at is None else available_at,
                    ),
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
                    updated_at=CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state, time.time() + delay, error[:1000], job_id),
            )

    def retry_failed_job(self, job_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_type, payload_json FROM jobs
                WHERE id = ? AND state='failed' AND payload_json != '{}'
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row["payload_json"])
            if row["job_type"] == "manager_reminder" and payload.get("reminder_id"):
                connection.execute(
                    """
                    UPDATE manager_reminders
                    SET state='retry', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id = ? AND state='failed'
                    """,
                    (int(payload["reminder_id"]),),
                )
            elif row["job_type"] == "followup_send" and payload.get("followup_id"):
                connection.execute(
                    """
                    UPDATE followup_jobs
                    SET state='scheduled', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id = ? AND state='failed'
                    """,
                    (int(payload["followup_id"]),),
                )
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state='retry', attempts=0, available_at=?, last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id = ? AND state='failed' AND payload_json != '{}'
                """,
                (time.time(), job_id),
            )
            return cursor.rowcount > 0

    def purge_old_jobs(self) -> None:
        failed_days = max(1, Config.JOB_FAILED_RETENTION_DAYS)
        completed_days = max(1, Config.JOB_COMPLETED_RETENTION_DAYS)
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM jobs
                WHERE state='failed'
                  AND updated_at < datetime('now', ?)
                """,
                (f"-{failed_days} days",),
            )
            connection.execute(
                """
                DELETE FROM jobs
                WHERE state='completed'
                  AND updated_at < datetime('now', ?)
                """,
                (f"-{completed_days} days",),
            )

    def recover_processing_jobs(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET state='retry', available_at=?, updated_at=CURRENT_TIMESTAMP WHERE state='processing'",
                (time.time(),),
            )
            connection.execute(
                """
                UPDATE followup_jobs
                SET state='scheduled', updated_at=CURRENT_TIMESTAMP
                WHERE state='sending'
                """
            )
            connection.execute(
                """
                UPDATE manager_reminders
                SET state='retry', updated_at=CURRENT_TIMESTAMP
                WHERE state='sending'
                """
            )

    def queue_status(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            ).fetchall()
            return {row["state"]: row["count"] for row in rows}

    def analytics_snapshot(self) -> dict:
        """Return aggregate operational metrics without message or profile contents."""
        with self._connect() as connection:
            def scalar(sql: str, parameters: tuple = ()):
                row = connection.execute(sql, parameters).fetchone()
                return row[0] if row is not None else 0

            queue_rows = connection.execute(
                "SELECT state, COUNT(*) FROM jobs GROUP BY state"
            ).fetchall()
            reminder_rows = connection.execute(
                "SELECT state, COUNT(*) FROM manager_reminders GROUP BY state"
            ).fetchall()
            tag_rows = connection.execute(
                """
                SELECT tag, COUNT(DISTINCT external_user_id) AS count
                FROM user_tags GROUP BY tag ORDER BY count DESC, tag
                """
            ).fetchall()
            recent_failed = connection.execute(
                """
                SELECT id, job_type, attempts, updated_at
                FROM jobs WHERE state='failed'
                ORDER BY id DESC LIMIT 20
                """
            ).fetchall()
            return {
                "dialogs": {
                    "total": scalar("SELECT COUNT(*) FROM dialogs"),
                    "active": scalar(
                        "SELECT COUNT(DISTINCT dialog_id) FROM messages"
                    ),
                },
                "messages": {
                    "working": scalar("SELECT COUNT(*) FROM messages"),
                    "last_24h": scalar(
                        "SELECT COUNT(*) FROM messages WHERE created_at >= datetime('now', '-1 day')"
                    ),
                },
                "archives": {
                    "total": scalar("SELECT COUNT(*) FROM chat_archives"),
                    "last_7d": scalar(
                        "SELECT COUNT(*) FROM chat_archives WHERE completed_at >= datetime('now', '-7 days')"
                    ),
                    "average_messages": round(
                        float(
                            scalar(
                                "SELECT COALESCE(AVG(message_count), 0) FROM chat_archives"
                            )
                        ),
                        1,
                    ),
                },
                "queue": {str(row[0]): int(row[1]) for row in queue_rows},
                "reminders": {str(row[0]): int(row[1]) for row in reminder_rows},
                "tags": [{"tag": str(row[0]), "count": int(row[1])} for row in tag_rows],
                "recent_failed_jobs": [dict(row) for row in recent_failed],
            }
