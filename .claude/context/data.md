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

---

## POC-T-03 — 2026-05-28 (feat/poc-t-03)

**What was done:**
- Created `data/store.py` with `Store` class:
  - `__init__(db_path)` — opens SQLite (accepts `":memory:"`), creates `candles` table with PK `(instrument, granularity, time)`.
  - `upsert(rows: Iterable[CandleRow])` — `INSERT OR REPLACE`; silently drops `complete=False` rows (only completed bars stored).
  - `load_candles(instrument, granularity, start, end) -> pd.DataFrame` — returns `time (datetime64[ns, UTC])`, bid/ask OHLC `float64`, `volume int64`.
  - `get_cached_times(instrument, granularity, start, end) -> set[str]` — returns RFC 3339 strings for rows present; used for gap detection.
  - `_to_rfc3339(dt)` — converts UTC-aware datetime to `"2024-01-15T14:00:00Z"` string (INV-03).
- Created `data/candles.py` with `fetch_and_cache(client, store, instrument, granularity, start, end) -> pd.DataFrame`:
  - Gap-aware: calls `get_cached_times` to find leading/trailing gaps; only fetches from OANDA for missing sub-ranges.
  - Cache-hit: if `[start, end]` is fully covered by the store, zero HTTP calls are made.
  - Posts `count=50_000` to `client.get_candles` (auto-paginated by OandaClient) to cover up to 2-year PoC windows.
  - Filters OANDA response to `r.time <= end` and `r.complete` before upsert.
  - Returns `store.load_candles(...)` as the single source of truth.

**Key patterns and gotchas (D-02 / INV-03):**
- `pd.to_datetime(..., utc=True)` is required — default produces tz-naive series.
- pandas 3.x (and 2.x) resolves string timestamps to `datetime64[us, UTC]` by default.
  We coerce to `datetime64[ns, UTC]` with `.astype("datetime64[ns, UTC]")` to match the AC dtype contract.
- Time stored as TEXT (never PARSE_DECLTYPES) — see library_defaults note in taskgraph.
- Gap detection uses min/max of cached timestamps: if `max_cached < end`, trailing gap; if `min_cached > start`, leading gap; both → fetch full range (upsert is idempotent).
- Cache-hit only triggers when `start == min_cached AND end == max_cached` (or store bounds cover exactly). Tests set `end = last_candle_time` to exercise true zero-call behaviour.
- `MagicMock()` is the right tool for mocking `OandaClient` — no `responses` library needed here (no HTTP stack involved).

**AC verification results:**
- `pytest tests/test_store_and_candles.py -v` → 17 passed, exit 0
- `pytest -v` (full suite) → 91 passed, exit 0
- `mypy data/ tests/test_store_and_candles.py` → "Success: no issues found in 5 source files", exit 0

**No new dependencies added** (sqlite3 is stdlib; pandas already in pyproject.toml).
**CLAUDE.md trigger-table check:** no new dep, no new CLI command — CLAUDE.md not edited.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)
