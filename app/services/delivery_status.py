import time

from app.config import Config
from app.services.bitrix_client import BitrixClient


class DeliveryStatusService:
    """Confirm successful external delivery of a Bitrix operator message."""

    @staticmethod
    async def send(payload: dict) -> None:
        result = await BitrixClient().call(
            "imconnector.send.status.delivery",
            {
                "CONNECTOR": Config.MAX_CONNECTOR_ID,
                "LINE": int(Config.BITRIX_OPENLINE_ID),
                "MESSAGES": [
                    {
                        "im": {
                            "chat_id": int(payload["im_chat_id"]),
                            "message_id": int(payload["im_message_id"]),
                        },
                        "message": {
                            "id": [str(item) for item in payload["external_message_ids"]],
                            "date": int(payload.get("delivered_at") or time.time()),
                        },
                        "chat": {"id": str(payload["external_chat_id"])},
                    }
                ],
            },
        )
        method_result = result.get("result") or {}
        if method_result.get("SUCCESS") is not True:
            raise RuntimeError("Bitrix did not accept the delivery status")
