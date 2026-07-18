import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.config import Config
from app.routes.bitrix import install, require_admin_token
from app.routes.max import receive_max_webhook
from app.services.bitrix_client import BitrixApiError, BitrixClient
from app.services.message_router import MessageRouter
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
        form: dict | None = None,
        json=None,
    ):
        self.headers = headers or {}
        self._form = form or {}
        self._json = json

    async def form(self) -> dict:
        return self._form

    async def json(self):
        return self._json


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_admin_endpoint_requires_matching_token(self):
        with patch.object(Config, "CONNECTOR_ADMIN_TOKEN", "expected"):
            with self.assertRaises(HTTPException) as missing:
                require_admin_token(FakeRequest())
            self.assertEqual(missing.exception.status_code, 403)

            require_admin_token(
                FakeRequest(headers={"X-Connector-Admin-Token": "expected"})
            )

    async def test_install_rejects_untrusted_application_token(self):
        request = FakeRequest(
            form={
                "auth[access_token]": "access",
                "auth[refresh_token]": "refresh",
                "auth[application_token]": "untrusted",
            }
        )
        with (
            patch.object(Config, "BITRIX_APPLICATION_TOKEN", "expected"),
            patch("app.routes.bitrix.OAuthService.save") as save,
        ):
            with self.assertRaises(HTTPException) as raised:
                await install(request)

        self.assertEqual(raised.exception.status_code, 403)
        save.assert_not_called()

    async def test_max_webhook_fails_closed_without_secret(self):
        with patch.object(Config, "MAX_WEBHOOK_SECRET", ""):
            with self.assertRaises(HTTPException) as raised:
                await receive_max_webhook(FakeRequest(json={}))

        self.assertEqual(raised.exception.status_code, 503)


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
        ):
            bitrix_client.return_value.call = bitrix_call
            max_client.return_value.send_message = max_send
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
        router.database.upsert_dialog(
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
        }

        with patch("app.services.message_router.MaxClient") as max_client:
            max_client.return_value.send_message = AsyncMock(return_value={"ok": True})
            await router.from_bitrix(form)

        max_client.return_value.send_message.assert_awaited_once_with(
            42, "👤 Manager\n\nHello"
        )

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


if __name__ == "__main__":
    unittest.main()
