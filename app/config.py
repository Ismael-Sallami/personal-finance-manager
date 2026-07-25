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
