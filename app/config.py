import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "storage"

# Always load the project .env, regardless of the directory from which uvicorn
# was started.
load_dotenv(PROJECT_ROOT / ".env")


class Config:
    APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
    APP_PORT = int(os.getenv("APP_PORT", 8000))

    # Support the existing .env names and the more explicit BITRIX_* variants.
    CLIENT_ID = os.getenv("BITRIX_CLIENT_ID") or os.getenv("CLIENT_ID", "")
    CLIENT_SECRET = os.getenv("BITRIX_CLIENT_SECRET") or os.getenv("CLIENT_SECRET", "")
    BITRIX_DOMAIN = os.getenv("BITRIX_DOMAIN", "")
    BITRIX_ACCESS_TOKEN = os.getenv("BITRIX_ACCESS_TOKEN", "")
    BITRIX_OPENLINE_ID = os.getenv("BITRIX_OPENLINE_ID", "17")
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    BITRIX_APPLICATION_TOKEN = os.getenv("BITRIX_APPLICATION_TOKEN", "")
    # A separate token is preferred. Falling back to the Bitrix application
    # token keeps existing installations operable while still protecting the
    # administrative HTTP endpoints.
    CONNECTOR_ADMIN_TOKEN = (
        os.getenv("CONNECTOR_ADMIN_TOKEN") or BITRIX_APPLICATION_TOKEN
    )
    MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")
    MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
    MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "")
    MAX_CONNECTOR_ID = os.getenv("MAX_CONNECTOR_ID", "max_bot_bridge")
    ANAMNEZ_GOOGLE_CREDENTIALS = os.getenv("ANKETA_GOOGLE_CREDENTIALS", "")
    AFTER_TESTS_GOOGLE_CREDENTIALS = os.getenv(
        "AFTER_TESTS_GOOGLE_CREDENTIALS", ""
    )
    ANAMNEZ_SPREADSHEET_NAME = os.getenv(
        "ANKETA_GOOGLE_SPREADSHEET_NAME", "anamnez_db_max"
    )
    AFTER_TESTS_SPREADSHEET_NAME = os.getenv(
        "AFTER_TESTS_GOOGLE_SPREADSHEET_NAME", "after_tests_db"
    )
    JOB_MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", 8))
    JOB_POLL_SECONDS = float(os.getenv("JOB_POLL_SECONDS", 0.5))
