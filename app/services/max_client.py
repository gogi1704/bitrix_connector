import asyncio
import logging
from pathlib import Path

import httpx

from app.config import Config
from app.services.media import (
    MediaTransferError,
    download_to_temporary_file,
    media_type,
    validate_remote_https_url,
)


logger = logging.getLogger(__name__)


class MaxClient:

    def _headers(self) -> dict:
        if not Config.MAX_BOT_TOKEN:
            raise RuntimeError("MAX_BOT_TOKEN is not configured")

        return {"Authorization": Config.MAX_BOT_TOKEN}

    async def send_message(
        self,
        chat_id: int,
        text: str | None,
        attachments: list[dict] | None = None,
    ) -> dict:
        payload = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{Config.MAX_API_URL}/messages",
                params={"chat_id": chat_id},
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def _upload_file(self, path: Path, name: str, content_type: str) -> dict:
        kind = media_type(name, content_type)
        async with httpx.AsyncClient(timeout=Config.MEDIA_DOWNLOAD_TIMEOUT) as client:
            prepare = await client.post(
                f"{Config.MAX_API_URL}/uploads",
                params={"type": kind},
                headers=self._headers(),
            )
            prepare.raise_for_status()
            upload_data = prepare.json()
            upload_url = upload_data.get("url")
            if not upload_url:
                raise MediaTransferError("MAX did not return a media upload URL")
            validate_remote_https_url(upload_url)

            with path.open("rb") as source:
                uploaded = await client.post(
                    upload_url,
                    files={"data": (name, source, content_type)},
                )
            uploaded.raise_for_status()
            try:
                result = uploaded.json() if uploaded.content else {}
            except ValueError:
                # Video/audio upload hosts may return an empty or non-JSON
                # success body. Their reusable token is returned by /uploads.
                result = {}

        token = result.get("token") or upload_data.get("token")
        if token:
            payload = {"token": token}
        elif kind == "image" and result.get("photos"):
            # Image upload responses can contain PhotoTokens instead of a
            # single token. MAX expects that object unchanged in payload.
            payload = result
        elif kind == "image" and upload_data.get("photos"):
            payload = {"photos": upload_data["photos"]}
        else:
            raise MediaTransferError(
                f"MAX did not return attachment data for media type {kind}"
            )

        logger.info("MAX media upload completed: type=%s", kind)
        return {"type": kind, "payload": payload}

    async def send_remote_file(
        self,
        chat_id: int,
        *,
        url: str,
        name: str | None,
        text: str | None = None,
    ) -> dict:
        path, safe_name, content_type = await download_to_temporary_file(url, name)
        try:
            attachment = await self._upload_file(path, safe_name, content_type)
            for delay in (0, 1, 2, 4):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    return await self.send_message(chat_id, text, [attachment])
                except httpx.HTTPStatusError as exc:
                    try:
                        code = exc.response.json().get("code")
                    except Exception:
                        code = None
                    if code != "attachment.not.ready" or delay == 4:
                        raise
            raise MediaTransferError("MAX media did not become ready")
        finally:
            path.unlink(missing_ok=True)

    async def subscribe(self, webhook_url: str) -> dict:
        payload = {
            "url": webhook_url,
            "update_types": ["message_created", "bot_started"],
        }
        if Config.MAX_WEBHOOK_SECRET:
            payload["secret"] = Config.MAX_WEBHOOK_SECRET

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{Config.MAX_API_URL}/subscriptions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()
