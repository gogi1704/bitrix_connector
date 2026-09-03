import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from app.config import Config
from app.routes.analytics import analytics_dashboard, require_analytics_auth
from app.routes.bitrix import (
    install,
    list_payment_dialogs,
    operator_job_payload,
    receive_consilium_payment,
    receive_consilium_funnel_report,
    require_admin_token,
    require_consilium_payment_secret,
    require_consilium_metrics_secret,
    validate_funnel_report,
    validate_payment_notification,
)
from app.routes.max import receive_max_webhook
from app.services.bitrix_client import BitrixApiError, BitrixClient
from app.services.delivery_status import DeliveryStatusService
from app.services.max_client import MaxClient
from app.services.job_worker import JobWorker
from app.services.manager_tools import DialogSummaryService, ReminderService, SummaryAgent
from app.services.message_router import MessageRouter
from app.services.payment_notifications import PaymentNotificationService
from app.services.funnel_reports import FunnelReportService
from app.services.followups import FollowupAgent, FollowupService
from app.services.media import (
    attachment_metadata,
    bitrix_api_file,
    bitrix_file_ids_from_form,
    bitrix_files_from_form,
    max_attachments_to_bitrix_files,
)
from app.services.user_profiles import UserProfileError, UserProfileService
from app.storage.database import MessageDatabase


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400

    def json(self) -> dict:
        return self._payload


class FakeRequest:
    def __init__(
        self,
        *,
        headers: dict | None = None,
        query_params: dict | None = None,
        form: dict | None = None,
        json=None,
    ):
        self.headers = headers or {}
        self.query_params = query_params or {}
        self._form = form or {}
        self._json = json

    async def form(self) -> dict:
        return self._form

    async def json(self):
        return self._json


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def funnel_payload():
        return {
            "schema_version": 1, "report_id": "consilium-funnel-test-123",
            "test": True, "dialog_id": "sg123", "period_days": 1,
            "analysis": "payments", "analysis_label": "Оплаты",
            "current_period": {"date_from": "2026-09-02", "date_to": "2026-09-02"},
            "comparison_period": {"date_from": "2026-09-01", "date_to": "2026-09-01"},
            "current": {"summary": {"visitors": 40, "users": 20}, "payments": {"attempts": 4, "successful_users": 2, "conversion": 50, "revenue_kopecks": 100000}},
            "comparison": {"summary": {"visitors": 38, "users": 22}, "payments": {"attempts": 5, "successful_users": 3, "conversion": 60}},
            "flows": [{"id": "standard", "label": "Обычный путь", "sample_sufficient": True, "summary": {"start_users": 40, "reached_completion": 12}, "alerts": [], "screens": []}],
            "ai_instruction": "Проанализируй изменения.",
        }

    def test_admin_endpoint_requires_matching_token(self):
        with patch.object(Config, "CONNECTOR_ADMIN_TOKEN", "expected"):
            with self.assertRaises(HTTPException) as missing:
                require_admin_token(FakeRequest())
            self.assertEqual(missing.exception.status_code, 403)

            require_admin_token(
                FakeRequest(headers={"X-Connector-Admin-Token": "expected"})
            )

    def test_admin_endpoint_rejects_non_ascii_token_without_server_error(self):
        with patch.object(Config, "CONNECTOR_ADMIN_TOKEN", "expected"):
            with self.assertRaises(HTTPException) as raised:
                require_admin_token(
                    FakeRequest(headers={"X-Connector-Admin-Token": "неверный"})
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_consilium_payment_endpoint_requires_its_own_secret(self):
        with patch.object(Config, "CONSILIUM_PAYMENT_SECRET", "payment-secret"):
            with self.assertRaises(HTTPException) as raised:
                require_consilium_payment_secret(FakeRequest(headers={}))
            self.assertEqual(raised.exception.status_code, 403)
            require_consilium_payment_secret(FakeRequest(headers={
                "X-Consilium-Payment-Secret": "payment-secret",
            }))

    def test_consilium_metrics_endpoint_requires_its_own_secret(self):
        with patch.object(Config, "CONSILIUM_METRICS_SECRET", "metrics-secret"):
            with self.assertRaises(HTTPException) as raised:
                require_consilium_metrics_secret(FakeRequest(headers={}))
            self.assertEqual(raised.exception.status_code, 403)
            require_consilium_metrics_secret(FakeRequest(headers={
                "X-Consilium-Metrics-Secret": "metrics-secret",
            }))

    async def test_consilium_funnel_report_is_private_and_deduplicated(self):
        payload = self.funnel_payload()
        with patch("app.routes.bitrix.MessageDatabase") as database:
            database.return_value.enqueue.return_value = True
            result = await receive_consilium_funnel_report(FakeRequest(json=payload))
        self.assertEqual(result["status"], "queued")
        call = database.return_value.enqueue.call_args.kwargs
        self.assertEqual(call["job_type"], "consilium_funnel_report")
        self.assertEqual(call["dedupe_key"], "consilium:funnel:consilium-funnel-test-123")
        with self.assertRaises(HTTPException) as raised:
            validate_funnel_report({**payload, "chel_id": "forbidden"})
        self.assertEqual(raised.exception.status_code, 422)

    async def test_funnel_report_uses_regular_chat_message(self):
        payload = self.funnel_payload()
        with (
            patch.object(Config, "BITRIX_METRICS_DIALOG_ID", ""),
            patch("app.services.funnel_reports.BitrixClient") as client,
        ):
            client.return_value.call = AsyncMock(return_value={"result": 42})
            await FunnelReportService.send(payload)
        method, params = client.return_value.call.await_args.args
        self.assertEqual(method, "im.message.add")
        self.assertEqual(params["DIALOG_ID"], "sg123")
        self.assertIn("Задание для Bitrix AI", params["MESSAGE"])
        self.assertIn("Вид анализа:[/B] Оплаты", params["MESSAGE"])
        self.assertIn("обезличенные агрегаты", params["MESSAGE"])

    async def test_consilium_payment_is_validated_and_deduplicated(self):
        payload = {
            "order_id": "ord_123",
            "provider_payment_id": "2abc-def",
            "status": "succeeded",
            "amount_kopecks": 1500000,
            "currency": "RUB",
            "client_name": "Иван Иванов",
            "company_inn": "7701234567",
            "organization_name": "ООО Пример",
            "brigade": "Бригада 7",
            "examination_date": "2026-09-15",
            "paid_at": "2026-09-01T12:00:00Z",
            "test": False,
            "items": [{"name": "Чекап", "amount_kopecks": 1500000}],
        }
        with patch("app.routes.bitrix.MessageDatabase") as database:
            database.return_value.enqueue.return_value = True
            result = await receive_consilium_payment(FakeRequest(json=payload))
        self.assertEqual(result["status"], "queued")
        call = database.return_value.enqueue.call_args.kwargs
        self.assertEqual(call["dedupe_key"], "consilium:payment:ord_123")
        self.assertEqual(call["job_type"], "consilium_payment_notification")

        invalid = dict(payload, status="pending")
        with self.assertRaises(HTTPException) as raised:
            validate_payment_notification(invalid)
        self.assertEqual(raised.exception.status_code, 422)

    async def test_payment_notification_uses_regular_chat_message(self):
        payload = {
            "order_id": "ord_123", "provider_payment_id": "payment-123",
            "status": "succeeded", "amount_kopecks": 1500000, "currency": "RUB",
            "client_name": "Иван Иванов", "company_inn": "7701234567",
            "organization_name": "ООО Пример", "paid_at": "2026-09-01T12:00:00Z",
            "brigade": "Бригада 7", "examination_date": "2026-09-15",
            "test": False, "items": [{"name": "Чекап", "amount_kopecks": 1500000}],
        }
        with (
            patch.object(Config, "BITRIX_PAYMENT_DIALOG_ID", "chat123"),
            patch("app.services.payment_notifications.BitrixClient") as client,
        ):
            client.return_value.call = AsyncMock(return_value={"result": 42})
            await PaymentNotificationService.send(payload)
        method, params = client.return_value.call.await_args.args
        self.assertEqual(method, "im.message.add")
        self.assertEqual(params["DIALOG_ID"], "chat123")
        self.assertIn("Иван Иванов", params["MESSAGE"])
        self.assertIn("Чекап", params["MESSAGE"])
        self.assertIn("Бригада 7", params["MESSAGE"])
        self.assertIn("2026-09-15", params["MESSAGE"])

    async def test_payment_dialog_setup_uses_recent_dialogs(self):
        with patch("app.routes.bitrix.BitrixClient") as client:
            client.return_value.call = AsyncMock(return_value={"result": {"items": []}})
            result = await list_payment_dialogs()
        client.return_value.call.assert_awaited_once_with(
            "im.recent.list", {"SKIP_OPENLINES": "Y"},
        )
        self.assertEqual(result, {"result": {"items": []}})

    async def test_install_rejects_untrusted_install_token(self):
        request = FakeRequest(
            query_params={"install_token": "wrong"},
            form={
                "auth[access_token]": "access",
                "auth[refresh_token]": "refresh",
                "auth[application_token]": "bitrix-generated",
            }
        )
        with (
            patch.object(Config, "BITRIX_INSTALL_TOKEN", "expected"),
            patch("app.routes.bitrix.OAuthService.save") as save,
        ):
            with self.assertRaises(HTTPException) as raised:
                await install(request)

        self.assertEqual(raised.exception.status_code, 403)
        save.assert_not_called()

    async def test_install_saves_application_token_received_from_bitrix(self):
        request = FakeRequest(
            query_params={"install_token": "setup-secret"},
            form={
                "auth[access_token]": "access",
                "auth[refresh_token]": "refresh",
                "auth[application_token]": "bitrix-generated",
                "auth[member_id]": "portal-1",
            },
        )
        with (
            patch.object(Config, "BITRIX_INSTALL_TOKEN", "setup-secret"),
            patch("app.routes.bitrix.OAuthService.load", return_value={}),
            patch("app.routes.bitrix.OAuthService.save") as save,
        ):
            result = await install(request)

        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(save.call_args.args[0]["application_token"], "bitrix-generated")

    async def test_max_webhook_fails_closed_without_secret(self):
        with patch.object(Config, "MAX_WEBHOOK_SECRET", ""):
            with self.assertRaises(HTTPException) as raised:
                await receive_max_webhook(FakeRequest(json={}))

        self.assertEqual(raised.exception.status_code, 503)

    def test_analytics_uses_admin_token_as_basic_password(self):
        with patch.object(Config, "CONNECTOR_ADMIN_TOKEN", "expected"):
            require_analytics_auth(
                HTTPBasicCredentials(username="admin", password="expected")
            )
            with self.assertRaises(HTTPException) as raised:
                require_analytics_auth(
                    HTTPBasicCredentials(username="admin", password="wrong")
                )
        self.assertEqual(raised.exception.status_code, 401)

    def test_bitrix_retry_payload_does_not_contain_callback_tokens(self):
        payload = operator_job_payload(
            {
                "data[MESSAGES][0][message][text]": "Ответ",
                "auth[domain]": "portal.example",
                "auth[access_token]": "access-secret",
                "auth[refresh_token]": "refresh-secret",
                "auth[application_token]": "application-secret",
            }
        )
        self.assertEqual(payload["auth[domain]"], "portal.example")
        self.assertNotIn("auth[access_token]", payload)
        self.assertNotIn("auth[refresh_token]", payload)
        self.assertNotIn("auth[application_token]", payload)


class BitrixClientTests(unittest.IsolatedAsyncioTestCase):
    def test_client_endpoint_rejects_local_network_target(self):
        client = BitrixClient.__new__(BitrixClient)
        client.domain = "example.bitrix24.ru"
        client.oauth = SimpleNamespace(client_endpoint="http://127.0.0.1/internal")

        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            _ = client.base_url

    async def test_json_error_is_raised_even_for_successful_http_status(self):
        client = BitrixClient.__new__(BitrixClient)
        client.domain = "example.bitrix24.ru"
        client.token = "token"
        client.oauth = SimpleNamespace(refresh=AsyncMock())
        client._post = AsyncMock(
            return_value=FakeResponse(
                {"error": "BAD_REQUEST", "error_description": "Invalid request"}
            )
        )

        with self.assertRaises(BitrixApiError) as raised:
            await client.call("example.method")

        self.assertEqual(raised.exception.payload["error"], "BAD_REQUEST")
        client.oauth.refresh.assert_not_awaited()

    async def test_delivery_status_uses_bitrix_and_external_message_ids(self):
        with patch("app.services.delivery_status.BitrixClient") as bitrix_client:
            bitrix_client.return_value.call = AsyncMock(
                return_value={"result": {"SUCCESS": True, "DATA": []}}
            )
            await DeliveryStatusService.send(
                {
                    "im_chat_id": "323",
                    "im_message_id": "85911",
                    "external_chat_id": "42",
                    "external_message_ids": ["mid.abc"],
                    "delivered_at": 1_700_000_000,
                }
            )
        method, payload = bitrix_client.return_value.call.await_args.args
        self.assertEqual(method, "imconnector.send.status.delivery")
        status_item = payload["MESSAGES"][0]
        self.assertEqual(status_item["im"], {"chat_id": 323, "message_id": 85911})
        self.assertEqual(status_item["message"]["id"], ["mid.abc"])
        self.assertEqual(status_item["chat"]["id"], "42")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            MessageDatabase,
            "PATH",
            Path(self.temporary_directory.name) / "connector.db",
        )
        self.path_patch.start()
        self.database = MessageDatabase()

    def tearDown(self):
        self.path_patch.stop()
        self.temporary_directory.cleanup()

    def test_partial_upsert_keeps_existing_user_metadata(self):
        dialog_id = self.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        self.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            line_id=17,
        )

        dialog = self.database.get_dialog(channel="max", external_chat_id="42")
        self.assertEqual(dialog["id"], dialog_id)
        self.assertEqual(dialog["external_user_id"], "7")
        self.assertEqual(dialog["external_user_name"], "Test User")

    def test_existing_database_is_migrated_without_repeating_legacy_welcome(self):
        database_path = MessageDatabase.PATH
        database_path.unlink()
        connection = sqlite3.connect(database_path)
        try:
            with connection:
                connection.executescript(
                    """
                CREATE TABLE dialogs (
                    id INTEGER PRIMARY KEY,
                    channel TEXT NOT NULL,
                    external_chat_id TEXT NOT NULL,
                    external_user_id TEXT,
                    external_user_name TEXT,
                    bitrix_chat_id TEXT,
                    bitrix_session_id TEXT,
                    line_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(channel, external_chat_id)
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    dialog_id INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    external_message_id TEXT,
                    bitrix_message_id TEXT,
                    text TEXT,
                    media_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO dialogs (id, channel, external_chat_id, line_id)
                VALUES (1, 'max', '42', 17);
                INSERT INTO messages (dialog_id, direction, external_message_id)
                VALUES (1, 'max_to_bitrix', 'max-start-42');
                    """
                )
        finally:
            connection.close()

        migrated_database = MessageDatabase()
        dialog = migrated_database.get_dialog(channel="max", external_chat_id="42")
        self.assertEqual(dialog["welcome_sent"], 1)

    def test_completed_job_payload_is_removed(self):
        self.database.enqueue(
            job_type="max_update",
            payload={"attachment": {"url": "https://private", "token": "secret"}},
            dedupe_key="media-job",
        )
        job = self.database.claim_job()
        self.database.complete_job(job["id"])
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT state, payload_json FROM jobs WHERE id = ?", (job["id"],)
            ).fetchone()
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["payload_json"], "{}")


    def test_old_profile_table_is_extended_without_losing_cached_data(self):
        database_path = MessageDatabase.PATH
        database_path.unlink()
        connection = sqlite3.connect(database_path)
        try:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE user_profiles (
                        external_user_id TEXT PRIMARY KEY,
                        age INTEGER,
                        weight TEXT,
                        height TEXT,
                        sex TEXT,
                        source_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO user_profiles (
                        external_user_id, age, weight, height, sex
                    ) VALUES ('7', 35, '72', '180', 'Мужчина');
                    """
                )
        finally:
            connection.close()

        migrated = MessageDatabase()
        profile = migrated.get_user_profile("7")
        self.assertEqual(profile["age"], 35)
        self.assertEqual(profile["sex"], "Мужчина")
        self.assertIn("phone", profile)
        self.assertIsNone(profile["client_synced_at"])

    def test_chat_is_archived_as_one_block_and_working_messages_are_deleted(self):
        dialog_id = self.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        self.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text="Здравствуйте",
            external_message_id="m1",
        )
        self.database.save_message(
            dialog_id=dialog_id,
            direction="bitrix_to_max",
            text="Добрый день",
            bitrix_message_id="b1",
        )

        archive = self.database.archive_dialog_messages(
            dialog_id=dialog_id,
            completed_by="99",
            source_message_id="42:complete-1",
        )
        repeated = self.database.archive_dialog_messages(
            dialog_id=dialog_id,
            completed_by="99",
            source_message_id="42:complete-1",
        )

        self.assertEqual(archive["id"], repeated["id"])
        self.assertEqual(archive["message_count"], 2)
        self.assertIn("Клиент: Здравствуйте", archive["transcript_text"])
        self.assertIn("Менеджер: Добрый день", archive["transcript_text"])
        stored = self.database.get_chat_archive(archive["id"])
        self.assertEqual(stored["completed_by"], "99")
        with self.database._connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE dialog_id = ?", (dialog_id,)
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_tags_and_archive_history_are_scoped_to_user(self):
        self.assertTrue(
            self.database.add_user_tag(external_user_id="7", tag="срочный")
        )
        self.assertFalse(
            self.database.add_user_tag(external_user_id="7", tag="срочный")
        )
        self.assertEqual(self.database.get_user_tags("7"), ["срочный"])

        dialog_id = self.database.upsert_dialog(
            channel="max", external_chat_id="42", external_user_id="7", line_id=17
        )
        self.database.save_message(
            dialog_id=dialog_id, direction="max_to_bitrix", text="Первый вопрос"
        )
        archive = self.database.archive_dialog_messages(
            dialog_id=dialog_id, source_message_id="complete-tags"
        )
        history = self.database.list_user_chat_archives(
            external_user_id="7", external_chat_id="42"
        )
        self.assertEqual(history[0]["id"], archive["id"])
        self.assertIsNone(
            self.database.get_user_chat_archive(
                position=1, external_user_id="another", external_chat_id="another"
            )
        )

    def test_failed_job_can_be_requeued_while_payload_is_retained(self):
        self.database.enqueue(
            job_type="max_update",
            payload={"message": {"id": "one"}},
            dedupe_key="failed-one",
        )
        job = self.database.claim_job()
        self.database.fail_job(
            job_id=job["id"], attempts=8, error="temporary", max_attempts=8
        )
        with self.database._connect() as connection:
            failed = connection.execute(
                "SELECT state, payload_json FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("message", failed["payload_json"])
        self.assertTrue(self.database.retry_failed_job(job["id"]))
        retried = self.database.claim_job()
        self.assertEqual(retried["id"], job["id"])
        self.assertEqual(retried["attempts"], 1)

    def test_analytics_snapshot_contains_only_aggregates(self):
        snapshot = self.database.analytics_snapshot()
        self.assertIn("dialogs", snapshot)
        self.assertIn("queue", snapshot)
        self.assertIn("reminders", snapshot)
        self.assertNotIn("messages_json", str(snapshot))
        response = analytics_dashboard()
        self.assertIn("Аналитика коннектора", response.body.decode("utf-8"))
        self.assertEqual(response.headers["x-frame-options"], "DENY")


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            MessageDatabase,
            "PATH",
            Path(self.temporary_directory.name) / "connector.db",
        )
        self.path_patch.start()
        self.database = MessageDatabase()
        self.dialog_id = self.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )

    def tearDown(self):
        self.path_patch.stop()
        self.temporary_directory.cleanup()

    def _manager_plan(self) -> tuple[int, int]:
        message_id = self.database.save_message(
            dialog_id=self.dialog_id,
            direction="bitrix_to_max",
            text="Выберите удобное время",
        )
        followup_id = self.database.create_followup_plan(
            dialog_id=self.dialog_id,
            based_on_message_id=message_id,
        )
        return message_id, followup_id

    async def test_plan_is_scheduled_for_agent_decision(self):
        _, followup_id = self._manager_plan()
        service = FollowupService(self.database)
        service.agent.decide = AsyncMock(
            return_value={
                "action": "send_now",
                "message": "Удалось выбрать удобное время?",
                "reason": "Ожидается выбор времени",
                "confidence": 0.9,
            }
        )
        with patch.object(service, "_due_at", return_value=12345.0):
            await service.plan(followup_id)

        followup = self.database.get_followup(followup_id)
        self.assertEqual(followup["state"], "scheduled")
        self.assertEqual(followup["due_at"], 12345.0)
        with self.database._connect() as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='followup_send'"
            ).fetchone()
        self.assertEqual(job["available_at"], 12345.0)

    async def test_new_client_message_prevents_scheduled_send(self):
        _, followup_id = self._manager_plan()
        self.database.schedule_followup(
            followup_id=followup_id,
            action="follow_up_30m",
            due_at=time.time() - 1,
            draft_text="Получилось?",
            reason="Ожидается действие",
            confidence=0.9,
        )
        self.database.save_message(
            dialog_id=self.dialog_id,
            direction="max_to_bitrix",
            text="Да, всё получилось",
        )
        service = FollowupService(self.database)
        with patch("app.services.followups.MaxClient") as max_client:
            await service.send(followup_id)

        max_client.return_value.send_message.assert_not_called()
        self.assertEqual(self.database.get_followup(followup_id)["state"], "cancelled")

    async def test_duplicate_client_webhook_does_not_cancel_newer_plan(self):
        update = {
            "update_type": "message_created",
            "chat_id": 42,
            "timestamp": 1_700_000_000,
            "message": {
                "id": "client-1",
                "body": {"text": "Первое сообщение"},
            },
        }
        request = FakeRequest(
            headers={"X-Max-Bot-Api-Secret": "secret"}, json=update
        )
        with patch.object(Config, "MAX_WEBHOOK_SECRET", "secret"):
            first = await receive_max_webhook(request)

            _, followup_id = self._manager_plan()
            self.database.schedule_followup(
                followup_id=followup_id,
                action="follow_up_30m",
                due_at=time.time() + 1800,
                draft_text="Получилось?",
                reason="Ожидается действие",
                confidence=0.9,
            )
            duplicate = await receive_max_webhook(request)

        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.database.get_followup(followup_id)["state"], "scheduled")

    async def test_current_followup_is_sent_once_and_saved_in_history(self):
        _, followup_id = self._manager_plan()
        self.database.schedule_followup(
            followup_id=followup_id,
            action="follow_up_30m",
            due_at=time.time() - 1,
            draft_text="Удалось выбрать удобное время?",
            reason="Ожидается выбор",
            confidence=0.9,
        )
        service = FollowupService(self.database)
        with (
            patch("app.services.followups.MaxClient") as max_client,
            patch("app.services.followups.BitrixClient") as bitrix_client,
        ):
            max_client.return_value.send_message = AsyncMock(return_value={"ok": True})
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await service.send(followup_id)
            await service.send(followup_id)

        max_client.return_value.send_message.assert_awaited_once_with(
            42, "Удалось выбрать удобное время?"
        )
        self.assertEqual(self.database.get_followup(followup_id)["state"], "sent")
        messages = self.database.get_dialog_messages(self.dialog_id)
        self.assertEqual(messages[-1]["direction"], "automated_to_max")
        mirror = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn(
            "Бот отправил клиенту", mirror["MESSAGES"][0]["message"]["text"]
        )

    async def test_medical_context_requires_manager_review_without_ai(self):
        decision = FollowupAgent._fallback(
            [
                {
                    "direction": "bitrix_to_max",
                    "text": "Расшифровка результатов обследования будет позже",
                }
            ]
        )
        self.assertEqual(decision["action"], "manager_review")

    async def test_manager_review_is_reported_only_to_bitrix(self):
        _, followup_id = self._manager_plan()
        service = FollowupService(self.database)
        service.agent.decide = AsyncMock(
            return_value={
                "action": "manager_review",
                "message": "",
                "reason": "Медицинский вопрос",
                "confidence": 1.0,
            }
        )
        with (
            patch("app.services.followups.BitrixClient") as bitrix_client,
            patch("app.services.followups.MaxClient") as max_client,
        ):
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await service.plan(followup_id)

        max_client.return_value.send_message.assert_not_called()
        bitrix_client.return_value.call.assert_awaited_once()
        payload = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn("требуется решение менеджера", payload["MESSAGES"][0]["message"]["text"])
        self.assertEqual(self.database.get_followup(followup_id)["state"], "manager_review")

    async def test_quick_followup_is_not_scheduled_during_quiet_hours(self):
        current = datetime(2026, 7, 20, 21, 15, tzinfo=ZoneInfo("Europe/Moscow"))
        with (
            patch("app.services.followups.datetime") as datetime_mock,
            patch.object(Config, "FOLLOWUP_DELAY_MINUTES", 30),
            patch.object(Config, "FOLLOWUP_QUIET_START_HOUR", 21),
            patch.object(Config, "FOLLOWUP_QUIET_END_HOUR", 9),
        ):
            datetime_mock.now.return_value = current
            due_at = FollowupService._due_at("send_now")

        due = datetime.fromtimestamp(due_at, ZoneInfo("Europe/Moscow"))
        self.assertEqual(due, datetime(2026, 7, 21, 9, 0, tzinfo=ZoneInfo("Europe/Moscow")))


class MessageRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            MessageDatabase,
            "PATH",
            Path(self.temporary_directory.name) / "connector.db",
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temporary_directory.cleanup()

    def test_operator_command_arguments_are_preserved(self):
        self.assertEqual(
            MessageRouter.extract_operator_request(
                "[b]Менеджер:[/b] [br] /remind 2h Проверить результаты"
            ),
            ("/remind", "2h Проверить результаты"),
        )

    def test_summary_agent_redacts_phone_and_email(self):
        redacted = SummaryAgent.redact(
            "Телефон +7 (999) 123-45-67, почта patient@example.com"
        )
        self.assertNotIn("999", redacted)
        self.assertNotIn("patient@example.com", redacted)
        self.assertIn("[телефон]", redacted)
        self.assertIn("[email]", redacted)

    async def test_bot_started_retries_only_missing_welcome(self):
        update = {
            "update_type": "bot_started",
            "chat_id": 42,
            "timestamp": 1_700_000_000,
            "user": {"user_id": 7, "name": "Test User"},
        }
        bitrix_call = AsyncMock(return_value={"result": {}})
        max_send = AsyncMock(side_effect=[RuntimeError("temporary failure"), {"ok": True}])

        with (
            patch("app.services.message_router.BitrixClient") as bitrix_client,
            patch("app.services.message_router.MaxClient") as max_client,
            patch("app.services.followups.BitrixClient") as mirror_client,
        ):
            bitrix_client.return_value.call = bitrix_call
            max_client.return_value.send_message = max_send
            mirror_client.return_value.call = AsyncMock(return_value={"result": {}})
            router = MessageRouter()

            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                await router.on_max_bot_started(update)

            dialog = router.database.get_dialog(channel="max", external_chat_id="42")
            self.assertEqual(dialog["welcome_sent"], 0)

            result = await router.on_max_bot_started(update)
            duplicate_result = await router.on_max_bot_started(update)

        self.assertEqual(result["result"], "started")
        self.assertEqual(duplicate_result["result"], "already_started")
        self.assertEqual(bitrix_call.await_count, 1)
        self.assertEqual(max_send.await_count, 2)
        dialog = router.database.get_dialog(channel="max", external_chat_id="42")
        self.assertEqual(dialog["welcome_sent"], 1)

    async def test_anketa_command_is_answered_in_bitrix_and_not_sent_to_max(self):
        router = MessageRouter()
        dialog_id = router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        form = {
            "data[MESSAGES][0][chat][id]": "42",
            "data[MESSAGES][0][message][text]": "[b]Manager:[/b][br]/anketa",
            "data[MESSAGES][0][im][message_id]": "100",
            "data[MESSAGES][0][im][chat_id]": "323",
        }

        with (
            patch("app.services.message_router.MaxClient") as max_client,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
            patch("app.services.message_router.UserProfileService") as profile_service,
        ):
            profile_service.return_value.get_profile = AsyncMock(
                return_value={"user_name": "Test User", "med_id": "55"}
            )
            profile_service.return_value.format_profile.return_value = "Анкета: Test User"
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})

            await router.from_bitrix(form)

        max_client.return_value.send_message.assert_not_called()
        profile_service.return_value.get_profile.assert_awaited_once_with(
            "7", refresh=False
        )
        bitrix_client.return_value.call.assert_awaited_once()
        method, payload = bitrix_client.return_value.call.await_args.args
        self.assertEqual(method, "imconnector.send.messages")
        self.assertEqual(
            payload["MESSAGES"][0]["message"]["text"], "Анкета: Test User"
        )

        with router.database._connect() as connection:
            saved = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE dialog_id = ?", (dialog_id,)
            ).fetchone()[0]
            delivery_job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='bitrix_delivery_status'"
            ).fetchone()
        self.assertEqual(saved, 0)
        self.assertIsNotNone(delivery_job)

    async def test_unknown_slash_command_is_not_sent_to_max(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )
        form = {
            "data[MESSAGES][0][chat][id]": "42",
            "data[MESSAGES][0][message][text]": "/unknown",
        }

        with (
            patch("app.services.message_router.MaxClient") as max_client,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router.from_bitrix(form)

        max_client.return_value.send_message.assert_not_called()
        response = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn("Неизвестная служебная команда", response["MESSAGES"][0]["message"]["text"])

    async def test_regular_operator_message_is_still_sent_to_max(self):
        router = MessageRouter()
        form = {
            "data[MESSAGES][0][chat][id]": "42",
            "data[MESSAGES][0][message][text]": "[b]Manager:[/b][br]Hello",
            "data[MESSAGES][0][im][message_id]": "101",
            "data[MESSAGES][0][im][chat_id]": "323",
        }

        started_at = time.time()
        with (
            patch("app.services.message_router.MaxClient") as max_client,
            patch.object(Config, "FOLLOWUP_DELAY_MINUTES", 30),
        ):
            max_client.return_value.send_message = AsyncMock(
                return_value={"body": {"mid": "mid.sent-101"}}
            )
            await router.from_bitrix(form)

        max_client.return_value.send_message.assert_awaited_once_with(
            42, "👤 Manager\n\nHello"
        )
        with router.database._connect() as connection:
            analysis_job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='followup_plan'"
            ).fetchone()
            delivery_job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='bitrix_delivery_status'"
            ).fetchone()
        self.assertGreaterEqual(analysis_job["available_at"], started_at + 1799)
        self.assertIsNotNone(delivery_job)
        self.assertIn("mid.sent-101", delivery_job["payload_json"])

    async def test_max_image_is_forwarded_to_bitrix_as_file(self):
        router = MessageRouter()
        update = {
            "chat_id": 42,
            "timestamp": 1_700_000_000,
            "message": {
                "id": "m1",
                "sender": {"user_id": 7, "name": "Test User"},
                "body": {
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "url": "https://cdn.example.org/photo.jpg",
                                "token": "must-not-be-stored",
                            },
                        }
                    ]
                },
            },
        }
        with patch("app.services.message_router.BitrixClient") as bitrix_client:
            bitrix_client.return_value.call = AsyncMock(
                return_value={"result": {"DATA": {"RESULT": [{}]}}}
            )
            await router.from_max(update)

        payload = bitrix_client.return_value.call.await_args.args[1]
        self.assertEqual(
            payload["MESSAGES"][0]["message"]["files"][0]["url"],
            "https://cdn.example.org/photo.jpg",
        )
        connection = sqlite3.connect(MessageDatabase.PATH)
        try:
            stored_media = connection.execute("SELECT media_json FROM messages").fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("cdn.example.org", stored_media)
        self.assertNotIn("must-not-be-stored", stored_media)

    async def test_client_message_queues_ai_analysis_only_after_silence(self):
        router = MessageRouter()
        update = {
            "chat_id": 42,
            "timestamp": 1_700_000_000,
            "message": {
                "id": "client-text-1",
                "sender": {"user_id": 7, "name": "Test User"},
                "body": {"text": "Подскажите по записи"},
            },
        }
        started_at = time.time()
        with (
            patch("app.services.message_router.BitrixClient") as bitrix_client,
            patch.object(Config, "FOLLOWUP_DELAY_MINUTES", 30),
        ):
            bitrix_client.return_value.call = AsyncMock(
                return_value={"result": {"DATA": {"RESULT": [{}]}}}
            )
            await router.from_max(update)

        with router.database._connect() as connection:
            followup = connection.execute("SELECT * FROM followup_jobs").fetchone()
            analysis_job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='followup_plan'"
            ).fetchone()
        self.assertEqual(followup["state"], "planning")
        self.assertGreaterEqual(analysis_job["available_at"], started_at + 1799)

    async def test_bitrix_file_without_text_is_sent_to_max(self):
        router = MessageRouter()
        form = {
            "data[MESSAGES][0][chat][id]": "42",
            "data[MESSAGES][0][im][message_id]": "102",
            "data[MESSAGES][0][message][files][0][url]": "https://portal.example/file.pdf",
            "data[MESSAGES][0][message][files][0][name]": "result.pdf",
        }
        with patch("app.services.message_router.MaxClient") as max_client:
            max_client.return_value.send_remote_file = AsyncMock(return_value={"ok": True})
            await router.from_bitrix(form)

        max_client.return_value.send_remote_file.assert_awaited_once_with(
            42,
            url="https://portal.example/file.pdf",
            name="result.pdf",
            text=None,
        )
        max_client.return_value.send_message.assert_not_called()

    async def test_help_command_is_shown_only_in_bitrix(self):
        router = MessageRouter()
        dialog_id = router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )
        with (
            patch("app.services.message_router.MaxClient") as max_client,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/help", chat_id="42", source_message_id="help-1"
            )
        max_client.return_value.send_message.assert_not_called()
        response = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn("/chat_complete", response["MESSAGES"][0]["message"]["text"])
        self.assertIn("/analytics", response["MESSAGES"][0]["message"]["text"])
        self.assertIn("/summary_all", response["MESSAGES"][0]["message"]["text"])
        with router.database._connect() as connection:
            saved = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE dialog_id = ?", (dialog_id,)
            ).fetchone()[0]
        self.assertEqual(saved, 0)

    async def test_analytics_command_shows_url_without_secret(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max", external_chat_id="42", external_user_id="7", line_id=17
        )
        with (
            patch.object(Config, "PUBLIC_BASE_URL", "https://connector.example"),
            patch.object(Config, "CONNECTOR_ADMIN_TOKEN", "top-secret"),
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/analytics", chat_id="42", source_message_id="analytics-1"
            )
        payload = bitrix_client.return_value.call.await_args.args[1]
        text = payload["MESSAGES"][0]["message"]["text"]
        self.assertIn("https://connector.example/analytics/", text)
        self.assertIn("Логин: admin", text)
        self.assertNotIn("top-secret", text)

    async def test_tag_and_summary_are_private_manager_commands(self):
        router = MessageRouter()
        dialog_id = router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        router.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text="Когда будут готовы результаты?",
        )
        router.database.save_message(
            dialog_id=dialog_id,
            direction="bitrix_to_max",
            text="Проверим и сообщим.",
        )
        with (
            patch("app.services.message_router.MaxClient") as max_client,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/tag",
                arguments="ожидает_результаты",
                chat_id="42",
                source_message_id="tag-1",
                completed_by="99",
            )
            await router._handle_operator_command(
                command="/summary", chat_id="42", source_message_id="summary-1"
            )
        self.assertEqual(
            router.database.get_user_tags("7"), ["ожидает_результаты"]
        )
        summary_payload = bitrix_client.return_value.call.await_args.args[1]
        summary_text = summary_payload["MESSAGES"][0]["message"]["text"]
        self.assertIn("Готовлю ИИ-резюме", summary_text)
        with router.database._connect() as connection:
            summary_job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='manager_summary'"
            ).fetchone()
        self.assertIsNotNone(summary_job)
        local_summary = await DialogSummaryService(router.database).build_ai(
            dialog_id=dialog_id, external_user_id="7"
        )
        self.assertIn("Когда будут готовы", local_summary)
        self.assertIn("ожидает_результаты", local_summary)
        max_client.return_value.send_message.assert_not_called()

    async def test_summary_all_scans_only_current_users_archives(self):
        router = MessageRouter()
        dialog_id = router.database.upsert_dialog(
            channel="max", external_chat_id="42", external_user_id="7", line_id=17
        )
        router.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text="История клиента семь",
        )
        router.database.archive_dialog_messages(
            dialog_id=dialog_id, source_message_id="complete-user-7"
        )
        router.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text="Текущий вопрос клиента семь",
        )

        other_dialog_id = router.database.upsert_dialog(
            channel="max", external_chat_id="84", external_user_id="8", line_id=17
        )
        router.database.save_message(
            dialog_id=other_dialog_id,
            direction="max_to_bitrix",
            text="Секрет другого клиента",
        )
        router.database.archive_dialog_messages(
            dialog_id=other_dialog_id, source_message_id="complete-user-8"
        )

        with (
            patch.object(SummaryAgent, "available", return_value=True),
            patch.object(
                SummaryAgent,
                "summarize",
                new=AsyncMock(return_value="Единое безопасное резюме"),
            ) as summarize,
        ):
            result = await DialogSummaryService(router.database).build_all_ai(
                dialog_id=dialog_id,
                external_user_id="7",
                external_chat_id="42",
            )
        segments = summarize.await_args.args[0]
        source = "\n".join(segments)
        self.assertIn("История клиента семь", source)
        self.assertIn("Текущий вопрос клиента семь", source)
        self.assertNotIn("Секрет другого клиента", source)
        self.assertIn("Единое безопасное резюме", result)

    async def test_summary_job_sends_result_back_to_same_bitrix_chat(self):
        database = MessageDatabase()
        dialog_id = database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        database.enqueue(
            job_type="manager_summary",
            payload={
                "mode": "current",
                "dialog_id": dialog_id,
                "external_user_id": "7",
                "external_chat_id": "42",
                "external_user_name": "Test User",
            },
            dedupe_key="summary-job-test",
        )
        job = database.claim_job()
        worker = JobWorker()
        worker.database = database
        with (
            patch(
                "app.services.job_worker.DialogSummaryService.build_ai",
                new=AsyncMock(return_value="Готовое ИИ-резюме"),
            ),
            patch(
                "app.services.job_worker.MessageRouter._send_internal_message",
                new=AsyncMock(),
            ) as send_internal,
        ):
            await worker._process(job)
        self.assertEqual(send_internal.await_args.kwargs["chat_id"], "42")
        self.assertEqual(send_internal.await_args.kwargs["text"], "Готовое ИИ-резюме")

    async def test_reminder_is_persistent_and_sent_only_to_bitrix(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        with patch("app.services.message_router.BitrixClient") as bitrix_client:
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/remind",
                arguments="2h Проверить результаты",
                chat_id="42",
                source_message_id="remind-1",
                completed_by="99",
            )
        with router.database._connect() as connection:
            reminder = connection.execute(
                "SELECT * FROM manager_reminders"
            ).fetchone()
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_type='manager_reminder'"
            ).fetchone()
            connection.execute(
                "UPDATE manager_reminders SET due_at=? WHERE id=?",
                (time.time() - 1, reminder["id"]),
            )
        self.assertIsNotNone(job)
        with (
            patch("app.services.manager_tools.BitrixClient") as bitrix_client,
            patch("app.services.manager_tools.MaxClient", create=True) as max_client,
        ):
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await ReminderService(router.database).send(int(reminder["id"]))
        sent = router.database.get_manager_reminder(int(reminder["id"]))
        self.assertEqual(sent["state"], "sent")
        payload = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn("Проверить результаты", payload["MESSAGES"][0]["message"]["text"])
        max_client.assert_not_called()

    async def test_history_opens_only_current_users_archive(self):
        router = MessageRouter()
        dialog_id = router.database.upsert_dialog(
            channel="max", external_chat_id="42", external_user_id="7", line_id=17
        )
        router.database.save_message(
            dialog_id=dialog_id, direction="max_to_bitrix", text="Архивный вопрос"
        )
        router.database.archive_dialog_messages(
            dialog_id=dialog_id, source_message_id="complete-history"
        )
        with patch("app.services.message_router.BitrixClient") as bitrix_client:
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/history",
                arguments="1",
                chat_id="42",
                source_message_id="history-1",
            )
        payload = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn("Архивный вопрос", payload["MESSAGES"][0]["message"]["text"])

    async def test_client_command_uses_cached_profile_service(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            external_user_name="Test User",
            line_id=17,
        )
        with (
            patch("app.services.message_router.UserProfileService") as service,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            service.return_value.get_client_profile = AsyncMock(
                return_value={"external_user_id": "7", "phone": "+70000000000"}
            )
            service.return_value.format_client.return_value = "Карточка клиента"
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/client", chat_id="42", source_message_id="client-1"
            )
        service.return_value.get_client_profile.assert_awaited_once_with("7")
        response = bitrix_client.return_value.call.await_args.args[1]
        self.assertEqual(response["MESSAGES"][0]["message"]["text"], "Карточка клиента")

    async def test_results_command_reports_ready_results(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )
        with (
            patch("app.services.message_router.UserProfileService") as service,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            service.return_value.get_results = AsyncMock(
                return_value={"med_id": "55", "results": "Готово"}
            )
            service.return_value.format_results.return_value = "Результаты: Готово"
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/results", chat_id="42", source_message_id="results-1"
            )
        service.return_value.get_results.assert_awaited_once_with("7")
        response = bitrix_client.return_value.call.await_args.args[1]
        self.assertEqual(response["MESSAGES"][0]["message"]["text"], "Результаты: Готово")

    async def test_chat_complete_archives_messages_without_saving_confirmation(self):
        router = MessageRouter()
        dialog_id = router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )
        router.database.save_message(
            dialog_id=dialog_id,
            direction="max_to_bitrix",
            text="Вопрос",
        )
        with patch("app.services.message_router.BitrixClient") as bitrix_client:
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/chat_complete",
                chat_id="42",
                source_message_id="complete-1",
                completed_by="99",
            )
        with router.database._connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE dialog_id = ?", (dialog_id,)
            ).fetchone()[0]
            archive = connection.execute("SELECT * FROM chat_archives").fetchone()
        self.assertEqual(remaining, 0)
        self.assertEqual(archive["message_count"], 1)
        self.assertEqual(archive["completed_by"], "99")

    async def test_anketa_reports_profile_not_found(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )
        with (
            patch("app.services.message_router.UserProfileService") as profile_service,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
        ):
            profile_service.return_value.get_profile = AsyncMock(return_value=None)
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/anketa",
                chat_id="42",
                source_message_id="102",
            )

        response = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn("не найдена", response["MESSAGES"][0]["message"]["text"])

    async def test_anketa_reports_temporary_google_error(self):
        router = MessageRouter()
        router.database.upsert_dialog(
            channel="max",
            external_chat_id="42",
            external_user_id="7",
            line_id=17,
        )
        with (
            patch("app.services.message_router.UserProfileService") as profile_service,
            patch("app.services.message_router.BitrixClient") as bitrix_client,
            patch("app.services.message_router.logger.exception"),
        ):
            profile_service.return_value.get_profile = AsyncMock(
                side_effect=UserProfileError("temporary error")
            )
            bitrix_client.return_value.call = AsyncMock(return_value={"result": {}})
            await router._handle_operator_command(
                command="/anketa",
                chat_id="42",
                source_message_id="103",
            )

        response = bitrix_client.return_value.call.await_args.args[1]
        self.assertIn(
            "Повторите команду позже",
            response["MESSAGES"][0]["message"]["text"],
        )


class UserProfileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_profile_is_merged_and_then_read_from_local_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            with patch.object(
                MessageDatabase, "PATH", directory_path / "connector.db"
            ):
                database = MessageDatabase()
                service = UserProfileService(database)
                first_profile = {
                    "external_user_id": "7",
                    "age": "35",
                    "weight": "72",
                    "height": "180",
                    "sex": "Мужчина",
                }
                with patch.object(
                    service,
                    "_load_from_google",
                    new=AsyncMock(return_value=first_profile),
                ) as google_loader:
                    profile = await service.get_profile(7)
                google_loader.assert_awaited_once_with("7")

                with patch.object(
                    service,
                    "_load_from_google",
                    side_effect=AssertionError("source must not be read twice"),
                ):
                    cached_profile = await service.get_profile(7)
                with patch.object(
                    service,
                    "_load_from_google",
                    new=AsyncMock(
                        return_value={
                        "external_user_id": "7",
                        "age": "36",
                        "weight": "73",
                        "height": "180",
                        "sex": "Мужчина",
                        }
                    ),
                ) as google_refresh:
                    refreshed_profile = await service.get_profile(7, refresh=True)

        self.assertEqual(profile["age"], "35")
        self.assertEqual(profile["weight"], "72")
        self.assertEqual(profile["height"], "180")
        self.assertEqual(profile["sex"], "Мужчина")
        self.assertEqual(cached_profile["external_user_id"], "7")
        self.assertEqual(refreshed_profile["age"], "36")
        google_refresh.assert_awaited_once_with("7")
        formatted = service.format_profile(profile)
        self.assertIn("Пол: Мужчина", formatted)
        self.assertIn("Возраст: 35", formatted)


    async def test_results_are_loaded_once_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                MessageDatabase,
                "PATH",
                Path(directory) / "connector.db",
            ):
                database = MessageDatabase()
                service = UserProfileService(database)
                loaded = {
                    "external_user_id": "7",
                    "med_id": "55",
                    "results": "Общий анализ готов",
                }
                with patch.object(
                    service,
                    "_load_results",
                    return_value=loaded,
                ) as google_loader:
                    first = await service.get_results("7")
                    second = await service.get_results("7")

        self.assertEqual(first["results"], "Общий анализ готов")
        self.assertEqual(second["med_id"], "55")
        google_loader.assert_called_once_with("7")


class MediaParsingTests(unittest.TestCase):
    def test_media_helpers_do_not_persist_urls_or_tokens(self):
        attachments = [
            {
                "type": "file",
                "payload": {
                    "url": "https://cdn.example/result.pdf",
                    "token": "secret-token",
                    "name": "result.pdf",
                    "size": 123,
                },
            }
        ]
        self.assertEqual(
            max_attachments_to_bitrix_files(attachments),
            [{"url": "https://cdn.example/result.pdf", "name": "result.pdf"}],
        )
        metadata = attachment_metadata(attachments)
        self.assertEqual(metadata[0]["name"], "result.pdf")
        self.assertNotIn("url", metadata[0])
        self.assertNotIn("token", metadata[0])

    def test_flattened_bitrix_files_are_sorted(self):
        form = {
            "data[MESSAGES][0][message][files][1][url]": "https://portal/two.pdf",
            "data[MESSAGES][0][message][files][1][name]": "two.pdf",
            "data[MESSAGES][0][message][files][0][url]": "https://portal/one.jpg",
            "data[MESSAGES][0][message][files][0][name]": "one.jpg",
        }
        self.assertEqual(
            [item["name"] for item in bitrix_files_from_form(form)],
            ["one.jpg", "two.pdf"],
        )

    def test_real_bitrix_download_link_format_is_supported(self):
        form = {
            "data[MESSAGES][0][message][files][0][downloadLink]": "/rest/download.json?token=signed",
            "data[MESSAGES][0][message][files][0][link]": "https://portal.example/preview",
            "data[MESSAGES][0][message][files][0][mime]": "image/jpeg",
            "data[MESSAGES][0][message][files][0][name]": "photo.jpg",
            "data[MESSAGES][0][message][files][0][size]": "1234",
            "data[MESSAGES][0][message][files][0][type]": "image",
        }
        with patch.object(Config, "BITRIX_DOMAIN", "portal.example"):
            files = bitrix_files_from_form(form)
        self.assertEqual(
            files,
            [
                {
                    "url": "https://portal.example/rest/download.json?token=signed",
                    "mime": "image/jpeg",
                    "name": "photo.jpg",
                    "size": "1234",
                    "type": "image",
                }
            ],
        )

    def test_bitrix_disk_file_id_and_response_are_normalized(self):
        form = {
            "data[MESSAGES][0][message][params][FILE_ID][0]": "5255",
        }
        self.assertEqual(bitrix_file_ids_from_form(form), ["5255"])
        self.assertEqual(
            bitrix_api_file(
                {
                    "DOWNLOAD_URL": "https://portal/rest/download.json?token=signed",
                    "NAME": "analysis.pdf",
                    "SIZE": "1234",
                }
            ),
            {
                "url": "https://portal/rest/download.json?token=signed",
                "name": "analysis.pdf",
                "size": "1234",
                "type": "file",
            },
        )

    def test_declared_max_file_over_50_mb_is_not_forwarded(self):
        attachments = [
            {
                "type": "file",
                "payload": {
                    "url": "https://cdn.example/large.zip",
                    "size": Config.MEDIA_MAX_BYTES + 1,
                },
            }
        ]
        self.assertEqual(max_attachments_to_bitrix_files(attachments), [])


class MaxMediaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_temporary_file_is_deleted_after_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.pdf"
            path.write_bytes(b"test")
            client = MaxClient()
            with (
                patch(
                    "app.services.max_client.download_to_temporary_file",
                    new=AsyncMock(
                        return_value=(path, "result.pdf", "application/pdf")
                    ),
                ),
                patch.object(
                    client,
                    "_upload_file",
                    new=AsyncMock(
                        return_value={"type": "file", "payload": {"token": "t"}}
                    ),
                ),
                patch.object(
                    client,
                    "send_message",
                    new=AsyncMock(return_value={"ok": True}),
                ),
            ):
                await client.send_remote_file(
                    42,
                    url="https://portal.example/result.pdf",
                    name="result.pdf",
                )

            self.assertFalse(path.exists())

    async def test_video_uses_token_from_prepare_response_with_empty_upload_body(self):
        client = MaxClient()
        prepare = FakeResponse({
            "url": "https://vu.okcdn.ru/upload.do?signed=1",
            "token": "video-token",
        })
        prepare.content = b'{"url":"x"}'
        prepare.raise_for_status = lambda: None
        uploaded = FakeResponse({})
        uploaded.content = b""
        uploaded.raise_for_status = lambda: None
        http_client = AsyncMock()
        http_client.post = AsyncMock(side_effect=[prepare, uploaded])
        context = AsyncMock()
        context.__aenter__.return_value = http_client
        context.__aexit__.return_value = False

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("app.services.max_client.httpx.AsyncClient", return_value=context),
        ):
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            attachment = await client._upload_file(path, "video.mp4", "video/mp4")

        self.assertEqual(
            attachment,
            {"type": "video", "payload": {"token": "video-token"}},
        )

    async def test_image_accepts_photo_tokens_payload(self):
        client = MaxClient()
        prepare = FakeResponse({"url": "https://iu.oneme.ru/uploadImage?signed=1"})
        prepare.content = b'{"url":"x"}'
        prepare.raise_for_status = lambda: None
        uploaded = FakeResponse({"photos": {"photo-id": {"token": "image-token"}}})
        uploaded.content = b'{"photos":{"photo-id":{"token":"image-token"}}}'
        uploaded.raise_for_status = lambda: None
        http_client = AsyncMock()
        http_client.post = AsyncMock(side_effect=[prepare, uploaded])
        context = AsyncMock()
        context.__aenter__.return_value = http_client
        context.__aexit__.return_value = False

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("app.services.max_client.httpx.AsyncClient", return_value=context),
        ):
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            attachment = await client._upload_file(path, "photo.jpg", "image/jpeg")

        self.assertEqual(
            attachment,
            {
                "type": "image",
                "payload": {"photos": {"photo-id": {"token": "image-token"}}},
            },
        )


if __name__ == "__main__":
    unittest.main()
