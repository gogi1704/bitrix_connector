import asyncio
import re
from pathlib import Path

from app.config import Config
from app.storage.database import MessageDatabase


class UserProfileError(Exception):
    """Raised when a user profile cannot be loaded from Google Sheets."""


class UserProfileService:
    """Find one profile in two Google spreadsheets and cache it locally."""

    def __init__(self, database: MessageDatabase | None = None):
        self.database = database or MessageDatabase()
        self.anamnez_credentials = Config.ANAMNEZ_GOOGLE_CREDENTIALS
        self.after_tests_credentials = Config.AFTER_TESTS_GOOGLE_CREDENTIALS
        self.anamnez_spreadsheet = Config.ANAMNEZ_SPREADSHEET_NAME
        self.after_tests_spreadsheet = Config.AFTER_TESTS_SPREADSHEET_NAME

    @staticmethod
    def _validate_credentials(path_value: str, setting_name: str) -> Path:
        if not path_value:
            raise UserProfileError(f"{setting_name} is not configured")
        path = Path(path_value)
        if not path.is_file():
            raise UserProfileError(f"Google credentials file not found: {path}")
        return path

    @staticmethod
    def _find_row(worksheet, user_id: str) -> int | None:
        import gspread

        identifier = re.compile(rf"^{re.escape(user_id)}(?:\.0+)?$")
        try:
            cell = worksheet.find(identifier, in_column=1)
        except gspread.exceptions.CellNotFound:
            return None
        return cell.row if cell is not None else None

    def _load_anamnez(self, user_id: str) -> dict | None:
        import gspread

        credentials = self._validate_credentials(
            self.anamnez_credentials,
            "ANKETA_GOOGLE_CREDENTIALS",
        )
        client = gspread.service_account(filename=str(credentials))
        spreadsheet = client.open(self.anamnez_spreadsheet)
        result = {}

        anketa_sheet = spreadsheet.worksheet("user_anketa")
        anketa_row_number = self._find_row(anketa_sheet, user_id)
        if anketa_row_number is not None:
            # user_anketa: B=organization, C=exam date, D=age, E=weight, F=height.
            values = anketa_sheet.get(f"B{anketa_row_number}:F{anketa_row_number}")
            row = values[0] if values else []
            result.update(
                {
                    "organization_or_inn": self._value(row, 0),
                    "osmotr_date": self._value(row, 1),
                    "age": self._value(row, 2),
                    "weight": self._value(row, 3),
                    "height": self._value(row, 4),
                }
            )

        user_data_sheet = spreadsheet.worksheet("user_data")
        user_data_row_number = self._find_row(user_data_sheet, user_id)
        if user_data_row_number is not None:
            # user_data: B=name, D=phone. Other fields are intentionally not exposed.
            values = user_data_sheet.get(
                f"B{user_data_row_number}:D{user_data_row_number}"
            )
            row = values[0] if values else []
            result.update(
                {
                    "full_name": self._value(row, 0),
                    "phone": self._value(row, 2),
                }
            )

        return result or None

    @staticmethod
    def _value(row: list, index: int) -> str | None:
        if len(row) <= index or row[index] in (None, ""):
            return None
        return str(row[index]).strip() or None

    def _load_after_tests_profile(self, user_id: str) -> dict | None:
        import gspread

        credentials = self._validate_credentials(
            self.after_tests_credentials,
            "AFTER_TESTS_GOOGLE_CREDENTIALS",
        )
        client = gspread.service_account(filename=str(credentials))
        worksheet = client.open(self.after_tests_spreadsheet).worksheet("users_max")
        row_number = self._find_row(worksheet, user_id)
        if row_number is None:
            return None

        # users_max: B=sex, E=med_id, F=user_state, G=chel_id.
        values = worksheet.get(f"B{row_number}:G{row_number}")
        row = values[0] if values else []
        return {
            "sex": self._value(row, 0),
            "med_id": self._value(row, 3),
            "user_state": self._value(row, 4),
            "chel_id": self._value(row, 5),
        }

    async def _load_from_google(self, user_id: str) -> dict | None:
        try:
            anamnez, after_tests = await asyncio.gather(
                asyncio.to_thread(self._load_anamnez, user_id),
                asyncio.to_thread(self._load_after_tests_profile, user_id),
            )
        except Exception as exc:
            raise UserProfileError("Google Sheets profile request failed") from exc

        if anamnez is None and after_tests is None:
            return None
        return {"external_user_id": user_id, **(anamnez or {}), **(after_tests or {})}

    async def get_profile(
        self,
        user_id: str | int,
        *,
        refresh: bool = False,
    ) -> dict | None:
        """Return cached data or make one on-demand Google lookup."""
        normalized_user_id = str(user_id).strip()
        if not refresh:
            cached = self.database.get_user_profile(normalized_user_id)
            if cached is not None:
                return cached

        profile = await self._load_from_google(normalized_user_id)
        if profile is not None:
            self.database.upsert_user_profile(**profile)
        return profile

    async def get_client_profile(
        self,
        user_id: str | int,
        *,
        refresh: bool = False,
    ) -> dict | None:
        normalized_user_id = str(user_id).strip()
        cached = self.database.get_user_profile(normalized_user_id)
        if not refresh and cached is not None and cached.get("client_synced_at"):
            return cached
        return await self.get_profile(normalized_user_id, refresh=True)

    def _load_results(self, user_id: str) -> dict | None:
        import gspread

        credentials = self._validate_credentials(
            self.after_tests_credentials,
            "AFTER_TESTS_GOOGLE_CREDENTIALS",
        )
        client = gspread.service_account(filename=str(credentials))
        spreadsheet = client.open(self.after_tests_spreadsheet)

        users_sheet = spreadsheet.worksheet("users_max")
        user_row = self._find_row(users_sheet, user_id)
        if user_row is None:
            return None
        med_id_value = users_sheet.acell(f"E{user_row}").value
        med_id = str(med_id_value).strip() if med_id_value else ""
        if not med_id:
            return {"external_user_id": user_id, "med_id": None, "results": None}

        results_sheet = spreadsheet.worksheet("tests_and_results")
        results_row = self._find_row(results_sheet, med_id)
        if results_row is None:
            return {"external_user_id": user_id, "med_id": med_id, "results": None}
        results_value = results_sheet.acell(f"B{results_row}").value
        results = str(results_value).strip() if results_value else None
        return {"external_user_id": user_id, "med_id": med_id, "results": results}

    async def get_results(
        self,
        user_id: str | int,
        *,
        refresh: bool = False,
    ) -> dict | None:
        normalized_user_id = str(user_id).strip()
        if not refresh:
            cached = self.database.get_user_test_results(normalized_user_id)
            if cached is not None:
                return cached
        try:
            result = await asyncio.to_thread(self._load_results, normalized_user_id)
        except Exception as exc:
            raise UserProfileError("Google Sheets results request failed") from exc
        if result and result.get("med_id") and result.get("results"):
            self.database.upsert_user_test_results(**result)
        return result

    @staticmethod
    def format_profile(profile: dict) -> str:
        fields = (
            ("Пол", profile.get("sex")),
            ("Возраст", profile.get("age")),
            ("Вес", profile.get("weight")),
            ("Рост", profile.get("height")),
        )
        lines = ["🗂 Анкета пользователя"]
        lines.extend(f"{label}: {value}" for label, value in fields if value not in (None, ""))
        if len(lines) == 1:
            lines.append("В анкете нет заполненных данных.")
        return "\n".join(lines)

    @staticmethod
    def format_client(profile: dict, fallback_name: str | None = None) -> str:
        fields = (
            ("Имя", profile.get("full_name") or fallback_name),
            ("MAX ID", profile.get("external_user_id")),
            ("Телефон", profile.get("phone")),
            ("Медицинский ID", profile.get("med_id")),
            ("chel_id", profile.get("chel_id")),
            ("Организация / ИНН", profile.get("organization_or_inn")),
            ("Дата осмотра", profile.get("osmotr_date")),
        )
        lines = ["👤 Карточка клиента"]
        lines.extend(f"{label}: {value}" for label, value in fields if value not in (None, ""))
        return "\n".join(lines)

    @staticmethod
    def format_results(result: dict) -> str:
        return "\n".join(
            (
                "🧪 Результаты анализов",
                f"Медицинский ID: {result['med_id']}",
                "",
                str(result["results"]),
            )
        )
