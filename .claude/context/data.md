# Data context

## POC-T-02 — 2026-05-28 (feat/poc-t-02)

**What was done:**
- Created `data/__init__.py` (empty package marker).
- Created `data/oanda_client.py` with:
  - `OandaAPIError(status_code, message)` — typed exception for HTTP 4xx/5xx.
  - `CandleRow` pydantic v2 model with all bid/ask/mid price fields (float), `volume: int`,
    `complete: bool`, and `time: datetime` (UTC-aware, INV-03).
  - `_parse_utc(iso_string)` — strips nanosecond precision and trailing "Z", then attaches
    `timezone.utc` explicitly. Handles both `T14:00:00Z` and `T14:00:00.000000000Z` forms.
  - `OandaClient(settings: Settings)` — uses `oandapyV20.API(environment=...)` where
    environment is derived from `settings.env` via `_ENV_MAP` (demo→"practice", live→"live")
    exclusively (INV-09). Token read via `settings.oanda_api_token.get_secret_value()` (INV-08).
  - `get_candles(instrument, granularity, count, from_time=None) -> list[CandleRow]`:
    auto-paginates when `count > 500`; each page beyond the first requests `page_size + 1`
    candles anchored at the last known time and drops the first (duplicate) result.
    Stops early if OANDA returns fewer candles than requested.

**Key oandapyV20 API notes (D-01):**
- Class is `oandapyV20.endpoints.instruments.InstrumentsCandles`, NOT `InstrumentsCandlesRequest`.
- Environment keys for `oandapyV20.API(environment=...)`: `"practice"` (demo) and `"live"`,
  as defined in `TRADING_ENVIRONMENTS` in `oandapyV20/oandapyV20.py`.
- Library raises `V20Error(code, msg)` on HTTP ≥ 400; we wrap it in `OandaAPIError`.
- `price="BAM"` in params returns bid, ask, and mid sub-dicts in each candle.

**pyproject.toml fix:**
- Build backend was `setuptools.backends.legacy:build` (T-01), but `setuptools.backends`
  subpackage does not exist in setuptools 82.x. Changed to `setuptools.build_meta`.
- Added `responses>=0.25` to `[project.optional-dependencies] dev` for HTTP mocking in tests.

**Patterns established:**
- All timestamps parsed immediately to UTC-aware `datetime` via `_parse_utc()` — never store
  naive datetimes (INV-03).
- `_ENV_MAP` is the single place that translates `settings.env` → oandapyV20 env string; no
  `if env == "live":` branches in logic (INV-09).
- Tests use `responses` library (`@responses.activate`) to mock HTTP without any live calls.
- `_make_settings()` helper in tests uses `SecretStr(...)` for the token field to satisfy mypy.

**AC verification results:**
- `pytest tests/test_oanda_client.py -v` → 22 passed, exit 0
- `pytest -v` (full suite) → 25 passed, exit 0
- `mypy data/ tests/test_oanda_client.py` → "Success: no issues found in 3 source files", exit 0

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)
