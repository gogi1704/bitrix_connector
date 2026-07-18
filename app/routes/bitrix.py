import hashlib
import json
import logging
import secrets
import time
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import Config, DATA_DIR
from app.services.bitrix_client import BitrixApiError, BitrixClient
from app.services.max_client import MaxClient
from app.services.media import attachment_field_names
from app.services.oauth import OAuthService
from app.storage.database import MessageDatabase

router = APIRouter(prefix="/bitrix", tags=["Bitrix"])
logger = logging.getLogger(__name__)

TEST_CONNECTOR_ID = "bitrix_connector_test"
TEST_CONNECTOR_ICON = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%2F%3E"


def require_admin_token(request: Request) -> None:
    """Protect endpoints that mutate or expose connector configuration."""
    expected_token = Config.CONNECTOR_ADMIN_TOKEN or OAuthService().load().get(
        "application_token"
    )
    if not expected_token:
        raise HTTPException(status_code=503, detail="Connector admin token is not configured")

    received_token = request.headers.get("X-Connector-Admin-Token", "")
    if not secrets.compare_digest(expected_token, received_token):
        raise HTTPException(status_code=403, detail="Invalid connector admin token")


def get_auth_data(form: dict) -> dict:
    """Extract the nested auth object from Bitrix form-urlencoded callback."""
    auth = form.get("auth")
    if isinstance(auth, dict):
        return auth

    return {
        key.removeprefix("auth[").removesuffix("]"): value
        for key, value in form.items()
        if key.startswith("auth[") and key.endswith("]")
    }


@router.post("/install")
async def install(request: Request):

    form = dict(await request.form())
    auth = get_auth_data(form)

    if not auth.get("access_token") or not auth.get("refresh_token"):
        raise HTTPException(status_code=400, detail="Bitrix OAuth auth data is required")

    expected_token = Config.BITRIX_APPLICATION_TOKEN or OAuthService().load().get(
        "application_token"
    )
    received_token = str(auth.get("application_token", ""))
    if not expected_token:
        raise HTTPException(status_code=503, detail="Bitrix application token is not configured")
    if not secrets.compare_digest(expected_token, received_token):
        raise HTTPException(status_code=403, detail="Invalid Bitrix application token")

    # Bitrix sends OAuth credentials in the installation form. Store them in
    # the same place used by BitrixClient so calls work immediately after install.
    OAuthService().save(auth)

    return {"result": "ok"}


@router.get("/test/outbound", dependencies=[Depends(require_admin_token)])
async def test_outbound():
    """Verify that this app can call Bitrix24 REST API."""
    return await BitrixClient().call("app.info")


@router.post("/test/bind", dependencies=[Depends(require_admin_token)])
async def bind_test_event():
    """Register a harmless ONAPPTEST event handler in Bitrix24."""
    if not Config.PUBLIC_BASE_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is not configured")

    handler = f"{Config.PUBLIC_BASE_URL}/bitrix/events"
    result = await BitrixClient().call(
        "event.bind",
        {"event": "ONAPPTEST", "handler": handler},
    )
    return {"handler": handler, "bitrix": result}


@router.post("/test/trigger", dependencies=[Depends(require_admin_token)])
async def trigger_test_event():
    """Ask Bitrix24 to send the registered ONAPPTEST callback."""
    return await BitrixClient().call("event.test", {"source": "bitrix_connector"})


@router.post("/events")
async def receive_event(request: Request):
    """Receive and record a Bitrix24 event without retaining OAuth secrets."""
    form = dict(await request.form())
    auth = get_auth_data(form)
    expected_token = Config.BITRIX_APPLICATION_TOKEN or OAuthService().load().get(
        "application_token"
    )
    received_token = auth.get("application_token")

    if not expected_token:
        raise HTTPException(status_code=503, detail="Bitrix application token is not configured")
    if not secrets.compare_digest(str(expected_token), str(received_token or "")):
        raise HTTPException(status_code=403, detail="Invalid Bitrix application token")

    media_fields = attachment_field_names(form)
    event = {
        "event": form.get("event"),
        "data": {
            key: "<redacted-media-value>" if key in media_fields else value
            for key, value in form.items()
            if not key.startswith("auth[")
        },
    }
    if media_fields:
        logger.info("Bitrix media field names: %s", media_fields)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "events.jsonl", "a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")

    if (
        form.get("event", "").upper() == "ONIMCONNECTORMESSAGEADD"
        and form.get("data[CONNECTOR]") == Config.MAX_CONNECTOR_ID
    ):
        message_id = form.get("data[MESSAGES][0][im][message_id]")
        if not message_id:
            canonical_data = json.dumps(event["data"], ensure_ascii=False, sort_keys=True)
            message_id = hashlib.sha256(canonical_data.encode("utf-8")).hexdigest()
        MessageDatabase().enqueue(
            job_type="bitrix_operator_message",
            payload=form,
            dedupe_key=f"bitrix:max:{message_id}",
        )

    return {"result": "received", "event": event["event"]}


@router.post("/openlines/test", dependencies=[Depends(require_admin_token)])
async def send_test_openline_message(
    line_id: int | None = None,
    message: str = "Здравствуйте! Это тестовое сообщение от клиента 777.",
):
    """Pass a message from external client 777 to an existing Open Line."""
    if not Config.PUBLIC_BASE_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is not configured")

    line_id = line_id or int(Config.BITRIX_OPENLINE_ID)

    client = BitrixClient()

    await client.call(
        "imconnector.register",
        {
            "ID": TEST_CONNECTOR_ID,
            "NAME": "Тестовый коннектор Bitrix Connector",
            "ICON": {
                "DATA_IMAGE": TEST_CONNECTOR_ICON,
                "COLOR": "#69acc0",
                "SIZE": "90%",
                "POSITION": "center",
            },
            "PLACEMENT_HANDLER": f"{Config.PUBLIC_BASE_URL}/",
            "CHAT_GROUP": False,
        },
    )
    await client.call(
        "imconnector.activate",
        {"CONNECTOR": TEST_CONNECTOR_ID, "LINE": line_id, "ACTIVE": "1"},
    )
    await client.call(
        "imconnector.connector.data.set",
        {
            "CONNECTOR": TEST_CONNECTOR_ID,
            "LINE": line_id,
            "DATA": {
                "ID": "test-client-777",
                "NAME": "Тестовый чат клиента 777",
                "URL": Config.PUBLIC_BASE_URL,
            },
        },
    )

    sent = await client.call(
        "imconnector.send.messages",
        {
            "CONNECTOR": TEST_CONNECTOR_ID,
            "LINE": line_id,
            "MESSAGES": [
                {
                    "user": {"id": "777", "name": "Клиент 777", "disable_crm": "Y"},
                    "message": {
                        "id": f"test-{uuid4()}",
                        "date": int(time.time()),
                        "text": message,
                        "disable_crm": "Y",
                    },
                    "chat": {
                        "id": "test-client-777",
                        "name": "Тестовый чат клиента 777",
                        "url": Config.PUBLIC_BASE_URL,
                    },
                }
            ],
        },
    )

    return {"line_id": line_id, "client_id": "777", "bitrix": sent}


@router.get("/openlines", dependencies=[Depends(require_admin_token)])
async def list_openlines():
    """Return Open Lines available to the installed Bitrix24 application."""
    return await BitrixClient().call("imopenlines.config.list.get")


@router.post("/openlines/test/bind", dependencies=[Depends(require_admin_token)])
async def bind_openline_message_event():
    """Subscribe to messages sent by a Bitrix24 operator into the connector."""
    if not Config.PUBLIC_BASE_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is not configured")

    handler = f"{Config.PUBLIC_BASE_URL}/bitrix/events"
    client = BitrixClient()
    registered_handlers = await client.call("event.get")
    stale_handlers = [
        item["handler"]
        for item in registered_handlers.get("result", [])
        if (
            item.get("event", "").upper() == "ONIMCONNECTORMESSAGEADD"
            and item.get("handler") != handler
        )
    ]

    for stale_handler in stale_handlers:
        await client.call(
            "event.unbind",
            {"event": "OnImConnectorMessageAdd", "handler": stale_handler},
        )

    is_bound = any(
        item.get("event", "").upper() == "ONIMCONNECTORMESSAGEADD"
        and item.get("handler") == handler
        for item in registered_handlers.get("result", [])
    )
    if is_bound:
        return {"handler": handler, "already_bound": True, "removed_stale": stale_handlers}

    try:
        result = await client.call(
            "event.bind",
            {"event": "OnImConnectorMessageAdd", "handler": handler},
        )
    except BitrixApiError as exc:
        if (
            exc.payload.get("error") == "ERROR_CORE"
            and "Handler already binded" in exc.payload.get("error_description", "")
        ):
            return {"handler": handler, "already_bound": True}
        raise

    return {"handler": handler, "bitrix": result, "removed_stale": stale_handlers}


@router.get("/openlines/test/handlers", dependencies=[Depends(require_admin_token)])
async def list_openline_event_handlers():
    """Show registered Bitrix24 handlers for diagnosing connector callbacks."""
    return await BitrixClient().call("event.get")


@router.post("/max/setup", dependencies=[Depends(require_admin_token)])
async def setup_max_connector():
    """Connect the MAX bot webhook and its custom connector to the Open Line."""
    if not Config.PUBLIC_BASE_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is not configured")

    line_id = int(Config.BITRIX_OPENLINE_ID)
    client = BitrixClient()
    await client.call(
        "imconnector.register",
        {
            "ID": Config.MAX_CONNECTOR_ID,
            "NAME": "MAX Bot",
            "ICON": {
                "DATA_IMAGE": TEST_CONNECTOR_ICON,
                "COLOR": "#4d7cff",
                "SIZE": "90%",
                "POSITION": "center",
            },
            "PLACEMENT_HANDLER": f"{Config.PUBLIC_BASE_URL}/",
            "CHAT_GROUP": False,
        },
    )
    await client.call(
        "imconnector.activate",
        {"CONNECTOR": Config.MAX_CONNECTOR_ID, "LINE": line_id, "ACTIVE": "1"},
    )
    await client.call(
        "imconnector.connector.data.set",
        {
            "CONNECTOR": Config.MAX_CONNECTOR_ID,
            "LINE": line_id,
            "DATA": {
                "ID": "max-bot",
                "NAME": "MAX Bot",
                "URL": "https://max.ru",
            },
        },
    )
    event_binding = await bind_openline_message_event()
    webhook_url = f"{Config.PUBLIC_BASE_URL}/max/webhook"
    subscription = await MaxClient().subscribe(webhook_url)

    return {
        "line_id": line_id,
        "connector": Config.MAX_CONNECTOR_ID,
        "webhook_url": webhook_url,
        "event_binding": event_binding,
        "max_subscription": subscription,
    }
