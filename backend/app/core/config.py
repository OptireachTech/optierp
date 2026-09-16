"""Application settings.

All configuration is read from environment variables (or a local ``.env``)
via pydantic-settings — no hardcoded secrets anywhere (Section 9, rule 5).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "OptiReach ERP"
    environment: str = "development"  # development | staging | production
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # --- Security ---
    secret_key: str = Field(min_length=32, description="JWT signing key. Required, no default.")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    refresh_cookie_name: str = "optireach_refresh"
    refresh_cookie_secure: bool = True  # set False only for plain-http local dev

    # --- Database ---
    # The application connects as a NON-owner role so PostgreSQL row-level
    # security applies. Alembic migrations use the owner role.
    # MANUAL_REVIEW: multi-tenancy model — shared DB + RLS assumed (vs. DB-per-tenant).
    database_url: str = Field(description="postgresql+asyncpg://erp_app:...@host/erp")
    migrations_database_url: str | None = Field(
        default=None, description="Owner-role DSN for Alembic; defaults to database_url."
    )
    db_echo: bool = False

    # --- Redis (websocket pub/sub, rate limiting) ---
    # MANUAL_REVIEW: Redis assumed available for realtime + rate limiting.
    redis_url: str = "redis://localhost:6379/0"

    # --- Rate limiting (Privacy Phase 0 — credential-testing surface) ---
    # Applied to /auth/login and /auth/refresh, keyed by client IP. Fails open
    # (logs a warning, lets the request through) if Redis is unreachable —
    # availability over strict enforcement, matching this app's existing
    # posture toward Redis (see app/core/websocket.py).
    auth_rate_limit_enabled: bool = True
    auth_rate_limit_attempts: int = 10
    auth_rate_limit_window_seconds: int = 60

    # --- CORS ---
    allowed_origins: str = "http://localhost:5173"

    # Public base URL of the SPA. Used to build the tokenised links emailed to
    # directors, who have no login (Module 13) — the link has to be absolute because
    # it is opened from an email client, not from within the app.
    public_base_url: str = "http://localhost:8080"

    # --- Email ---
    # MANUAL_REVIEW: email provider — plain SMTP assumed (vs. SendGrid/SES).
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"
    smtp_tls: bool = True

    # --- Scheduler ---
    scheduler_enabled: bool = True

    # --- OCR (line-item extraction from scanned invoices / POs) ---
    # OpenAI-compatible vision API. Empty key → extract returns `ocr_not_configured`.
    ocr_provider: str = "openai"  # openai | azure | disabled
    ocr_api_key: str = ""
    ocr_api_base: str = "https://api.openai.com/v1"
    ocr_model: str = "gpt-4o-mini"
    ocr_timeout_seconds: float = 90
    ocr_max_bytes: int = 10 * 1024 * 1024

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def alembic_url(self) -> str:
        return self.migrations_database_url or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
