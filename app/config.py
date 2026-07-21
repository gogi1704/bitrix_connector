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
    BITRIX_INSTALL_TOKEN = os.getenv("BITRIX_INSTALL_TOKEN", "")
    # Optional legacy override. Normally Bitrix sends application_token during
    # installation and it is persisted in storage/oauth.json.
    BITRIX_APPLICATION_TOKEN = os.getenv("BITRIX_APPLICATION_TOKEN", "")
    CONNECTOR_ADMIN_TOKEN = os.getenv("CONNECTOR_ADMIN_TOKEN", "")
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
    JOB_FAILED_RETENTION_DAYS = int(os.getenv("JOB_FAILED_RETENTION_DAYS", 7))
    JOB_COMPLETED_RETENTION_DAYS = int(os.getenv("JOB_COMPLETED_RETENTION_DAYS", 30))
    MEDIA_MAX_BYTES = int(os.getenv("MEDIA_MAX_BYTES", 50 * 1024 * 1024))
    MEDIA_DOWNLOAD_TIMEOUT = float(os.getenv("MEDIA_DOWNLOAD_TIMEOUT", 120))

    # Analyze a dialog once after a period of silence. The feature works with
    # conservative templates when AI is disabled or temporarily unavailable.
    FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "true").lower() in {
        "1", "true", "yes", "on"
    }
    FOLLOWUP_DELAY_MINUTES = int(os.getenv("FOLLOWUP_DELAY_MINUTES", 30))
    FOLLOWUP_NEXT_DAY_HOUR = int(os.getenv("FOLLOWUP_NEXT_DAY_HOUR", 9))
    FOLLOWUP_TIMEZONE = os.getenv("FOLLOWUP_TIMEZONE", "Europe/Moscow")
    FOLLOWUP_QUIET_START_HOUR = int(os.getenv("FOLLOWUP_QUIET_START_HOUR", 21))
    FOLLOWUP_QUIET_END_HOUR = int(os.getenv("FOLLOWUP_QUIET_END_HOUR", 9))
    FOLLOWUP_MAX_CONTEXT_MESSAGES = int(os.getenv("FOLLOWUP_MAX_CONTEXT_MESSAGES", 20))
    FOLLOWUP_AI_ENABLED = os.getenv("FOLLOWUP_AI_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }
    FOLLOWUP_AI_API_KEY = os.getenv("FOLLOWUP_AI_API_KEY", "")
    FOLLOWUP_AI_BASE_URL = os.getenv(
        "FOLLOWUP_AI_BASE_URL", "https://api.openai.com/v1"
    ).rstrip("/")
    FOLLOWUP_AI_MODEL = os.getenv("FOLLOWUP_AI_MODEL", "gpt-5-mini")
    FOLLOWUP_AI_TIMEOUT = float(os.getenv("FOLLOWUP_AI_TIMEOUT", 30))

    REMINDER_MAX_DAYS = int(os.getenv("REMINDER_MAX_DAYS", 365))
    ANALYTICS_REFRESH_SECONDS = int(os.getenv("ANALYTICS_REFRESH_SECONDS", 60))
