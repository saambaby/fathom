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

---

## P1A-T-01 — 2026-05-29 (feat/p1a-t-01)

**What was done:**
- Added `InstrumentMeta` pydantic v2 model to `data/oanda_client.py`:
  - Fields: `name`, `pip_location (int)`, `min_trade_size`, `margin_rate`,
    `display_precision`, `long_rate`, `short_rate`, `financing_days_of_week`.
  - Canonical financing field names (`long_rate`/`short_rate`; swap-cost model
    maps these to `CostParams.swap_long_rate/swap_short_rate` at engine boundary).
  - `field_validator` coerces OANDA string rates to float at the boundary.
  - `_instrument_from_raw()` helper maps OANDA camelCase wire format; parses
    `financingDaysOfWeek` dicts to int weekday numbers (0=Mon, 6=Sun); ignores
    unknown day strings silently.
- Added `OandaClient.list_instruments() -> list[InstrumentMeta]`:
  - Uses `oandapyV20.endpoints.accounts.AccountInstruments`.
  - Mirrors existing `get_candles` error pattern: `V20Error` → `OandaAPIError`.
  - Filters to `type == "CURRENCY"` only (INV-09: account-scoped).
- Extended `data/store.py` with:
  - `instruments` SQLite table (PK: `name`); created in `_create_tables`.
  - `upsert_instruments(instruments, fetched_at=None)` — idempotent; stores
    `financing_days_of_week` as JSON string; timestamps UTC RFC 3339 (INV-03).
  - `load_instruments() -> list[InstrumentMeta]` — reconstructs from SQLite.
  - `write_parquet(instrument, granularity, df)` — pyarrow; archive layout
    `{archive_dir}/{instrument}/{granularity}/{YYYY-MM-DD}.parquet`. Granularity
    encoded in path (not in file) to prevent collisions between H1/H4 on same date.
  - `load_parquet(instrument, granularity, start, end)` — enumerates daily files
    in date range, reads and concatenates, filters to [start, end], coerces dtypes.
  - Backward-compat alias `_UPSERT_SQL = _UPSERT_CANDLE_SQL` for existing test helpers.
  - `_archive_dir: Path | None` type annotation to satisfy mypy strict mode.
- Updated `data/candles.py` `fetch_and_cache`:
  - Added `write_parquet: bool = True` parameter.
  - When `True` and new rows fetched: calls `store.write_parquet()` after SQLite upsert.
  - When `False`: Parquet write skipped (for in-memory-store tests without archive_dir).
  - Return contract (DataFrame shape) unchanged.
- Added `pyarrow>=14` to `pyproject.toml` dependencies.
- Updated `CLAUDE.md` Stack line to include `pyarrow>=14`.

**Key patterns and gotchas (pyarrow/INV-03):**
- pyarrow Parquet round-trip DOES preserve UTC timezone: stores tz in column
  metadata; reads back as `datetime64[us, UTC]` which is coerced to
  `datetime64[ns, UTC]` via `.astype()` in `load_parquet`. Confirmed by test.
- Granularity encoded in file path, not as a column in the Parquet file.
  H1 and H4 for same instrument+date → separate subdirectories, no collision.
- `pq.write_table` and `pq.read_table` are untyped in pyarrow stubs; suppressed
  with `# type: ignore[no-untyped-call]`.
- `Store(":memory:")` with no `archive_dir` sets `_archive_dir = None`; any
  `write_parquet`/`load_parquet` call raises `RuntimeError` immediately.
  Tests that don't need Parquet pass `write_parquet=False` to `fetch_and_cache`.
- `financing_days_of_week` stored as JSON string in SQLite; reconstructed via
  `json.loads` on load. `daysCharged` multiplier not stored (cost model handles it).

**AC verification results:**
- `pytest tests/test_data_layer_expansion.py -v` → 26 passed, exit 0
- `pytest -v` (full suite) → 171 passed, exit 0
- `mypy data/` → "Success: no issues found in 4 source files", exit 0

**New dependency:** `pyarrow>=14` added to `pyproject.toml`.
**CLAUDE.md trigger-table:** pyproject.toml dep added → CLAUDE.md Stack updated (YES).

**Merge plan:** `gh pr merge 31 --squash --delete-branch` (lead action after reviewer pass)

---

## 1B-T-01 — 2026-05-29 (feat/p1b-t-01)

**What was done:**
- Created `data/stream.py` with:
  - `OandaStreamError(status_code, message)` — typed exception for stream failures,
    subclass of `OandaAPIError` for consistent error handling.
  - `PriceTick` pydantic v2 model: `instrument`, `time: AwareDatetime` (INV-03),
    `bid`, `ask`, `status`, `gap_detected: bool`.
  - `_parse_utc(iso_string)` — strips nanoseconds and trailing "Z", attaches
    `timezone.utc` explicitly. Mirrors pattern from `oanda_client.py`.
  - `_backoff_delay(attempt)` — capped exponential backoff with ±50% multiplicative
    jitter. Base 1s, multiplier ×2, pre-jitter cap 30s (actual max ≈ 45s with jitter).
  - `_make_tick(msg, gap_detected)` — converts raw PRICE dict to `PriceTick`; returns
    `None` on malformed input.
  - `PriceStream(settings, instruments, heartbeat_timeout=10.0, queue_maxsize=0)`:
    - `start()` / `stop()` — launches/joins a background daemon thread.
    - `get_tick(timeout)` — pulls from the internal `queue.Queue`; returns `None`
      on sentinel (stream stopped); re-raises `OandaStreamError` on error.
    - `__iter__` — iterator interface over ticks until stream stops.
    - `_run()` — main loop: connects, reconnects with backoff, sets `gap_on_next`
      after any disconnect. Never logs the token (INV-08).
    - `_stream_once()` — single long-lived reader thread per connection iterates the
      blocking oandapyV20 generator; run/liveness loop reads from message queue with
      timeout for heartbeat detection. Reader is joined on exit — at most ONE reader
      thread alive at any time, no per-poll thread spawning.
- Created `tests/test_stream.py` with 32 tests (no live HTTP):
  - `TestParseUtc` — nanosecond/microsecond/second precision, UTC-aware output.
  - `TestBackoffDelay` — range check, cap check, median monotonicity, non-negative.
  - `TestMakeTick` — basic tick, gap_detected, non-tradeable status, missing
    bids/asks, invalid price (all return None), UTC-aware timestamp.
  - `TestPriceStreamTickParsing` — single tick, multiple instruments.
  - `TestPriceStreamHeartbeat` — heartbeats not surfaced, liveness timer reset.
  - `TestPriceStreamReconnect` — gap_detected on reconnect, reconnect on timeout,
    backoff called with correct attempt index.
  - `TestPriceStreamShutdown` — thread joins, stop before start, sentinel drains,
    iterator terminates, double-start raises RuntimeError.
  - `TestPriceStreamNoThreadAccumulation` — simulates sustained heartbeat-only
    operation; asserts fathom-stream-reader thread count ≤ 1 at all sample points.
  - `TestPriceStreamTypedErrors` — 4xx → OandaStreamError, inheritance check,
    error delivered via queue (uses real _SENTINEL; asserts second get_tick is None).
  - `TestPriceStreamNoTokenInLogs` — caplog asserts token not in any log record.

**Key oandapyV20 streaming notes:**
- `PricingStream.STREAM = True` → `api.request()` returns a generator (not a dict).
- Generator yields dicts with `type: "PRICE"` or `type: "HEARTBEAT"`.
- `req.terminate(message)` stops the generator; library raises `StreamTerminated`.
- `StreamTerminated` is a subclass of `Exception`, not `GeneratorExit`.
- Generator is a blocking synchronous iterator; single long-lived reader thread
  per connection avoids per-poll thread accumulation.
- 4xx errors (401/403/404) are unrecoverable; 5xx are transient and retry.

**Design decisions (from spec):**
- Background thread + `queue.Queue` — spec lean; simplest fit for sync codebase.
- Tick persistence deferred (spec: Phase 2+); stream exposes live iterator only.
- `_ENV_MAP` mirrors `oanda_client.py`; single-source env→oandapyV20 translation (INV-09).

**AC verification results (post-review fixes):**
- `pytest tests/test_stream.py -v` → 32 passed, exit 0
- `pytest -q` (full suite) → 397 passed, exit 0
- `mypy data/` → "Success: no issues found in 5 source files", exit 0

**No new dependencies added** (`oandapyV20` was already present; no `pyproject.toml` changes).
**CLAUDE.md trigger-table check:** no new dep, no new CLI command — CLAUDE.md not edited.

**Merge plan:** `gh pr merge 47 --squash --delete-branch` (lead action after reviewer pass)

---

## 1B-T-02 — 2026-05-29 (feat/p1b-t-02)

**What was done:**
- Created `data/calendar.py` with:
  - `Impact(str, Enum)` — high / medium / low values.
  - `CalendarEvent` (plain class with `__slots__`) — fields: `currency`, `event_name`,
    `time` (UTC-aware, INV-03 enforced in `__init__`), `impact`, optional `actual/forecast/previous`.
    Raises `ValueError` for naive datetimes.
  - `EconomicCalendar` ABC — `refresh() -> int` and `upcoming_events(currencies, window) -> list[CalendarEvent]`.
  - `FairEconomyCalendar(EconomicCalendar)` — fetches the free FairEconomy/ForexFactory weekly XML
    (`ff_calendar_thisweek.xml` and optionally `ff_calendar_nextweek.xml`) via `httpx` with an explicit
    10 s timeout (httpx default is None — never use the default). Parses with `xml.etree.ElementTree`.
    Persists to a `calendar_events` SQLite table (its own connection; `CREATE TABLE IF NOT EXISTS`).
    Upsert is `INSERT OR REPLACE` keyed on `(currency, event_name, time)`.
- Created `tests/test_calendar.py` — 25 tests, all fixture-based (no live HTTP).
  `httpx.Client.get` never called in tests; `_fetch` is patched via `MagicMock`.

**INV-03 (the sharp edge) — feed TZ → UTC conversion:**
- `FEED_TZ = "America/New_York"` is the single documented assumption. The FF feed publishes
  times in US Eastern (EDT in summer, EST in winter — DST-aware via `zoneinfo.ZoneInfo`).
- Each `<date>` + `<time>` pair is parsed as a naive datetime, given the feed TZ, then
  converted to UTC via `.astimezone(timezone.utc)` before `CalendarEvent.time` is set.
- `All Day` / `Tentative` / empty times fall back to midnight in the feed TZ (then → UTC).
- Fixture test asserts the UTC instant: NFP `8:30am EDT on 2025-06-06` → `2025-06-06T12:30:00Z`.
- BOJ `11:50pm EDT on June 5` → `2025-06-06T03:50:00Z` (crosses UTC midnight — verified).

**Impact mapping (documented in module docstring):**
- `High → Impact.high`, `Medium → Impact.medium`, `Low → Impact.low`, `Holiday → Impact.low`
  (Holiday not skipped; consumers can filter). Unknown strings default to `Impact.low` defensively.

**Patterns established:**
- `_to_rfc3339()` imported from `data.store` (shared RFC 3339 formatter — no duplication).
- `FairEconomyCalendar` carries its own SQLite connection (separate from `Store`) because
  the calendar module is self-contained; callers may use a shared DB path for co-location.
- `httpx.Client` used as a context manager with `timeout=HTTP_TIMEOUT_SECONDS` (10 s).
  `resp.raise_for_status()` called explicitly; `httpx.HTTPStatusError` / `httpx.TimeoutException`
  propagate to callers.

**AC verification results:**
- `pytest tests/test_calendar.py -v` → 25 passed, exit 0
- `pytest -v` (full suite) → 390 passed, exit 0
- `mypy data/` → "Success: no issues found in 5 source files", exit 0

**New dependency:** `httpx>=0.27` added to `pyproject.toml`.
**CLAUDE.md trigger-table:** pyproject.toml dep added → CLAUDE.md Stack updated (YES).

**Merge plan:** `gh pr merge 46 --squash --delete-branch` (lead action after reviewer pass)

---

## P4-T-03 — 2026-05-29 (feat/p4-T-03-equity)

**What was done:**
- Added the `equity_snapshots` table to `data/store.py` — an APPEND-ONLY broker-sourced
  equity time series for the panel's equity curve. Columns: `as_of` (TEXT, UTC RFC-3339 /
  INV-03), `equity` (REAL, broker NAV / INV-16), `day_pl` (REAL, `nav − start_of_day_equity`).
  - `_CREATE_EQUITY_SNAPSHOTS_SQL` — **no PK / no unique constraint** (deliberate): every
    reconcile pass yields a distinct `rowid`, so history is never clobbered. Wired into
    `_create_tables` via `CREATE TABLE IF NOT EXISTS` (additive migration).
  - `_INSERT_EQUITY_SNAPSHOT_SQL` — plain `INSERT` (NOT `INSERT OR REPLACE/IGNORE` — those
    would defeat append-only; contrast with the `account_state` SINGLETON `id=1` row).
  - `write_equity_snapshot(*, as_of: str, equity: float, day_pl: float) -> None` — note the
    signature takes `as_of` as an already-formatted RFC-3339 **str** (per the feature spec),
    unlike `write_account_state` which takes a `datetime` and formats internally.
  - `load_equity_snapshots(*, since: str | None = None) -> list[dict]` — ordered by `as_of`
    ASC; `since` is an INCLUSIVE (`>=`) lower bound, works lexicographically because RFC-3339
    UTC `Z` strings sort chronologically (same trick as `load_watchlist`).
- Added the snapshot append to `execution/reconcile.py`, **STRICTLY AFTER**
  `store.write_account_state(...)`, reusing already-computed `broker.nav` + `day_pl` (no new
  broker call). The `as_of` is the reconcile `now` formatted via `data.store._to_rfc3339` so
  it byte-matches the `account_state.as_of` written from the same `now`.
  - Import of `_to_rfc3339` is **local** (inside the guarded block) — keeps reconcile's
    module-level import discipline intact (`data.store` is otherwise TYPE_CHECKING-only there,
    to avoid an import cycle) and only runs on the snapshot path.
  - Wrapped in a **NON-FATAL guard** (`try/except Exception` → `logger.warning(..., exc_info=True)`):
    a snapshot-write failure never aborts the reconcile. Broker-truth (the kill switch's
    `account_state` row) is already committed before the append is attempted, so a failure
    here cannot delay or interpose before it.

**Behaviour-preserving guarantee (the load-bearing claim):**
- ZERO change to the diff/adopt/close/refresh logic, the `account_state` write, or the
  `ReconcileReport`. **All 18 existing `tests/test_reconciliation.py` tests pass UNCHANGED.**
- Store-edit ownership (D-03): this task OWNS the `equity_snapshots` migration and **lands
  first** on `data/store.py`. P4-T-04 (panel-data-layer) will later add `load_fills` to the
  same file — serialized after, not parallel.

**Tests (`tests/test_equity_snapshots.py`, 12 tests):**
- Store: empty store → `[]`; round-trip; append-only (two writes at SAME `as_of` → two rows);
  ascending order from out-of-order inserts; `since` inclusive filter; `since=None` returns all.
- Reconcile (v20 mocked via `responses`): one snapshot per pass with `equity == broker.nav`
  and `day_pl == nav − start_of_day_equity` (== `ReconcileReport.day_pl` == `account_state`);
  drawdown second-pass (`day_pl == -50`, NOT the lifetime `pl`); two reconciles → two ordered
  points; **call-order spy** proving `account_state` write precedes the snapshot; non-fatal
  guard (snapshot raise → reconcile still returns, account_state still written, WARNING logged,
  no partial row).
- Reuses helpers imported from `tests.test_reconciliation` (`_client`, `_register`,
  `_summary_response`, `_open_trades_response`, `NOW`, `OPEN_TRADES_URL`, `SUMMARY_URL`).

**AC verification results:**
- `./.venv/bin/python -m mypy .` → "Success: no issues found in 82 source files", exit 0
- `./.venv/bin/python -m pytest -q tests/test_equity_snapshots.py` → 12 passed, exit 0
- `./.venv/bin/python -m pytest -q tests/test_reconciliation.py` → 18 passed (UNCHANGED), exit 0
- `./.venv/bin/python -m pytest -q` (full suite) → 984 passed, exit 0

**No new dependencies** (sqlite3 stdlib; `responses` already a dev dep).
**CLAUDE.md trigger-table check:** no new dep, no new CLI command, no new doc/feature —
CLAUDE.md NOT edited.

**Merge plan:** `gh pr merge 112 --squash --delete-branch` (lead action after reviewer pass)
