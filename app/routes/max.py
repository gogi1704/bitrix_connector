import secrets
from fastapi import APIRouter, HTTPException, Request

from app.config import Config
from app.storage.database import MessageDatabase

router = APIRouter(prefix="/max", tags=["MAX"])


@router.post("/webhook")
async def receive_max_webhook(request: Request):
    """Forward incoming MAX messages to the configured Bitrix24 Open Line."""
    if not Config.MAX_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="MAX webhook secret is not configured")

    received_secret = request.headers.get("X-Max-Bot-Api-Secret", "")
    if not secrets.compare_digest(Config.MAX_WEBHOOK_SECRET, received_secret):
        raise HTTPException(status_code=403, detail="Invalid MAX webhook secret")

    update = await request.json()
    update_type = update.get("update_type")
    if update_type not in {"bot_started", "message_created"}:
        return {"result": "ignored"}

    message = update.get("message") or {}
    chat_id = update.get("chat_id") or (message.get("recipient") or {}).get("chat_id")
    database = MessageDatabase()
    dedupe_key = f"max:{update_type}:{message.get('id') or chat_id}:{update.get('timestamp', '')}"
    accepted = database.enqueue(
        job_type="max_update", payload=update, dedupe_key=dedupe_key
    )
    body = message.get("body") or {}
    if (
        accepted
        and update_type == "message_created"
        and chat_id is not None
        and (body.get("text") or body.get("attachments"))
    ):
        dialog = database.get_dialog(channel="max", external_chat_id=str(chat_id))
        if dialog is not None:
            database.cancel_pending_followups(
                int(dialog["id"]), reason="Клиент продолжил диалог"
            )
    return {"result": "accepted", "duplicate": not accepted}
