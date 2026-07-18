import hashlib
import logging
import re
import time
from uuid import uuid4

from app.config import Config
from app.resources.messages import BITRIX_DIALOG_STARTED_TEXT, MAX_WELCOME_TEXT
from app.services.bitrix_client import BitrixClient
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

    @staticmethod
    def bitrix_to_max_text(text: str) -> str:
        """Turn Bitrix BBCode-style operator messages into readable MAX text."""
        text = text or ""
        text = re.sub(r"\[b\](.+?):\[/b\]\s*\[br\]\s*", r"👤 \1\n\n", text, flags=re.DOTALL)
        text = re.sub(r"\[br\]", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"\[/?(?:b|i|u|s)\]", "", text, flags=re.IGNORECASE)
        return text.strip()

    @staticmethod
    def extract_operator_command(text: str) -> str | None:
        """Extract a leading slash command from Bitrix operator BBCode."""
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
        if text.startswith("/"):
            return text.split(maxsplit=1)[0].casefold()

        # Be tolerant of other Bitrix speaker markup: a command may remain on
        # the final line after the operator name has been stripped partially.
        text = re.sub(r"\[/?[^\]]+\]", "", text).strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and lines[-1].startswith("/") and (
            len(lines) == 1 or " ".join(lines[:-1]).endswith(":")
        ):
            return lines[-1].split(maxsplit=1)[0].casefold()
        return None

    @staticmethod
    def _safe_name(value: str | None) -> str:
        name = re.sub(r"[^\w\s'\-]", "", value or "Пользователь MAX", flags=re.UNICODE).strip()
        return name[:25] or "Пользователь"

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
        self.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text=text,
            external_message_id=max_message_id,
            media=attachment_metadata(attachments),
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
        return {"result": "started", "bitrix": result}

    async def from_bitrix(self, form: dict) -> None:
        """Forward an operator reply received from Bitrix24 to MAX."""
        chat_id = form.get("data[MESSAGES][0][chat][id]")
        raw_text = form.get("data[MESSAGES][0][message][text]", "")
        files = bitrix_files_from_form(form)
        file_ids = bitrix_file_ids_from_form(form)
        bitrix_message_id = form.get("data[MESSAGES][0][im][message_id]")
        if not chat_id or (not raw_text and not files and not file_ids):
            return

        command = self.extract_operator_command(raw_text) if raw_text else None
        if command:
            await self._handle_operator_command(
                command=command,
                chat_id=str(chat_id),
                source_message_id=str(bitrix_message_id or raw_text),
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
        if files:
            for index, file in enumerate(files):
                await max_client.send_remote_file(
                    int(chat_id),
                    url=str(file["url"]),
                    name=file.get("name"),
                    text=text if index == 0 and text else None,
                )
        else:
            await max_client.send_message(int(chat_id), text)
        dialog_id = self.database.upsert_dialog(
            channel="max",
            external_chat_id=str(chat_id),
            line_id=int(Config.BITRIX_OPENLINE_ID),
        )
        self.database.save_message(
            dialog_id=dialog_id,
            direction="bitrix_to_max",
            text=text,
            bitrix_message_id=str(bitrix_message_id) if bitrix_message_id else None,
            media=attachment_metadata(files),
        )

    async def _handle_operator_command(
        self,
        *,
        command: str,
        chat_id: str,
        source_message_id: str,
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

        if command not in {"/anketa", "/anketa_refresh"}:
            await self._send_internal_message(
                chat_id=chat_id,
                user_id=dialog.get("external_user_id") or chat_id,
                user_name=dialog.get("external_user_name") or "Пользователь MAX",
                text=f"⚠️ Неизвестная служебная команда: {command}",
                dialog_id=int(dialog["id"]),
                message_id=response_message_id,
            )
            return

        profile_service = UserProfileService(self.database)
        user_id = dialog.get("external_user_id") or chat_id
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
            dialog_id=int(dialog["id"]),
            message_id=response_message_id,
        )

    async def _send_internal_message(
        self,
        *,
        chat_id: str,
        user_id: str,
        user_name: str,
        text: str,
        dialog_id: int | None = None,
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
        if dialog_id is not None:
            self.database.save_message(
                dialog_id=dialog_id,
                direction="internal_to_bitrix",
                text=text,
                external_message_id=message_id,
            )
