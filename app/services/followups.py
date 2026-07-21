import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.config import Config
from app.services.bitrix_client import BitrixClient
from app.services.max_client import MaxClient
from app.storage.database import MessageDatabase


logger = logging.getLogger(__name__)

SENSITIVE_MEDICAL_RE = re.compile(
    r"\b(диагноз|лечени[ея]|назначени[ея]|дозировк|препарат|лекарств|"
    r"расшифровк|отклонени[ея]|результат(?:ы|ов)?\s+(?:анализа|анализов|обследования))\b",
    re.IGNORECASE,
)
FINISHED_RE = re.compile(
    r"\b(спасибо|благодарю|всего доброго|до свидания|хорошего дня|обращайтесь|рады помочь|"
    r"запись подтверждена|вы записаны)\b",
    re.IGNORECASE,
)
QUICK_RE = re.compile(
    r"\b(выбрать|подтвердить|подтвердите|пришлите|отправьте|загрузите|"
    r"заполните|перейдите|ссылка|удобное время|вариант времени|документ|"
    r"получилось|удалось|готовы продолжить)\b",
    re.IGNORECASE,
)


class FollowupAgent:
    """Choose a narrow business follow-up action; never provide medical advice."""

    @staticmethod
    def _redact(text: str) -> str:
        text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}\b", "[email]", text)
        return re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)", "[телефон]", text)

    @staticmethod
    def _fallback(messages: list[dict]) -> dict:
        last_text = (messages[-1].get("text") or "").strip() if messages else ""
        if FINISHED_RE.search(last_text):
            return {
                "action": "no_message",
                "message": "",
                "reason": "Диалог выглядит завершённым",
                "confidence": 0.95,
            }
        if messages and messages[-1].get("direction") == "max_to_bitrix":
            return {
                "action": "manager_review",
                "message": "",
                "reason": "Последнее сообщение клиента осталось без ответа менеджера",
                "confidence": 1.0,
            }
        if not last_text:
            return {
                "action": "next_day_09",
                "message": "Доброе утро! Получилось ознакомиться с отправленной информацией?",
                "reason": "Менеджер отправил вложение без поясняющего текста",
                "confidence": 0.8,
            }
        if SENSITIVE_MEDICAL_RE.search(last_text):
            return {
                "action": "manager_review",
                "message": "",
                "reason": "Последнее сообщение относится к медицинской теме",
                "confidence": 1.0,
            }
        if QUICK_RE.search(last_text):
            if re.search(r"\b(время|дат[ау]|запис)\w*\b", last_text, re.IGNORECASE):
                message = "Удалось выбрать удобное время? Если нужна помощь, я рядом."
            elif re.search(r"\b(документ|пришлите|отправьте|загрузите)\w*\b", last_text, re.IGNORECASE):
                message = "Получилось подготовить необходимые документы?"
            else:
                message = "Получилось выполнить действие, о котором мы говорили?"
            return {
                "action": "send_now",
                "message": message,
                "reason": "В диалоге осталось конкретное действие клиента",
                "confidence": 0.85,
            }
        return {
            "action": "next_day_09",
            "message": (
                "Доброе утро! Возвращаюсь к нашему вчерашнему разговору. "
                "Остались ли у вас вопросы?"
            ),
            "reason": "Срочного действия нет; уместен один мягкий контакт на следующий день",
            "confidence": 0.8,
        }

    @staticmethod
    def _response_text(payload: dict) -> str:
        for item in payload.get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        raise ValueError("AI response did not contain structured output text")

    async def decide(self, messages: list[dict], *, external_user_id: str | None) -> dict:
        fallback = self._fallback(messages)
        if (
            not Config.FOLLOWUP_AI_ENABLED
            or not Config.FOLLOWUP_AI_API_KEY
            or not Config.FOLLOWUP_AI_MODEL
        ):
            return fallback

        transcript = []
        for message in messages:
            role = "Клиент" if message["direction"] == "max_to_bitrix" else "Менеджер"
            transcript.append(f"{role}: {self._redact((message.get('text') or '').strip())}")
        input_text = "\n".join(transcript)[-12000:]
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send_now", "next_day_09", "no_message", "manager_review"],
                },
                "message": {"type": "string"},
                "reason": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["action", "message", "reason", "confidence"],
            "additionalProperties": False,
        }
        request = {
            "model": Config.FOLLOWUP_AI_MODEL,
            "store": False,
            "instructions": (
                "Ты контролируемый агент повторного контакта медицинского сервиса. "
                "Анализируй только деловую уместность сообщения после ответа менеджера. "
                "Анализ выполняется после 30 минут тишины. send_now выбирай лишь при "
                "незавершённом конкретном действии клиента и только если последнее сообщение "
                "принадлежит менеджеру; если последнее сообщение клиента — всегда manager_review. "
                "next_day_09 — для одного мягкого контакта завтра; no_message — если диалог "
                "завершён или повторный контакт лишний; manager_review — для медицинских "
                "рекомендаций, интерпретации результатов, жалоб, конфликтов и сомнений. "
                "Не придумывай цены, сроки, факты, диагнозы и назначения. Сообщение должно "
                "быть кратким, естественным, без давления и не более 350 символов."
            ),
            "input": input_text,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "followup_decision",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": 400,
            "safety_identifier": hashlib.sha256(
                (external_user_id or "unknown").encode("utf-8")
            ).hexdigest(),
        }
        try:
            async with httpx.AsyncClient(timeout=Config.FOLLOWUP_AI_TIMEOUT) as client:
                response = await client.post(
                    f"{Config.FOLLOWUP_AI_BASE_URL}/responses",
                    headers={
                        "Authorization": f"Bearer {Config.FOLLOWUP_AI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=request,
                )
                response.raise_for_status()
                decision = json.loads(self._response_text(response.json()))
            return self._validate(decision)
        except Exception as exc:
            logger.warning("Follow-up AI unavailable; safe fallback used: %s", type(exc).__name__)
            return fallback

    @staticmethod
    def _validate(decision: dict) -> dict:
        action = decision.get("action")
        message = str(decision.get("message") or "").strip()
        reason = str(decision.get("reason") or "Решение ИИ").strip()
        confidence = max(0.0, min(float(decision.get("confidence", 0)), 1.0))
        if action not in {"send_now", "next_day_09", "no_message", "manager_review"}:
            raise ValueError("Unsupported follow-up action")
        if action in {"send_now", "next_day_09"}:
            if not message or len(message) > 350 or confidence < 0.7:
                return {
                    "action": "manager_review",
                    "message": "",
                    "reason": "ИИ не уверен в безопасной автоматической отправке",
                    "confidence": confidence,
                }
            if SENSITIVE_MEDICAL_RE.search(message):
                return {
                    "action": "manager_review",
                    "message": "",
                    "reason": "Черновик затрагивает медицинскую интерпретацию",
                    "confidence": confidence,
                }
        return {
            "action": action,
            "message": message,
            "reason": reason[:1000],
            "confidence": confidence,
        }


class FollowupService:
    def __init__(self, database: MessageDatabase | None = None):
        self.database = database or MessageDatabase()
        self.agent = FollowupAgent()

    @staticmethod
    def _timezone():
        try:
            return ZoneInfo(Config.FOLLOWUP_TIMEZONE)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown FOLLOWUP_TIMEZONE; fixed UTC+03:00 used")
            return timezone(timedelta(hours=3))

    @classmethod
    def _due_at(cls, action: str) -> float | None:
        timezone_info = cls._timezone()
        now = datetime.now(timezone_info)
        if action in {"send_now", "follow_up_30m"}:
            due = now
            quiet_start = max(0, min(Config.FOLLOWUP_QUIET_START_HOUR, 23))
            quiet_end = max(0, min(Config.FOLLOWUP_QUIET_END_HOUR, 23))
            if due.hour >= quiet_start:
                due = (due + timedelta(days=1)).replace(
                    hour=quiet_end, minute=0, second=0, microsecond=0
                )
            elif due.hour < quiet_end:
                due = due.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
            return due.timestamp()
        if action == "next_day_09":
            now = datetime.now(timezone_info)
            tomorrow = now + timedelta(days=1)
            due = tomorrow.replace(
                hour=max(0, min(Config.FOLLOWUP_NEXT_DAY_HOUR, 23)),
                minute=0,
                second=0,
                microsecond=0,
            )
            return due.timestamp()
        return None

    async def plan(self, followup_id: int) -> None:
        followup = self.database.get_followup(followup_id)
        if followup is None or followup["state"] != "planning":
            return
        messages = self.database.get_dialog_messages(
            int(followup["dialog_id"]), limit=Config.FOLLOWUP_MAX_CONTEXT_MESSAGES
        )
        if (
            not messages
            or int(messages[-1]["id"]) != int(followup["based_on_message_id"])
        ):
            self.database.cancel_pending_followups(
                int(followup["dialog_id"]), reason="Диалог изменился до анализа"
            )
            return
        decision = await self.agent.decide(
            messages, external_user_id=followup.get("external_user_id")
        )
        due_at = self._due_at(decision["action"])
        scheduled = self.database.schedule_followup(
            followup_id=followup_id,
            action=decision["action"],
            due_at=due_at,
            draft_text=decision["message"] or None,
            reason=decision["reason"],
            confidence=decision["confidence"],
        )
        if scheduled and due_at is not None:
            self.database.enqueue(
                job_type="followup_send",
                payload={"followup_id": followup_id},
                dedupe_key=f"followup:send:{followup_id}",
                available_at=due_at,
            )
        elif scheduled and decision["action"] == "manager_review":
            await self._notify_manager_review(followup, decision["reason"])

    @staticmethod
    async def _notify_manager_review(followup: dict, reason: str) -> None:
        """Best-effort Bitrix-only notice; a notification failure must not send to MAX."""
        user_id = followup.get("external_user_id") or followup["external_chat_id"]
        user_name = followup.get("external_user_name") or "Пользователь MAX"
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
                                "id": f"followup-review-{followup['id']}",
                                "date": int(time.time()),
                                "text": (
                                    "⚠️ Автоматический follow-up не отправлен: требуется "
                                    f"решение менеджера. Причина: {reason[:500]}"
                                ),
                            },
                            "chat": {
                                "id": str(followup["external_chat_id"]),
                                "name": f"MAX: {user_name}",
                                "url": "https://max.ru",
                            },
                        }
                    ],
                },
            )
        except Exception as exc:
            logger.warning(
                "Could not notify manager about follow-up review: %s", type(exc).__name__
            )

    async def send(self, followup_id: int) -> None:
        followup = self.database.claim_followup_for_send(followup_id)
        if followup is None:
            return
        try:
            await MaxClient().send_message(
                int(followup["dialog_external_chat_id"]), followup["draft_text"]
            )
            self.database.save_message(
                dialog_id=int(followup["dialog_id"]),
                direction="automated_to_max",
                text=followup["draft_text"],
                external_message_id=f"followup-{followup_id}",
            )
            self.database.mark_followup_sent(followup_id)
            await mirror_bot_message_to_bitrix(
                {
                    "external_chat_id": followup["dialog_external_chat_id"],
                    "external_user_id": followup.get("dialog_external_user_id"),
                    "external_user_name": followup.get("dialog_external_user_name"),
                },
                text=followup["draft_text"],
                message_id=f"followup-mirror-{followup_id}",
            )
        except Exception as exc:
            self.database.release_followup_after_error(followup_id, str(exc))
            raise


async def mirror_bot_message_to_bitrix(
    dialog: dict,
    *,
    text: str,
    message_id: str,
) -> bool:
    """Show a MAX bot message in Bitrix without sending it to MAX a second time."""
    user_id = dialog.get("external_user_id") or dialog["external_chat_id"]
    user_name = dialog.get("external_user_name") or "Пользователь MAX"
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
                            "id": message_id,
                            "date": int(time.time()),
                            "text": f"🤖 Бот отправил клиенту:\n\n{text}",
                        },
                        "chat": {
                            "id": str(dialog["external_chat_id"]),
                            "name": f"MAX: {user_name}",
                            "url": "https://max.ru",
                        },
                    }
                ],
            },
        )
        return True
    except Exception as exc:
        logger.warning("Could not mirror bot message to Bitrix: %s", type(exc).__name__)
        return False
