import httpx

from app.config import Config


class MaxClient:

    def _headers(self) -> dict:
        if not Config.MAX_BOT_TOKEN:
            raise RuntimeError("MAX_BOT_TOKEN is not configured")

        return {"Authorization": Config.MAX_BOT_TOKEN}

    async def send_message(self, chat_id: int, text: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{Config.MAX_API_URL}/messages",
                params={"chat_id": chat_id},
                headers=self._headers(),
                json={"text": text},
            )
            response.raise_for_status()
            return response.json()

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
