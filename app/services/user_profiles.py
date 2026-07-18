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
        worksheet = client.open(self.anamnez_spreadsheet).worksheet("user_anketa")
        row_number = self._find_row(worksheet, user_id)
        if row_number is None:
            return None

        # user_anketa: D=age, E=weight, F=height.
        values = worksheet.get(f"D{row_number}:F{row_number}")
        row = values[0] if values else []
        return {
            "age": str(row[0]).strip() if len(row) > 0 else None,
            "weight": str(row[1]).strip() if len(row) > 1 else None,
            "height": str(row[2]).strip() if len(row) > 2 else None,
        }

    def _load_sex(self, user_id: str) -> str | None:
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

        # users_max: B=user_name, which currently stores the user's sex.
        value = worksheet.acell(f"B{row_number}").value
        return value.strip() if value else None

    async def _load_from_google(self, user_id: str) -> dict | None:
        try:
            anamnez, sex = await asyncio.gather(
                asyncio.to_thread(self._load_anamnez, user_id),
                asyncio.to_thread(self._load_sex, user_id),
            )
        except Exception as exc:
            raise UserProfileError("Google Sheets profile request failed") from exc

        if anamnez is None and sex is None:
            return None
        return {
            "external_user_id": user_id,
            "age": anamnez.get("age") if anamnez else None,
            "weight": anamnez.get("weight") if anamnez else None,
            "height": anamnez.get("height") if anamnez else None,
            "sex": sex,
        }

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
