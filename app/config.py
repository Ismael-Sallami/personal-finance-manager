"""Central configuration, read from environment variables (.env).

Every value that changes between users lives here, so the code never carries
personal data or machine-specific paths.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (this file is at <root>/app/config.py).
APP_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=APP_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- DB ---
    # Empty DATABASE_URL => local SQLite (dev). Set it to use Postgres.
    database_url: str = ""

    # --- Security ---
    secret_key: str = "dev-insecure-change-me"
    cookie_secure: bool = False
    # In production (is_prod True) the API docs are hidden and HSTS is sent.
    is_prod: bool = False

    # --- Single user ---
    app_user_email: str = "admin@local"
    app_user_password: str = "admin"

    # --- Display ---
    report_owner: str = "Account owner"
    currency_symbol: str = "€"
    # Timezone the scheduled jobs run in (IANA name).
    timezone: str = "UTC"

    # --- Investments: ISIN -> ticker lookup (OpenFIGI, optional) ---
    openfigi_api_key: str = ""

    # --- Telegram bot ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""        # allowlist (owner only). Comma separated.
    telegram_webhook_secret: str = ""
    public_base_url: str = ""         # e.g. https://your-app.example.com

    # --- Scheduled tasks ---
    # In-process scheduler: only on always-on hosts. Where the app sleeps, keep
    # it false and let an external cron hit /tasks/* (see app/routers/tasks.py).
    use_scheduler: bool = False
    # Token that authorises calls to /tasks/*. Empty = endpoints disabled.
    tasks_token: str = ""

    # --- Bank sync (Enable Banking, PSD2 read-only) ---
    # Empty EB_* = bank mode off, manual mode (see docs/BANK_SYNC.md).
    # EB_PRIVATE_KEY is the content of the .pem (real newlines or escaped \\n).
    eb_app_id: str = ""
    eb_private_key: str = ""
    # Entries before this date (yyyy-mm-dd) are never imported, so nothing you
    # already typed by hand gets duplicated. Empty = start of the current month.
    bank_sync_from: str = ""
    # Platforms used only to invest (comma list, matched inside the bank name).
    # Money sent to them from your spending accounts is "Investment"; their own
    # movements are ignored (only their cash balance counts).
    investment_banks: str = "trade republic,myinvestor"
    # How entries get in: auto (bank if EB is configured, else manual) | manual | bank
    entry_mode: str = "auto"

    @property
    def bank_sync_enabled(self) -> bool:
        if self.entry_mode.strip().lower() == "manual":
            return False
        return bool(self.eb_app_id.strip() and self.eb_private_key.strip())

    @property
    def mode(self) -> str:
        """Effective entry mode: "bank" or "manual"."""
        return "bank" if self.bank_sync_enabled else "manual"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.strip())

    @property
    def allowed_chat_ids(self) -> set[str]:
        return {c.strip() for c in self.telegram_chat_id.split(",") if c.strip()}

    @property
    def sqlalchemy_url(self) -> str:
        """Final SQLAlchemy URL. Falls back to SQLite when no Postgres is set."""
        if self.database_url.strip():
            return self.database_url.strip()
        return f"sqlite:///{APP_ROOT / 'dev.db'}"

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
