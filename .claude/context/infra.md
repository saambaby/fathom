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

---

## P5-T-01 — 2026-05-30 (feat/p5-T-01-settings)

**What was done:**
- `config/settings.py`: added two Phase 5 live-trading gate fields (additive, no existing behaviour changed):
  - `live_trading_enabled: bool = False` — explicit opt-in required before any live order (INV-07, D-P5-2).
  - `live_risk_fraction: float = Field(default=0.001, gt=0.0, le=0.0025)` — per-trade risk fraction for live orders; `le=0.0025` mirrors the INV-05 0.25% per-trade cap so the two cannot drift (B-5). A `Settings` constructed with `live_risk_fraction <= 0` or `> 0.0025` raises `pydantic.ValidationError` at load time.
  - Added `Field` to the pydantic import. Pattern mirrors `LimitsConfig`'s `Field` constraints in `risk/limits.py`.
- `.env.example`: added `LIVE_TRADING_ENABLED=false` and `LIVE_RISK_FRACTION=0.001` entries (required by the drift-guard test in `test_config.py`).
- `risk/limits.py`: added `kill_switch_armed(account_state, now, *, config, staleness_minutes=10) -> tuple[bool, str]` (pure, exported in `__all__`). Single source of truth for kill-switch readiness used by the preflight check (P5-T-03). "Armed and healthy" = account_state present + `as_of` within `staleness_minutes` of `now` (UTC, INV-03) + `kill_switch_status().active is False`. Returns `(True, "")` or `(False, "missing" | "stale" | "tripped")`. Reuses `kill_switch_status` internally — no logic duplication.
  - `as_of` is parsed from RFC 3339 UTC string via `fromisoformat(s.rstrip("Z")).replace(tzinfo=timezone.utc)` (codebase-standard pattern). The `dict[str, object]` values are cast with `# type: ignore[arg-type]` before `float()` (mypy cannot narrow from `object`; the value contract is documented).

**Patterns established / confirmed:**
- `Field(gt=0.0, le=0.0025)` in `Settings` validates at construction — same pattern as `LimitsConfig.daily_loss_cap` etc.
- `kill_switch_armed` is the single-source readiness helper; never re-implement the tripped/staleness logic elsewhere — always call this.
- Staleness window default is 10 min (D-P5-B). Callers can override via `staleness_minutes`.
- RFC 3339 parsing: `datetime.fromisoformat(s.rstrip("Z")).replace(tzinfo=timezone.utc)` — matches `execution/reconcile.py` and `execution/orders.py` pattern.

**AC verification results:**
- `mypy .` (87 files) → "Success: no issues found", exit 0
- `pytest -q` → 1058 passed (18 new), 87 pre-existing warnings, exit 0
- `pytest tests/test_limits.py tests/test_config.py -v` → 54 passed, exit 0

**New dependency added to pyproject.toml:** NO

**Merge plan:** `gh pr merge 125 --squash --delete-branch`
