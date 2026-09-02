"""
app/core/config.py
------------------
Centralised application settings loaded from environment variables via
pydantic-settings.  All settings have sensible defaults so the app
starts without a .env file during local development.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------ #
    # Project metadata                                                     #
    # ------------------------------------------------------------------ #
    PROJECT_NAME: str = "Revora Revenue Recovery Engine"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = True

    # ------------------------------------------------------------------ #
    # Razorpay credentials                                                 #
    # ------------------------------------------------------------------ #
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ------------------------------------------------------------------ #
    # Database                                                             #
    # ------------------------------------------------------------------ #
    DATABASE_URL: str = "sqlite+aiosqlite:///./revora.db"

    # ------------------------------------------------------------------ #
    # Gemini AI Configuration (Phase 8B)                                   #
    # ------------------------------------------------------------------ #
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ------------------------------------------------------------------ #
    # Pydantic-Settings configuration                                      #
    # ------------------------------------------------------------------ #
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()


settings: Settings = get_settings()
