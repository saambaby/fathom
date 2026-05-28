# Infra context

## POC-T-01 — 2026-05-28 (feat/poc-t-01)

**What was done:**
- Created `pyproject.toml` with `requires-python = ">=3.11"` and runtime deps:
  `pydantic>=2`, `pydantic-settings>=2`, `python-dotenv>=1.0`, `oandapyV20>=0.6`, `pandas>=2.0`.
  Dev deps (optional group `dev`): `pytest>=7.4`, `mypy>=1.8`.
- Created `config/settings.py` — `Settings(BaseSettings)` with:
  - `env: Literal["demo", "live"] = "demo"`
  - `oanda_api_token: SecretStr` (required)
  - `oanda_account_id: str` (required)
  - `oanda_base_url: str` — **auto-derived** from `env` via `@model_validator(mode="after")`;
    demo → `https://api-fxpractice.oanda.com`, live → `https://api-fxtrade.oanda.com`.
    User does not set this key.
- Created `.env.example` listing `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, `ENV` with placeholders.
- Created `tests/test_config.py` with 3 tests:
  1. Drift guard: `.env.example` keys ↔ `Settings` fields (excluding derived `oanda_base_url`).
  2. Validation guard: `Settings()` raises `pydantic.ValidationError` on missing required fields.
  3. URL derivation: demo/live `env` values produce correct `oanda_base_url`.

**Patterns established:**
- `SecretStr.get_secret_value()` is the only way to read the token — never access `.oanda_api_token` directly.
- pydantic v2 uses `model_config = SettingsConfigDict(...)` not inner `class Config`.
- `BaseSettings` is in `pydantic_settings`, NOT `pydantic`.

**AC verification results:**
- `pytest tests/test_config.py -v` → 3 passed, exit 0
- `mypy config/` → "Success: no issues found in 2 source files", exit 0

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)
