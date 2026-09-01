import hashlib
import json
import logging
import re
import time

import httpx

from app.config import Config
from app.services.bitrix_client import BitrixClient
from app.storage.database import MessageDatabase


logger = logging.getLogger(__name__)

ALLOWED_TAGS = (
    "повторный",
    "ожидает_результаты",
    "срочный",
    "нужна_консультация",
)


class SummaryAgent:
    """Summarize manager-visible history without storing it at the AI provider."""

    @staticmethod
    def available() -> bool:
        return bool(
            Config.SUMMARY_AI_ENABLED
            and Config.SUMMARY_AI_API_KEY
            and Config.SUMMARY_AI_MODEL
        )

    @staticmethod
    def redact(text: str) -> str:
        text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}\b", "[email]", text)
        text = re.sub(
            r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)", "[телефон]", text
        )
        return text

    @staticmethod
    def _response_text(payload: dict) -> str:
        for item in payload.get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"]).strip()
        raise ValueError("AI summary response did not contain output text")

    async def _request(self, text: str, *, combined: bool) -> str:
        instructions = (
            "Ты помощник менеджера медицинского сервиса. Составь точное краткое "
            "резюме переписки только по представленным фактам. Не ставь диагнозы, "
            "не интерпретируй медицинские результаты и не давай назначения. Не "
            "додумывай отсутствующие сведения. Отдели: причину обращения, важные "
            "факты, что уже сделал менеджер, договорённости, нерешённые вопросы и "
            "следующий организационный шаг. Если информация противоречива, укажи это. "
        )
        if combined:
            instructions += (
                "Это несколько обращений одного клиента. Покажи развитие ситуации по "
                "диалогам и сформируй единый итог без повторов."
            )
        request = {
            "model": Config.SUMMARY_AI_MODEL,
            "store": False,
            "instructions": instructions,
            "input": self.redact(text),
            "max_output_tokens": max(300, Config.SUMMARY_AI_MAX_OUTPUT_TOKENS),
            "safety_identifier": hashlib.sha256(
                text[:1000].encode("utf-8")
            ).hexdigest(),
        }
        async with httpx.AsyncClient(timeout=Config.SUMMARY_AI_TIMEOUT) as client:
            response = await client.post(
                f"{Config.SUMMARY_AI_BASE_URL.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {Config.SUMMARY_AI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            return self._response_text(response.json())

    @staticmethod
    def _chunks(segments: list[str]) -> list[str]:
        limit = max(4000, Config.SUMMARY_AI_MAX_INPUT_CHARS)
        pieces: list[str] = []
        for segment in segments:
            if len(segment) <= limit:
                pieces.append(segment)
                continue
            for start in range(0, len(segment), limit):
                pieces.append(segment[start : start + limit])

        batches: list[str] = []
        current: list[str] = []
        current_size = 0
        for piece in pieces:
            extra = len(piece) + 2
            if current and current_size + extra > limit:
                batches.append("\n\n".join(current))
                current = []
                current_size = 0
            current.append(piece)
            current_size += extra
        if current:
            batches.append("\n\n".join(current))
        return batches

    async def summarize(self, segments: list[str], *, combined: bool) -> str:
        summaries = [
            await self._request(batch, combined=combined)
            for batch in self._chunks(segments)
        ]
        for _ in range(8):
            if len(summaries) <= 1:
                break
            summaries = [
                await self._request(batch, combined=True)
                for batch in self._chunks(
                    [f"Частичное резюме {index}:\n{text}" for index, text in enumerate(summaries, 1)]
                )
            ]
        if len(summaries) != 1:
            raise RuntimeError("AI summary could not be reduced to one result")
        return summaries[0]


class DialogSummaryService:
    """Build local or AI summaries for one MAX user."""

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

    @staticmethod
    def _messages_segment(label: str, messages: list[dict]) -> str:
        roles = {
            "max_to_bitrix": "Клиент",
            "bitrix_to_max": "Менеджер",
            "automated_to_max": "Бот",
            "internal_to_bitrix": "Система",
        }
        lines = [label]
        for message in messages:
            role = roles.get(message.get("direction"), "Система")
            text = re.sub(r"\s+", " ", message.get("text") or "").strip()
            if not text:
                try:
                    media = json.loads(message.get("media_json") or "[]")
                except (TypeError, ValueError):
                    media = []
                text = "[вложение]" if media else "[без текста]"
            lines.append(f"[{message.get('created_at') or 'без даты'}] {role}: {text}")
        return "\n".join(lines)

    async def build_ai(self, *, dialog_id: int, external_user_id: str) -> str:
        if not SummaryAgent.available():
            return self.build(dialog_id=dialog_id, external_user_id=external_user_id)
        messages = self.database.get_dialog_messages(dialog_id, limit=1000)
        if not messages:
            return "ℹ️ В текущем диалоге пока нет сообщений для резюме."
        try:
            summary = await SummaryAgent().summarize(
                [self._messages_segment("Текущий диалог", messages)], combined=False
            )
        except Exception as exc:
            logger.warning("Summary AI unavailable; local summary used: %s", type(exc).__name__)
            fallback = self.build(dialog_id=dialog_id, external_user_id=external_user_id)
            return fallback + "\n\n⚠️ ИИ временно недоступен; показана локальная выжимка."
        return "🤖 ИИ-резюме текущего диалога\n\n" + summary

    def _local_all_overview(
        self,
        *,
        dialog_id: int,
        external_user_id: str,
        archives: list[dict],
    ) -> str:
        current = self.database.get_dialog_messages(dialog_id, limit=1000)
        total_archived_messages = sum(int(item["message_count"]) for item in archives)
        tags = self.database.get_user_tags(external_user_id)
        lines = [
            "🗂 Обзор всех сохранённых обращений клиента",
            "",
            f"Завершённых диалогов: {len(archives)}",
            f"Сообщений в архивах: {total_archived_messages}",
            f"Сообщений в текущем диалоге: {len(current)}",
        ]
        if archives:
            lines.append(
                f"Период архивов: {archives[0].get('completed_at')} — "
                f"{archives[-1].get('completed_at')}"
            )
        if tags:
            lines.append("Теги: " + ", ".join(tags))
        lines.extend(["", "ИИ не настроен или временно недоступен; показан локальный обзор."])
        return "\n".join(lines)

    async def build_all_ai(
        self,
        *,
        dialog_id: int,
        external_user_id: str,
        external_chat_id: str,
    ) -> str:
        archives = self.database.list_user_chat_archives_full(
            external_user_id=external_user_id,
            external_chat_id=external_chat_id,
        )
        current = self.database.get_dialog_messages(dialog_id, limit=1000)
        if not archives and not current:
            return "ℹ️ У клиента пока нет сообщений или архивов для общего резюме."
        if not SummaryAgent.available():
            return self._local_all_overview(
                dialog_id=dialog_id,
                external_user_id=external_user_id,
                archives=archives,
            )

        segments: list[str] = []
        for index, archive in enumerate(archives, 1):
            try:
                messages = json.loads(archive.get("messages_json") or "[]")
            except (TypeError, ValueError):
                messages = []
            if messages:
                segments.append(
                    self._messages_segment(
                        f"Завершённый диалог {index}, архив №{archive['id']}, "
                        f"дата завершения {archive.get('completed_at')}",
                        messages,
                    )
                )
            else:
                segments.append(
                    f"Завершённый диалог {index}, архив №{archive['id']}: "
                    "структурированная переписка отсутствует"
                )
        if current:
            segments.append(self._messages_segment("Текущий незавершённый диалог", current))
        try:
            summary = await SummaryAgent().summarize(segments, combined=True)
        except Exception as exc:
            logger.warning(
                "Summary-all AI unavailable; local overview used: %s", type(exc).__name__
            )
            return self._local_all_overview(
                dialog_id=dialog_id,
                external_user_id=external_user_id,
                archives=archives,
            )
        return (
            f"🤖 Общее ИИ-резюме по обращениям клиента\n"
            f"Обработано архивов: {len(archives)}; текущий диалог: "
            f"{'да' if current else 'нет'}.\n\n{summary}"
        )


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
