import hashlib
import json
import logging
import re
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


def tokens_equal(expected: str | None, received: str | None) -> bool:
    """Compare arbitrary token strings without compare_digest ASCII errors."""
    return secrets.compare_digest(
        str(expected or "").encode("utf-8"),
        str(received or "").encode("utf-8"),
    )


def require_admin_token(request: Request) -> None:
    """Protect endpoints that mutate or expose connector configuration."""
    expected_token = Config.CONNECTOR_ADMIN_TOKEN
    if not expected_token:
        raise HTTPException(status_code=503, detail="Connector admin token is not configured")

    received_token = request.headers.get("X-Connector-Admin-Token", "")
    if not tokens_equal(expected_token, received_token):
        raise HTTPException(status_code=403, detail="Invalid connector admin token")


def require_consilium_payment_secret(request: Request) -> None:
    """Allow payment events only from the Consilium backend."""
    expected_token = Config.CONSILIUM_PAYMENT_SECRET
    if not expected_token:
        raise HTTPException(status_code=503, detail="Consilium payment integration is not configured")
    received_token = request.headers.get("X-Consilium-Payment-Secret", "")
    if not tokens_equal(expected_token, received_token):
        raise HTTPException(status_code=403, detail="Invalid Consilium payment secret")


def require_consilium_metrics_secret(request: Request) -> None:
    expected_token = Config.CONSILIUM_METRICS_SECRET
    if not expected_token:
        raise HTTPException(status_code=503, detail="Consilium metrics integration is not configured")
    received_token = request.headers.get("X-Consilium-Metrics-Secret", "")
    if not tokens_equal(expected_token, received_token):
        raise HTTPException(status_code=403, detail="Invalid Consilium metrics secret")


def validate_funnel_report(payload) -> dict:
    """Accept only a bounded aggregate report; individual user data is forbidden."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON object is required")
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 256_000:
        raise HTTPException(status_code=413, detail="Metrics report is too large")
    report_id = str(payload.get("report_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", report_id):
        raise HTTPException(status_code=422, detail="Invalid report_id")
    if payload.get("schema_version") != 1:
        raise HTTPException(status_code=422, detail="Unsupported schema_version")
    dialog_id = str(payload.get("dialog_id", "")).strip()
    if dialog_id and not re.fullmatch(r"(?:chat|sg)?\d+", dialog_id):
        raise HTTPException(status_code=422, detail="Invalid dialog_id")
    if not dialog_id and not Config.BITRIX_METRICS_DIALOG_ID:
        raise HTTPException(status_code=422, detail="Metrics dialog is required")
    for period_name in ("current_period", "comparison_period"):
        period = payload.get(period_name)
        if not isinstance(period, dict) or any(
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(period.get(key, "")))
            for key in ("date_from", "date_to")
        ):
            raise HTTPException(status_code=422, detail=f"Invalid {period_name}")
    if not isinstance(payload.get("current"), dict) or not isinstance(payload.get("comparison"), dict):
        raise HTTPException(status_code=422, detail="Current and comparison aggregates are required")
    flows = payload.get("flows")
    if not isinstance(flows, list) or len(flows) > 2:
        raise HTTPException(status_code=422, detail="Invalid flows")
    for flow in flows:
        if not isinstance(flow, dict) or flow.get("id") not in {"standard", "result"}:
            raise HTTPException(status_code=422, detail="Invalid flow")
        if not isinstance(flow.get("screens"), list) or len(flow["screens"]) > 100:
            raise HTTPException(status_code=422, detail="Invalid flow screens")
        if not isinstance(flow.get("alerts"), list) or len(flow["alerts"]) > 20:
            raise HTTPException(status_code=422, detail="Invalid flow alerts")
    forbidden = {"chel_id", "phone", "tube_number", "messages", "answers", "recent"}

    def inspect(value, depth: int = 0) -> None:
        if depth > 8:
            raise HTTPException(status_code=422, detail="Metrics report is too deeply nested")
        if isinstance(value, dict):
            if len(value) > 100 or forbidden.intersection(value):
                raise HTTPException(status_code=422, detail="Metrics report contains forbidden fields")
            for nested in value.values():
                inspect(nested, depth + 1)
        elif isinstance(value, list):
            if len(value) > 200:
                raise HTTPException(status_code=422, detail="Metrics report list is too large")
            for nested in value:
                inspect(nested, depth + 1)
        elif isinstance(value, str) and len(value) > 2_000:
            raise HTTPException(status_code=422, detail="Metrics report text is too long")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise HTTPException(status_code=422, detail="Metrics report contains an invalid value")

    inspect(payload)
    return payload


def validate_payment_notification(payload) -> dict:
    """Return a bounded, queue-safe notification without accepting arbitrary data."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON object is required")

    def required_text(name: str, maximum: int, pattern: str | None = None) -> str:
        value = str(payload.get(name, "")).strip()
        if not value or len(value) > maximum or (pattern and not re.fullmatch(pattern, value)):
            raise HTTPException(status_code=422, detail=f"Invalid {name}")
        return value

    order_id = required_text("order_id", 80, r"[A-Za-z0-9_-]+")
    provider_payment_id = required_text("provider_payment_id", 80, r"[A-Za-z0-9-]+")
    status = required_text("status", 30)
    if status != "succeeded":
        raise HTTPException(status_code=422, detail="Only succeeded payments are accepted")
    currency = required_text("currency", 3, r"[A-Z]{3}")
    try:
        amount_kopecks = int(payload.get("amount_kopecks"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid amount_kopecks") from None
    if amount_kopecks <= 0 or amount_kopecks > 100_000_000_000:
        raise HTTPException(status_code=422, detail="Invalid amount_kopecks")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 100:
        raise HTTPException(status_code=422, detail="Invalid items")
    items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise HTTPException(status_code=422, detail="Invalid item")
        name = str(raw_item.get("name", "")).strip()
        try:
            item_amount = int(raw_item.get("amount_kopecks"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid item amount") from None
        if not name or len(name) > 128 or item_amount <= 0:
            raise HTTPException(status_code=422, detail="Invalid item")
        items.append({"name": name, "amount_kopecks": item_amount})

    company_inn = str(payload.get("company_inn", "")).strip()
    if company_inn and not re.fullmatch(r"\d{10}|\d{12}", company_inn):
        raise HTTPException(status_code=422, detail="Invalid company_inn")
    examination_date = str(payload.get("examination_date", "")).strip()
    if examination_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", examination_date):
        raise HTTPException(status_code=422, detail="Invalid examination_date")
    return {
        "order_id": order_id,
        "provider_payment_id": provider_payment_id,
        "status": status,
        "amount_kopecks": amount_kopecks,
        "currency": currency,
        "client_name": str(payload.get("client_name", "")).strip()[:100],
        "company_inn": company_inn,
        "organization_name": str(payload.get("organization_name", "")).strip()[:300],
        "brigade": str(payload.get("brigade", "")).strip()[:200],
        "examination_date": examination_date,
        "paid_at": str(payload.get("paid_at", "")).strip()[:80],
        "provider_created_at": str(payload.get("provider_created_at", "")).strip()[:80],
        "provider_description": str(payload.get("provider_description", "")).strip()[:300],
        "payment_method": str(payload.get("payment_method", "")).strip()[:100],
        "test": bool(payload.get("test")),
        "items": items,
    }


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


def operator_job_payload(form: dict) -> dict:
    """Remove callback secrets while retaining the domain needed for Disk files."""
    return {
        key: value
        for key, value in form.items()
        if not key.startswith("auth[") or key == "auth[domain]"
    }


@router.post("/install")
async def install(request: Request):

    if not Config.BITRIX_INSTALL_TOKEN:
        raise HTTPException(status_code=503, detail="Bitrix install token is not configured")
    received_install_token = request.query_params.get("install_token", "")
    if not tokens_equal(Config.BITRIX_INSTALL_TOKEN, received_install_token):
        raise HTTPException(status_code=403, detail="Invalid Bitrix install token")

    form = dict(await request.form())
    auth = get_auth_data(form)

    if not all(
        auth.get(field)
        for field in ("access_token", "refresh_token", "application_token")
    ):
        raise HTTPException(status_code=400, detail="Bitrix OAuth auth data is required")

    oauth = OAuthService()
    existing_auth = oauth.load()
    existing_member_id = existing_auth.get("member_id")
    received_member_id = auth.get("member_id")
    if existing_member_id and received_member_id and not tokens_equal(
        existing_member_id, received_member_id
    ):
        raise HTTPException(status_code=409, detail="Another Bitrix portal is already installed")

    # Bitrix sends OAuth credentials in the installation form. Store them in
    # the same place used by BitrixClient so calls work immediately after install.
    oauth.save({**existing_auth, **auth})

    return {"result": "ok"}


@router.get("/test/outbound", dependencies=[Depends(require_admin_token)])
async def test_outbound():
    """Verify that this app can call Bitrix24 REST API."""
    return await BitrixClient().call("app.info")


@router.post(
    "/payments/consilium",
    status_code=202,
    dependencies=[Depends(require_consilium_payment_secret)],
)
async def receive_consilium_payment(request: Request):
    """Persist a verified payment event and deliver it asynchronously to Bitrix."""
    try:
        payload = validate_payment_notification(await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    accepted = MessageDatabase().enqueue(
        job_type="consilium_payment_notification",
        payload=payload,
        dedupe_key=f"consilium:payment:{payload['order_id']}",
    )
    return {"status": "queued" if accepted else "duplicate", "order_id": payload["order_id"]}


@router.post(
    "/metrics/consilium",
    status_code=202,
    dependencies=[Depends(require_consilium_metrics_secret)],
)
async def receive_consilium_funnel_report(request: Request):
    """Queue a privacy-safe aggregate funnel snapshot for a Bitrix project chat."""
    try:
        payload = validate_funnel_report(await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    accepted = MessageDatabase().enqueue(
        job_type="consilium_funnel_report",
        payload=payload,
        dedupe_key=f"consilium:funnel:{payload['report_id']}",
    )
    return {
        "status": "queued" if accepted else "duplicate",
        "report_id": payload["report_id"],
    }


@router.get("/payments/dialogs", dependencies=[Depends(require_admin_token)])
async def list_payment_dialogs():
    """List recent dialogs visible to the OAuth user for setup diagnostics."""
    return await BitrixClient().call("im.recent.list", {"SKIP_OPENLINES": "Y"})


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
    expected_token = OAuthService().load().get(
        "application_token"
    ) or Config.BITRIX_APPLICATION_TOKEN
    received_token = auth.get("application_token")

    if not expected_token:
        raise HTTPException(status_code=503, detail="Bitrix application token is not configured")
    if not tokens_equal(expected_token, received_token):
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
        # The worker needs the Bitrix domain for resolving Disk files, but it
        # must never persist callback OAuth/application tokens in the retry queue.
        MessageDatabase().enqueue(
            job_type="bitrix_operator_message",
            payload=operator_job_payload(form),
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
