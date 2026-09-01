import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Config
from app.resources.messages import BITRIX_DIALOG_STARTED_TEXT, MAX_WELCOME_TEXT
from app.services.bitrix_client import BitrixClient
from app.services.followups import mirror_bot_message_to_bitrix
from app.services.manager_tools import ALLOWED_TAGS
from app.services.max_client import MaxClient
from app.services.media import (
    attachment_metadata,
    bitrix_api_file,
    bitrix_file_ids_from_form,
    bitrix_files_from_form,
    max_attachments_to_bitrix_files,
)
from app.services.user_profiles import UserProfileError, UserProfileService
from app.storage.database import MessageDatabase


logger = logging.getLogger(__name__)


class MessageRouter:
    """Routes messages between external channels and Bitrix24 Open Lines."""

    def __init__(self):
        self.database = MessageDatabase()

    def _schedule_followup_analysis(self, *, dialog_id: int, message_id: int) -> None:
        if not Config.FOLLOWUP_ENABLED:
            return
        followup_id = self.database.create_followup_plan(
            dialog_id=dialog_id,
            based_on_message_id=message_id,
        )
        self.database.enqueue(
            job_type="followup_plan",
            payload={"followup_id": followup_id},
            dedupe_key=f"followup:plan:{followup_id}",
            available_at=time.time() + max(1, Config.FOLLOWUP_DELAY_MINUTES) * 60,
        )

    @staticmethod
    def bitrix_to_max_text(text: str) -> str:
        """Turn Bitrix BBCode-style operator messages into readable MAX text."""
        text = text or ""
        text = re.sub(r"\[b\](.+?):\[/b\]\s*\[br\]\s*", r"👤 \1\n\n", text, flags=re.DOTALL)
        text = re.sub(r"\[br\]", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"\[/?(?:b|i|u|s)\]", "", text, flags=re.IGNORECASE)
        return text.strip()

    @staticmethod
    def extract_operator_request(text: str) -> tuple[str, str] | None:
        """Extract a leading slash command and its arguments from Bitrix BBCode."""
        text = text or ""
        text = re.sub(
            r"^\s*\[b\].+?:\[/b\]\s*\[br\]\s*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"\[br\]", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"\[/?(?:b|i|u|s)\]", "", text, flags=re.IGNORECASE).strip()
        candidate = text if text.startswith("/") else None

        # Be tolerant of other Bitrix speaker markup: a command may remain on
        # the final line after the operator name has been stripped partially.
        if candidate is None:
            text = re.sub(r"\[/?[^\]]+\]", "", text).strip()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines and lines[-1].startswith("/") and (
                len(lines) == 1 or " ".join(lines[:-1]).endswith(":")
            ):
                candidate = lines[-1]
        if candidate is None:
            return None
        parts = candidate.split(maxsplit=1)
        return parts[0].casefold(), parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def extract_operator_command(text: str) -> str | None:
        request = MessageRouter.extract_operator_request(text)
        return request[0] if request else None

    @staticmethod
    def _safe_name(value: str | None) -> str:
        name = re.sub(r"[^\w\s'\-]", "", value or "Пользователь MAX", flags=re.UNICODE).strip()
        return name[:25] or "Пользователь"

    @staticmethod
    def _max_message_id(response: dict | None, *, fallback: str) -> str:
        response = response or {}
        body = response.get("body") or {}
        return str(
            response.get("id")
            or response.get("message_id")
            or response.get("mid")
            or body.get("mid")
            or fallback
        )

    def _queue_delivery_status(
        self,
        *,
        im_chat_id: str | None,
        im_message_id: str | None,
        external_chat_id: str,
        external_message_ids: list[str],
    ) -> None:
        if (
            not im_chat_id
            or not str(im_chat_id).isdigit()
            or not im_message_id
            or not str(im_message_id).isdigit()
            or not external_message_ids
        ):
            logger.warning(
                "Bitrix delivery status was not queued: missing im.chat_id, "
                "im.message_id, or external message id"
            )
            return
        self.database.enqueue(
            job_type="bitrix_delivery_status",
            payload={
                "im_chat_id": str(im_chat_id),
                "im_message_id": str(im_message_id),
                "external_chat_id": str(external_chat_id),
                "external_message_ids": [str(item) for item in external_message_ids],
                "delivered_at": int(time.time()),
            },
            dedupe_key=f"bitrix:delivery:{im_chat_id}:{im_message_id}",
        )

    async def from_max(self, update: dict) -> dict:
        """Forward one MAX message_created update to Bitrix24."""
        message = update.get("message") or {}
        body = message.get("body") or {}
        sender = message.get("sender") or update.get("user") or {}
        text = body.get("text")
        attachments = body.get("attachments") or []
        chat_id = update.get("chat_id") or (message.get("recipient") or {}).get("chat_id")
        sender_id = sender.get("user_id") or sender.get("id")

        if not chat_id or not sender_id or (not text and not attachments):
            return {"result": "ignored", "reason": "message data is incomplete"}

        timestamp = int(update.get("timestamp") or message.get("timestamp") or time.time())
        if timestamp > 10_000_000_000:
            timestamp //= 1000

        max_message_id = str(message.get("id") or uuid4())
        user_name = self._safe_name(sender.get("name"))
        bitrix_files = max_attachments_to_bitrix_files(attachments)
        message_payload = {"id": f"max-{max_message_id}", "date": timestamp}
        if text:
            message_payload["text"] = text
        if bitrix_files:
            message_payload["files"] = bitrix_files
        if not text and not bitrix_files:
            message_payload["text"] = "Attachment from MAX could not be transferred"
        result = await BitrixClient().call(
            "imconnector.send.messages",
            {
                "CONNECTOR": Config.MAX_CONNECTOR_ID,
                "LINE": int(Config.BITRIX_OPENLINE_ID),
                "MESSAGES": [
                    {
                        "user": {"id": f"max-{sender_id}", "name": user_name},
                        "message": message_payload,
                        "chat": {
                            "id": str(chat_id),
                            "name": f"MAX: {user_name}",
                            "url": "https://max.ru",
                        },
                    }
                ],
            },
        )

        item = result.get("result", {}).get("DATA", {}).get("RESULT", [{}])[0]
        session = item.get("session") or {}
        dialog_id = self.database.upsert_dialog(
            channel="max",
            external_chat_id=str(chat_id),
            external_user_id=str(sender_id),
            external_user_name=user_name,
            bitrix_chat_id=session.get("CHAT_ID"),
            bitrix_session_id=session.get("ID"),
            line_id=int(Config.BITRIX_OPENLINE_ID),
        )
        message_row_id = self.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text=text,
            external_message_id=max_message_id,
            media=attachment_metadata(attachments),
        )
        self.database.cancel_pending_followups(
            dialog_id, reason="Клиент продолжил диалог"
        )
        self._schedule_followup_analysis(
            dialog_id=dialog_id, message_id=message_row_id
        )
        return {"result": "forwarded", "bitrix": result}

    async def on_max_bot_started(self, update: dict) -> dict:
        """Open a Bitrix24 dialog and greet a user starting the MAX bot."""
        chat_id = update.get("chat_id")
        user = update.get("user") or {}
        user_id = user.get("user_id") or user.get("id")
        if not chat_id or not user_id:
            return {"result": "ignored", "reason": "chat_id or user is missing"}

        chat_id = str(chat_id)
        existing_dialog = self.database.get_dialog(channel="max", external_chat_id=chat_id)
        if existing_dialog and existing_dialog["welcome_sent"]:
            return {"result": "already_started"}

        user_name = self._safe_name(user.get("name"))
        timestamp = int(update.get("timestamp") or time.time())
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        result = None
        if existing_dialog:
            dialog_id = int(existing_dialog["id"])
        else:
            start_message_id = f"max-start-{chat_id}"
            result = await BitrixClient().call(
                "imconnector.send.messages",
                {
                    "CONNECTOR": Config.MAX_CONNECTOR_ID,
                    "LINE": int(Config.BITRIX_OPENLINE_ID),
                    "MESSAGES": [
                        {
                            "user": {"id": f"max-{user_id}", "name": user_name},
                            "message": {
                                "id": start_message_id,
                                "date": timestamp,
                                "text": BITRIX_DIALOG_STARTED_TEXT,
                            },
                            "chat": {
                                "id": chat_id,
                                "name": f"MAX: {user_name}",
                                "url": "https://max.ru",
                            },
                        }
                    ],
                },
            )
            result_items = result.get("result", {}).get("DATA", {}).get("RESULT") or [{}]
            session = (result_items[0].get("session") or {}) if result_items else {}
            dialog_id = self.database.upsert_dialog(
                channel="max",
                external_chat_id=chat_id,
                external_user_id=str(user_id),
                external_user_name=user_name,
                bitrix_chat_id=session.get("CHAT_ID"),
                bitrix_session_id=session.get("ID"),
                line_id=int(Config.BITRIX_OPENLINE_ID),
            )
            self.database.save_message(
                dialog_id=dialog_id,
                direction="max_to_bitrix",
                text=BITRIX_DIALOG_STARTED_TEXT,
                external_message_id=start_message_id,
            )

        await MaxClient().send_message(int(chat_id), MAX_WELCOME_TEXT)
        self.database.mark_welcome_sent(dialog_id)
        dialog = self.database.get_dialog(channel="max", external_chat_id=chat_id)
        if dialog is not None:
            await mirror_bot_message_to_bitrix(
                dialog,
                text=MAX_WELCOME_TEXT,
                message_id=f"max-welcome-mirror-{chat_id}",
            )
        return {"result": "started", "bitrix": result}

    async def from_bitrix(self, form: dict) -> None:
        """Forward an operator reply received from Bitrix24 to MAX."""
        chat_id = form.get("data[MESSAGES][0][chat][id]")
        raw_text = form.get("data[MESSAGES][0][message][text]", "")
        files = bitrix_files_from_form(
            form,
            bitrix_domain=form.get("auth[domain]"),
        )
        file_ids = bitrix_file_ids_from_form(form)
        bitrix_message_id = form.get("data[MESSAGES][0][im][message_id]")
        bitrix_chat_id = form.get("data[MESSAGES][0][im][chat_id]")
        if not chat_id or (not raw_text and not files and not file_ids):
            return

        command_request = self.extract_operator_request(raw_text) if raw_text else None
        if command_request:
            command, arguments = command_request
            await self._handle_operator_command(
                command=command,
                arguments=arguments,
                chat_id=str(chat_id),
                source_message_id=str(bitrix_message_id or raw_text),
                completed_by=form.get("data[MESSAGES][0][message][user_id]"),
            )
            self._queue_delivery_status(
                im_chat_id=str(bitrix_chat_id) if bitrix_chat_id else None,
                im_message_id=str(bitrix_message_id) if bitrix_message_id else None,
                external_chat_id=str(chat_id),
                external_message_ids=[f"connector-command-{bitrix_message_id}"],
            )
            return

        if not files:
            for file_id in file_ids:
                response = await BitrixClient().call("disk.file.get", {"id": file_id})
                resolved_file = bitrix_api_file(response.get("result") or {})
                if resolved_file:
                    files.append(resolved_file)

        text = self.bitrix_to_max_text(raw_text) if raw_text else ""
        max_client = MaxClient()
        external_message_ids: list[str] = []
        if files:
            for index, file in enumerate(files):
                result = await max_client.send_remote_file(
                    int(chat_id),
                    url=str(file["url"]),
                    name=file.get("name"),
                    text=text if index == 0 and text else None,
                )
                external_message_ids.append(
                    self._max_message_id(
                        result,
                        fallback=f"bitrix-{bitrix_message_id or 'unknown'}-{index + 1}",
                    )
                )
        else:
            result = await max_client.send_message(int(chat_id), text)
            external_message_ids.append(
                self._max_message_id(
                    result, fallback=f"bitrix-{bitrix_message_id or 'unknown'}"
                )
            )
        dialog_id = self.database.upsert_dialog(
            channel="max",
            external_chat_id=str(chat_id),
            line_id=int(Config.BITRIX_OPENLINE_ID),
        )
        message_row_id = self.database.save_message(
            dialog_id=dialog_id,
            direction="bitrix_to_max",
            text=text,
            external_message_id=external_message_ids[0] if external_message_ids else None,
            bitrix_message_id=str(bitrix_message_id) if bitrix_message_id else None,
            media=attachment_metadata(files),
        )
        if text or files:
            self._schedule_followup_analysis(
                dialog_id=dialog_id, message_id=message_row_id
            )
        self._queue_delivery_status(
            im_chat_id=str(bitrix_chat_id) if bitrix_chat_id else None,
            im_message_id=str(bitrix_message_id) if bitrix_message_id else None,
            external_chat_id=str(chat_id),
            external_message_ids=external_message_ids,
        )

    async def _handle_operator_command(
        self,
        *,
        command: str,
        arguments: str = "",
        chat_id: str,
        source_message_id: str,
        completed_by: str | None = None,
    ) -> None:
        response_message_id = "internal-command-" + hashlib.sha256(
            f"{chat_id}:{source_message_id}".encode("utf-8")
        ).hexdigest()[:24]
        dialog = self.database.get_dialog(channel="max", external_chat_id=chat_id)
        if dialog is None:
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=chat_id,
                user_name="Пользователь MAX",
                text="⚠️ Не удалось определить пользователя для служебной команды.",
                message_id=response_message_id,
            )
            return

        if command not in {
            "/help",
            "/client",
            "/anketa",
            "/anketa_refresh",
            "/results",
            "/chat_complete",
            "/summary",
            "/summary_all",
            "/remind",
            "/tag",
            "/history",
            "/analytics",
        }:
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=dialog.get("external_user_id") or chat_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=f"⚠️ Неизвестная служебная команда: {command}",
                message_id=response_message_id,
            )
            return

        profile_service = UserProfileService(self.database)
        user_id = dialog.get("external_user_id") or chat_id

        if command == "/help":
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=(
                    "🛠 Команды менеджера\n\n"
                    "/client — карточка клиента\n"
                    "/anketa — рост, вес, возраст и пол\n"
                    "/anketa_refresh — обновить анкету из Google\n"
                    "/results — результаты анализов\n"
                    "/summary — краткое резюме текущего диалога\n"
                    "/summary_all — ИИ-резюме всех обращений клиента\n"
                    "/remind 2h текст — напомнить менеджеру\n"
                    "/tag повторный — добавить тег клиенту\n"
                    "/history — завершённые обращения\n"
                    "/history 1 — открыть первый архив в списке\n"
                    "/analytics — ссылка на защищённую панель\n"
                    "/chat_complete — архивировать завершённый диалог"
                ),
                message_id=response_message_id,
            )
            return

        if command == "/analytics":
            analytics_url = (
                f"{Config.PUBLIC_BASE_URL}/analytics/"
                if Config.PUBLIC_BASE_URL
                else "/analytics/"
            )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=(
                    "📊 Панель аналитики\n\n"
                    f"Адрес: {analytics_url}\n"
                    "Логин: admin\n"
                    "Пароль: значение CONNECTOR_ADMIN_TOKEN из .env\n\n"
                    "Сам пароль в чат не выводится."
                ),
                message_id=response_message_id,
            )
            return

        if command == "/summary":
            accepted = self.database.enqueue(
                job_type="manager_summary",
                payload={
                    "mode": "current",
                    "dialog_id": int(dialog["id"]),
                    "external_user_id": str(user_id),
                    "external_chat_id": chat_id,
                    "external_user_name": dialog.get("external_user_name"),
                },
                dedupe_key=f"manager-summary:{chat_id}:{source_message_id}",
            )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=(
                    "⏳ Готовлю ИИ-резюме текущего диалога. Результат появится "
                    "здесь отдельным сообщением."
                    if accepted
                    else "ℹ️ Этот запрос резюме уже принят в обработку."
                ),
                message_id=response_message_id,
            )
            return

        if command == "/summary_all":
            accepted = self.database.enqueue(
                job_type="manager_summary",
                payload={
                    "mode": "all",
                    "dialog_id": int(dialog["id"]),
                    "external_user_id": str(user_id),
                    "external_chat_id": chat_id,
                    "external_user_name": dialog.get("external_user_name"),
                },
                dedupe_key=f"manager-summary-all:{chat_id}:{source_message_id}",
            )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=(
                    "⏳ Готовлю общее ИИ-резюме всех обращений этого клиента. "
                    "Результат появится здесь отдельным сообщением."
                    if accepted
                    else "ℹ️ Этот запрос общего резюме уже принят в обработку."
                ),
                message_id=response_message_id,
            )
            return

        if command == "/tag":
            tag = arguments.casefold().strip().replace(" ", "_")
            if not tag:
                current = self.database.get_user_tags(str(user_id))
                response_text = (
                    "🏷 Теги клиента: " + ", ".join(current)
                    if current
                    else "🏷 У клиента пока нет тегов."
                )
                response_text += "\nДоступны: " + ", ".join(ALLOWED_TAGS)
            elif tag not in ALLOWED_TAGS:
                response_text = "⚠️ Неизвестный тег. Доступны: " + ", ".join(ALLOWED_TAGS)
            else:
                added = self.database.add_user_tag(
                    external_user_id=str(user_id),
                    tag=tag,
                    created_by=str(completed_by) if completed_by else None,
                )
                response_text = (
                    f"✅ Тег «{tag}» добавлен клиенту."
                    if added
                    else f"ℹ️ Тег «{tag}» уже установлен."
                )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=response_text,
                message_id=response_message_id,
            )
            return

        if command == "/remind":
            match = re.fullmatch(r"(\d+)\s*([mhd])\s+(.+)", arguments, re.DOTALL | re.IGNORECASE)
            if match is None:
                response_text = "⚠️ Формат: /remind 2h текст напоминания (m — минуты, h — часы, d — дни)."
            else:
                amount = int(match.group(1))
                unit = match.group(2).casefold()
                reminder_text = re.sub(r"\s+", " ", match.group(3)).strip()
                seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
                max_seconds = max(1, Config.REMINDER_MAX_DAYS) * 86400
                if amount < 1 or seconds > max_seconds or len(reminder_text) > 1000:
                    response_text = (
                        f"⚠️ Срок должен быть от 1 минуты до {Config.REMINDER_MAX_DAYS} дней, "
                        "а текст — не длиннее 1000 символов."
                    )
                else:
                    due_at = time.time() + seconds
                    reminder = self.database.create_manager_reminder(
                        dialog_id=int(dialog["id"]),
                        external_user_id=str(user_id),
                        external_chat_id=chat_id,
                        external_user_name=dialog.get("external_user_name"),
                        manager_id=str(completed_by) if completed_by else None,
                        reminder_text=reminder_text,
                        due_at=due_at,
                        source_message_id=f"{chat_id}:{source_message_id}",
                    )
                    try:
                        timezone_info = ZoneInfo(Config.FOLLOWUP_TIMEZONE)
                    except ZoneInfoNotFoundError:
                        timezone_info = timezone(timedelta(hours=3))
                    due_text = datetime.fromtimestamp(due_at, timezone_info).strftime(
                        "%d.%m.%Y %H:%M"
                    )
                    response_text = (
                        f"✅ Напоминание №{reminder['id']} установлено на {due_text}.\n"
                        f"Текст: {reminder_text}"
                    )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=response_text,
                message_id=response_message_id,
            )
            return

        if command == "/history":
            position_text = arguments.strip()
            if not position_text:
                archives = self.database.list_user_chat_archives(
                    external_user_id=dialog.get("external_user_id"),
                    external_chat_id=chat_id,
                    limit=10,
                )
                if not archives:
                    response_text = "ℹ️ У клиента пока нет завершённых обращений."
                else:
                    lines = ["🗂 Завершённые обращения (сначала новые):", ""]
                    for index, archive in enumerate(archives, start=1):
                        completed = str(archive.get("completed_at") or "дата не указана")
                        lines.append(
                            f"{index}. {completed} — {archive['message_count']} сообщений "
                            f"(архив №{archive['id']})"
                        )
                    lines.append("\nОткрыть: /history 1")
                    response_text = "\n".join(lines)
            elif not position_text.isdigit() or int(position_text) < 1:
                response_text = "⚠️ Формат: /history или /history 1"
            else:
                position = int(position_text)
                archive = self.database.get_user_chat_archive(
                    position=position,
                    external_user_id=dialog.get("external_user_id"),
                    external_chat_id=chat_id,
                )
                if archive is None:
                    response_text = f"🔍 Архив под номером {position} для этого клиента не найден."
                else:
                    transcript = str(archive["transcript_text"])
                    if len(transcript) > 14000:
                        transcript = transcript[:13900].rstrip() + "\n\n[Архив сокращён для показа в чате]"
                    response_text = f"🗂 Архив №{archive['id']}\n\n{transcript}"
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=response_text,
                message_id=response_message_id,
            )
            return

        if command == "/chat_complete":
            self.database.cancel_pending_followups(
                int(dialog["id"]), reason="Диалог завершён менеджером"
            )
            archive = self.database.archive_dialog_messages(
                dialog_id=int(dialog["id"]),
                completed_by=str(completed_by) if completed_by else None,
                source_message_id=f"{chat_id}:{source_message_id}",
            )
            response_text = (
                f"✅ Диалог сохранён в архиве №{archive['id']}. "
                f"Сообщений: {archive['message_count']}. Рабочая история очищена."
                if archive
                else "ℹ️ В рабочей истории нет сообщений для архивирования."
            )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=response_text,
                message_id=response_message_id,
            )
            return

        if command == "/client":
            try:
                profile = await profile_service.get_client_profile(user_id)
            except UserProfileError:
                logger.exception("Could not load client card for MAX user %s", user_id)
                response_text = "⚠️ Не удалось получить карточку. Повторите команду позже."
            else:
                response_text = (
                    profile_service.format_client(
                        profile,
                        fallback_name=dialog.get("external_user_name"),
                    )
                    if profile
                    else f"🔍 Карточка пользователя с ID {user_id} не найдена."
                )
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=response_text,
                message_id=response_message_id,
            )
            return

        if command == "/results":
            try:
                result = await profile_service.get_results(user_id)
            except UserProfileError:
                logger.exception("Could not load results for MAX user %s", user_id)
                response_text = "⚠️ Не удалось получить результаты. Повторите команду позже."
            else:
                if not result:
                    response_text = f"🔍 Пользователь с ID {user_id} не найден."
                elif not result.get("med_id"):
                    response_text = "ℹ️ Медицинский ID пользователя пока не назначен."
                elif not result.get("results"):
                    response_text = "ℹ️ Результаты анализов пока не готовы."
                else:
                    response_text = profile_service.format_results(result)
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=user_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=response_text,
                message_id=response_message_id,
            )
            return

        try:
            profile = await profile_service.get_profile(
                user_id,
                refresh=command == "/anketa_refresh",
            )
        except UserProfileError:
            logger.exception("Could not load Google Sheets profile for MAX user %s", user_id)
            response_text = (
                "⚠️ Не удалось получить анкету. Повторите команду позже."
            )
        else:
            response_text = (
                profile_service.format_profile(profile)
                if profile
                else f"🔍 Анкета пользователя с ID {user_id} не найдена."
            )

        await self._send_internal_message(
            chat_id=chat_id,
            user_id=user_id,
            user_name=dialog.get("external_user_name") or "Пользователь MAX",
            text=response_text,
            message_id=response_message_id,
        )

    async def _send_internal_message(
        self,
        *,
        chat_id: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: str | None = None,
    ) -> None:
        """Show a service response in Bitrix without sending it to MAX."""
        message_id = message_id or f"internal-{uuid4()}"
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
                            "text": text,
                        },
                        "chat": {
                            "id": chat_id,
                            "name": f"MAX: {user_name}",
                            "url": "https://max.ru",
                        },
                    }
                ],
            },
        )
