"""Fathom application settings.

Loaded from a .env file (or environment variables) via pydantic-settings.
Conforms to INV-08: secrets are read via SecretStr and never logged/printed directly.

Usage:
    from config.settings import Settings
    settings = Settings()  # raises ValidationError if required fields are absent
    token = settings.oanda_api_token.get_secret_value()  # explicit secret access
"""

from typing import Literal, Optional

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_URLS: dict[str, str] = {
    "demo": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


class Settings(BaseSettings):
    """Application-wide configuration — validated at startup.

    Required fields (must be present in .env or environment):
        OANDA_API_TOKEN   — OANDA v20 API bearer token (SecretStr)
        OANDA_ACCOUNT_ID  — OANDA account identifier string

    Optional fields (have sensible defaults):
        ENV               — "demo" (default) or "live"
        OANDA_BASE_URL    — auto-derived from ENV; only override if you know what you're doing
        DISCORD_WEBHOOK_URL — Discord webhook URL for alert/watchlist delivery (SecretStr);
                              required at runtime by the deviation monitor alerter (T-09) and
                              the Phase 2 Hermes watchlist job.  Optional here so the Settings
                              model can be constructed in contexts that do not need Discord
                              (e.g. backtest-only runs). INV-08: stored as SecretStr, never logged.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    env: Literal["demo", "live"] = "demo"
    oanda_api_token: SecretStr
    oanda_account_id: str
    oanda_base_url: str = ""
    discord_webhook_url: Optional[SecretStr] = None

    @model_validator(mode="after")
    def derive_base_url(self) -> "Settings":
        """Derive oanda_base_url from env if not explicitly set."""
        if not self.oanda_base_url:
            object.__setattr__(self, "oanda_base_url", _BASE_URLS[self.env])
        return self
