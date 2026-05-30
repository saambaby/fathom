"""Fathom application settings.

Loaded from a .env file (or environment variables) via pydantic-settings.
Conforms to INV-08: secrets are read via SecretStr and never logged/printed directly.

Usage:
    from config.settings import Settings
    settings = Settings()  # raises ValidationError if required fields are absent
    token = settings.oanda_api_token.get_secret_value()  # explicit secret access
"""

from typing import Literal, Optional

from pydantic import Field, SecretStr, model_validator
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

    # --- Phase 5: live-trading gate fields (INV-05 / INV-07) ----------------
    #: Enable live-order placement. Defaults to ``False`` — an explicit opt-in
    #: is required before a live `fathom execute` can proceed (D-P5-2).
    live_trading_enabled: bool = False

    #: Per-trade risk fraction for live orders.  Validated ≤ 0.0025 (the
    #: INV-05 per-trade cap) so a ``.env`` typo can never exceed the cap (B-5).
    #: Default 0.001 (0.10%) — the reduced initial live size (D-P5-3).
    live_risk_fraction: float = Field(
        default=0.001,
        gt=0.0,
        le=0.0025,
        description=(
            "Per-trade risk fraction for live orders (default 0.001 = 0.10%). "
            "Must be > 0 and ≤ 0.0025 (the INV-05 0.25% per-trade cap). "
            "Validated at Settings construction so a .env typo raises immediately."
        ),
    )

    @model_validator(mode="after")
    def derive_base_url(self) -> "Settings":
        """Derive oanda_base_url from env if not explicitly set."""
        if not self.oanda_base_url:
            object.__setattr__(self, "oanda_base_url", _BASE_URLS[self.env])
        return self
