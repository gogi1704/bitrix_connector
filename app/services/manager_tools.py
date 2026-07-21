import re
import time

from app.config import Config
from app.services.bitrix_client import BitrixClient
from app.storage.database import MessageDatabase


ALLOWED_TAGS = (
    "повторный",
    "ожидает_результаты",
    "срочный",
    "нужна_консультация",
)


class DialogSummaryService:
    """Build a private extractive summary without sending dialog data to AI."""

    def __init__(self, database: MessageDatabase | None = None):
        self.database = database or MessageDatabase()

    @staticmethod
    def _excerpt(text: str | None, limit: int = 260) -> str:
        compact = re.sub(r"\s+", " ", text or "").strip()
        if not compact:
            return "[вложение без текста]"
        return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"

    def build(self, *, dialog_id: int, external_user_id: str) -> str:
        messages = self.database.get_dialog_messages(dialog_id, limit=100)
        if not messages:
            return "ℹ️ В текущем диалоге пока нет сообщений для резюме."

        client_messages = [m for m in messages if m["direction"] == "max_to_bitrix"]
        manager_messages = [
            m
            for m in messages
            if m["direction"] in {"bitrix_to_max", "automated_to_max"}
        ]
        last = messages[-1]
        if last["direction"] == "max_to_bitrix":
            state = "ожидается ответ менеджера"
        elif last["direction"] in {"bitrix_to_max", "automated_to_max"}:
            state = "ожидается ответ клиента"
        else:
            state = "последнее событие — служебное"

        lines = [
            "📝 Краткое резюме текущего диалога",
            "",
            f"Период: {messages[0]['created_at']} — {messages[-1]['created_at']}",
            (
                f"Сообщений: {len(messages)} "
                f"(клиент: {len(client_messages)}, менеджер/бот: {len(manager_messages)})"
            ),
            f"Текущее состояние: {state}.",
        ]
        tags = self.database.get_user_tags(external_user_id)
        if tags:
            lines.append("Теги: " + ", ".join(tags))

        if client_messages:
            lines.extend(["", "Последние сообщения клиента:"])
            for message in client_messages[-3:]:
                lines.append("• " + self._excerpt(message.get("text")))
        if manager_messages:
            lines.extend(["", "Последний ответ менеджера:"])
            lines.append("• " + self._excerpt(manager_messages[-1].get("text")))

        lines.extend(
            [
                "",
                "Резюме сформировано локально по текущей рабочей истории без внешнего ИИ.",
            ]
        )
        return "\n".join(lines)


class ReminderService:
    def __init__(self, database: MessageDatabase | None = None):
        self.database = database or MessageDatabase()

    async def send(self, reminder_id: int) -> None:
        reminder = self.database.claim_manager_reminder(reminder_id)
        if reminder is None:
            return
        user_id = reminder.get("external_user_id") or reminder["external_chat_id"]
        user_name = reminder.get("external_user_name") or "Пользователь MAX"
        manager = (
            f"\nМенеджер: {reminder['manager_id']}"
            if reminder.get("manager_id")
            else ""
        )
        try:
            await BitrixClient().call(
                "imconnector.send.messages",
                {
                    "CONNECTOR": Config.MAX_CONNECTOR_ID,
                    "LINE": int(Config.BITRIX_OPENLINE_ID),
                    "MESSAGES": [
                        {
                            "user": {"id": f"max-{user_id}", "name": user_name},
                            "message": {
                                "id": f"manager-reminder-{reminder_id}",
                                "date": int(time.time()),
                                "text": (
                                    "⏰ Напоминание менеджеру\n\n"
                                    f"{reminder['reminder_text']}"
                                    f"{manager}\nКлиент: {user_name} (MAX ID: {user_id})"
                                ),
                            },
                            "chat": {
                                "id": str(reminder["external_chat_id"]),
                                "name": f"MAX: {user_name}",
                                "url": "https://max.ru",
                            },
                        }
                    ],
                },
            )
            self.database.mark_manager_reminder_sent(reminder_id)
        except Exception as exc:
            self.database.release_manager_reminder(reminder_id, str(exc))
            raise
