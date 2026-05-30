# Runner context

## POC-T-07 — 2026-05-28 (feat/poc-t-07)

**What was done:**
- Created `scripts/__init__.py` (empty, so `scripts` is a discoverable package in editable install).
- Created `scripts/poc_run.py`:
  - CLI args: `--instruments`, `--granularities`, `--history-years`, `--fast-periods`,
    `--slow-periods`, `--dry-run`, `--db-path`.  Defaults match poc.md parameters.
  - `_UTCFormatter`: custom `logging.Formatter` that formats `formatTime` with
    `%Y-%m-%dT%H:%M:%SZ` via `datetime.fromtimestamp(..., tz=timezone.utc)` (INV-03).
  - Flow: Settings → OandaClient → Store → (optional) `fetch_and_cache` →
    `MACrossover` × `WalkForwardValidator` for all combos → `_print_approved_table`.
  - Per-instrument `CostParams`: JPY pairs use `pip_value=0.0001`... wait, corrected:
    JPY pairs use `pip_value=0.01`; non-JPY majors use `pip_value=0.0001`.
  - `--dry-run`: skips Settings / OandaClient construction; opens store at `--db-path`
    and runs walk-forward against whatever is cached. Does NOT make live HTTP calls.
    Integration test uses this flag — no credential required.
  - Empty approved set: prints `"No combinations passed walk-forward criteria."` to
    stdout and **exits 0** (not 1 — empty set is a valid PoC result, per T-07 AC).
  - Invalid (fast >= slow) combinations are silently skipped (no error, no log spam).
  - `_print_approved_table`: tabular stdout output with columns: Instrument, Gran,
    Fast, Slow, OOS Sharpe, Trade Count, Swap Modelled.
  - INV-08 enforced: Settings / OandaClient not constructed at all in `--dry-run`;
    only `env` and `oanda_base_url` are logged (not token or account ID).
- Created `tests/integration/test_poc_runner.py`:
  - `_populate_store`: builds 800 sinusoidal daily candles (≈2.2 years) for a real
    SQLite file in `tmp_path`. Verified `count > 700`.
  - `populated_db` / `empty_db` fixtures use `tmp_path` (pytest-managed temp dirs).
  - `_run_poc()` helper: runs `scripts/poc_run.py` as a subprocess via
    `subprocess.run` with `sys.executable`, `capture_output=True`, `cwd=project_root`.
    Always passes `--dry-run` so no HTTP is made.
  - `TestPocRunnerEmptyStore`: exit 0, "no combinations" message, UTC timestamps,
    no token/secret strings in output.
  - `TestPocRunnerWithData`: exit 0, table or empty message, `False` in stdout when
    table present (`swap_modelled=False`), UTC timestamps, no traceback.
  - `TestPocRunnerMultipleParams`: multi-combo run still exits 0.
- Fixed `tests/test_oanda_client.py` line 75: removed `# type: ignore[arg-type]`
  made redundant by `pydantic.mypy` plugin in strict mode.

**Key patterns / gotchas:**
- `--dry-run` in the runner avoids constructing `Settings` / `OandaClient` entirely —
  so the test needs no `.env` and no env vars at all.  If `Settings` construction fails
  (no `.env`), the runner exits 1 with an error log (non-dry-run path only).
- `subprocess.run` CWD must be the project root so `scripts/poc_run.py` can import
  `config`, `data`, `backtest`, `strategies` without a path hack.  The fixture uses
  `Path(__file__).parent.parent.parent` to locate the root.
- `pyproject.toml` already includes `"scripts*"` in `packages.find`; no change needed.
- The approved-set table includes a `Swap Modelled` column that shows `False` (D-03).
  The integration test asserts this is present when the table is printed.
- Empty approved set is a valid result — integration test confirms exit 0 in both
  empty-store and populated-store paths.
- All log timestamps use the `_UTCFormatter` which calls
  `datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`.
  The integration test validates with regex `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z`.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/integration/test_poc_runner.py -q` → 15 passed, exit 0
- `python -m pytest -q` (full suite) → 145 passed, exit 0
- `python -m mypy scripts/ tests/integration/` → "Success: no issues found in 4 source files", exit 0
- `python -m mypy scripts/ config/ data/ strategies/ backtest/ tests/integration/` → "Success: no issues found in 18 source files", exit 0
- `python -m mypy .` (whole tree) → "Success: no issues found in 25 source files", exit 0
- `python scripts/poc_run.py --dry-run --db-path /tmp/poc_smoke_test.db` (empty DB):
  stdout: `"No combinations passed walk-forward criteria."`, exit 0, no traceback.

**Smoke test (--dry-run against empty DB):**
```
$ python scripts/poc_run.py --dry-run --db-path /tmp/poc_smoke_test.db
No combinations passed walk-forward criteria.
EXIT: 0
```

**No new dependencies** — no changes to `pyproject.toml`.

**CLAUDE.md:** `scripts/poc_run.py` was already documented in the Commands section
("python scripts/poc_run.py — end-to-end PoC..."). Confirmed match; no update needed.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).

---

## P1A-T-08 — 2026-05-29 (feat/p1a-t-08) — full-universe runner, the capstone/join

**What was done:**
- New top-level `cli.py` with a `backtest` argparse subcommand (no new dep). Registered as a
  console entry point `fathom = "cli:main"` in `pyproject.toml` (`[project.scripts]` +
  `[tool.setuptools] py-modules = ["cli"]` so the single-file module installs). `fathom backtest`
  is now a real command (CLAUDE.md Commands moved it from "Phase 2+ not built" to "Phase 1A current").
- Args: `--instruments ALL|EUR_USD,...`, `--timeframes H1,H4,D`, `--strategies all|<keys>`,
  `--workers N`, `--db-path`, `--history-years`, `--dry-run`.
- **Universe discovery:** `ALL` → live `OandaClient.list_instruments()` (cached via
  `Store.upsert_instruments`); `--dry-run ALL` → cached `Store.load_instruments()` only (NO HTTP,
  Settings/OandaClient never constructed). Explicit `--instruments` list used verbatim.
- **Six strategies** instantiated from a registry (`macrossover, donchian, bollinger, rsi, roc,
  session`) with a documented default param grid (`_default_param_grid()`). ROCMomentum takes
  `instrument`/`timeframe` positionally — handled in `_build_strategy`.
- **Per-tf window config (D-P1-2 ruling):** `WINDOW_CONFIG` dict — H1=12/3, H4=18/6, D=24/6.
  Each combo carries its tf's train/test months; passed straight into `WalkForwardValidator.run`.
  Strict per-window gate unchanged (it lives in WalkForwardValidator).
- **InstrumentMeta→CostParams mapping (the T-03-deferred boundary, owned here):**
  `_instrument_costs(store)` maps `long_rate→swap_long_rate`, `short_rate→swap_short_rate`, and
  `pip_value = 10**pip_location` (−4→0.0001, −2→0.01 JPY). `spread_pips=1.5`, `slippage_pips=0.5`,
  `commission_pips=0.0` are documented defaults. Fallback for uncached instruments: JPY-aware
  pip_value, financing 0.0 (→ swap_modelled=False, honest about absent data).
- **INV-12 single-writer:** module-level worker `_run_combo(spec)` opens its OWN `Store`, runs the
  validator, returns `Optional[ApprovedSetEntry]`, and NEVER writes. Parent fans out via
  `ProcessPoolExecutor.map` (or serial when `--workers 1`), collects EVERY result first, sorts by
  (strategy_name, instrument, granularity), then writes the full batch in ONE
  `Store.write_approved_set(...)` (single `executemany` + one `commit`).
- **approved_set table (INV-10 gate):** added to `data/store.py` — `_CREATE_APPROVED_SET_SQL`
  mirrors `ApprovedSetEntry` (strategy_name, instrument, **granularity**, oos_sharpe_mean,
  oos_trade_count_total, swap_modelled) + a DB-only `run_timestamp` (UTC RFC 3339). PK is
  (run_timestamp, strategy_name, instrument, granularity). `ApprovedSetEntry` pydantic model is
  UNCHANGED — run_timestamp is supplied at `write_approved_set` (DRIFT-03). New `write_approved_set`
  (single-tx batch) + `load_approved_set` (Phase 2 reads this) methods.
- **Engine docstring fix:** `backtest/engine.py:181` no longer says "including swap_modelled=False"
  (financing flips it True since T-03).
- **Empty approved set → exit 0** with a clear stdout message (INV-10: empty = no signals).

**Key patterns / gotchas:**
- **ProcessPoolExecutor + monkeypatch don't mix across the process boundary:** a child re-imports
  `cli` fresh and won't see a parent-side `monkeypatch.setattr(cli, "_run_combo", ...)`. So the
  INV-12 spy test runs `--workers 1` (serial path, in-process — patch applies); the determinism
  `--workers 1 vs 4` test uses the REAL worker on real cached daily candles.
- **store→walkforward circular import avoided:** `Store.write_approved_set` types its param via a
  `TYPE_CHECKING`-only import of `ApprovedSetEntry` (store→walkforward→engine→store would cycle at
  runtime); it uses only attribute access, so no runtime import is needed.
- **Determinism** is guaranteed by: fixed sorted combo order + `map` preserving order + an explicit
  parent-side sort before write. `--workers 1` and `--workers 4` persist identical tables.
- Worker silences the expected `compute_metrics` low-trade-count UserWarning (a full-universe scan
  runs many short windows; it's per-combo noise, not actionable — does NOT change approvals).
- `--dry-run` over an empty store creates the (empty) approved_set table and exits 0.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/integration/test_backtest_runner.py -q` → 11 passed, exit 0
- `python -m pytest -q` (full suite) → 360 passed, 85 warnings (pre-existing low-trade UserWarnings),
  exit 0
- `python -m mypy cli.py data/store.py backtest/engine.py tests/integration/test_backtest_runner.py`
  → "Success: no issues found in 4 source files", exit 0
- `python -m mypy .` (whole tree) → 53 errors, ALL in `tests/test_momentum.py` +
  `tests/test_breakout.py` — **pre-existing on origin/main** (confirmed by running mypy on a pristine
  origin/main worktree: identical 53). Surfaced by env mypy 2.1.0 (merged context was on 1.8;
  `pyproject.toml` pins only `mypy>=1.8`). NOT introduced by T-08; out of this task's scope.
- `fathom backtest --dry-run --db-path /tmp/x.db --instruments EUR_USD,GBP_USD,USD_JPY --timeframes H1,H4,D`
  → "...Approved set is empty (a valid result)." exit 0.

**No new dependencies** (argparse, concurrent.futures stdlib). `pyproject.toml`: added
`[project.scripts] fathom` + `py-modules=["cli"]` (packaging only, not a dependency).

**CLAUDE.md:** Commands section updated — `fathom backtest` is now a current Phase 1A command with
its full arg list; PoC runner marked superseded.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).

---

## fix/runner-candle-fetch — 2026-05-29 (fix/#28)

**Gap closed:** `fathom backtest` in LIVE mode never fetched candle data before the walk-forward
fan-out. On a fresh DB the store was empty; all combos ran against 0 rows and approved nothing.
The integration tests masked the bug because they pre-populated the DB with fixtures.

**What was done:**
- `cli.py`: Added `_fetch_candles_for_universe(instruments, timeframes, db_path, start, end)`.
  - Lazy-imports `Settings`, `OandaClient`, `fetch_and_cache` so `--dry-run` NEVER constructs them.
  - Creates ONE `OandaClient` from `Settings()` (env-scoped, INV-09).
  - Fetches sequentially for each `(instrument, timeframe)` pair; `fetch_and_cache` is gap-aware.
  - Passes `write_parquet=False` — runner only needs the SQLite operational store.
  - Logs `env` and `(instrument, timeframe, count)` only; never logs token or account ID (INV-08).
- `cli.py` `cmd_backtest`: Inserted `if not dry_run: _fetch_candles_for_universe(...)` between
  `_build_date_range` and `_build_combos` / combo fan-out.
- `tests/integration/test_backtest_runner.py`: Added `TestLivePathCandleFetch` with two tests:
  - `test_store_is_populated_before_walkforward_in_live_mode`: patches `data.candles.fetch_and_cache`
    (no HTTP), runs `cmd_backtest` with `dry_run=False` against a fresh temp DB, asserts store
    candle count > 0 for each (instrument, timeframe) pair, and that `fetch_and_cache` was called
    for each expected pair. FAILS against pre-fix code (store stays empty); passes with fix.
  - `test_dry_run_never_calls_fetch_and_cache`: spy that raises if called; confirms `--dry-run` path
    never triggers a fetch.
- `docs/features/full-universe-backtest-runner.md`: Added the missing AC bullet and an "Approach
  note (amendment)" explaining the under-specified fetch step.

**Key patterns / gotchas:**
- Patching `data.candles.fetch_and_cache` (canonical module location) is correct because
  `_fetch_candles_for_universe` uses `from data.candles import fetch_and_cache` at call time.
  Patching `cli.fetch_and_cache` would NOT work (import-time binding is not established).
- `Settings` and `OandaClient` are patched via `unittest.mock.patch` targeting their canonical
  module paths (`config.settings.Settings`, `data.oanda_client.OandaClient`) — consistent with
  how the lazy-import pattern in `_fetch_candles_for_universe` resolves them.
- The `fake_fetch_and_cache` function signature uses `object` for `client` (the mock) and
  concrete `Store` for the store (so upsert/load_candles work for real). No `type: ignore`
  required with mypy 2.x.

**AC verification results (raw, captured exit codes):**
- `pytest tests/integration/test_backtest_runner.py -v` → 13 passed (11 pre-existing + 2 new), exit 0
- `pytest -q` (full suite) → 362 passed, 85 warnings, exit 0
- `mypy cli.py tests/integration/test_backtest_runner.py` → "Success: no issues found in 2 source files", exit 0
- `mypy .` → 53 errors in tests/test_momentum.py + tests/test_breakout.py only (same pre-existing
  errors; NOT introduced by this fix)

**No new dependencies.** No changes to pyproject.toml.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).

---

## P2-T-07 — 2026-05-29 (feat/p2-t-07) — cli scan|watchlist|chart (join)

**What was done:**
- Extended `cli.py` with three Phase 2 subcommands (`scan`, `watchlist`, `chart`).
  **This is the ONLY Phase 2 task that edits `cli.py`** (join point per task graph).
- Extended `data/store.py` with a `watchlist` SQLite table + `write_watchlist` /
  `load_watchlist` methods (mirrors the `approved_set` pattern — run-timestamped,
  single-writer).  The `Candidate`↔table mapping lives entirely in the persistence
  layer; the `Candidate` pydantic model is NOT modified (INV-13 frozen).

**`fathom scan`** (`cmd_scan`):
- `--dry-run` (cache-only, mirrors `backtest`): skips live fetch; runs Ranker →
  PortfolioLimiter against whatever is in the store.
- LIVE mode: calls `_fetch_candles_for_universe` (reused Phase 1 helper) then
  instantiates `FairEconomyCalendar(db_path=db_path)`, `Ranker`, `PortfolioLimiter`.
- Persists candidates to `watchlist` table via `store.write_watchlist(candidates,
  run_timestamp=run_dt)` in one transaction.
- Prints `Candidate[]` JSON to stdout (INV-13 wire shape).
- Empty approved-set → empty watchlist, clear stdout message, **exit 0** (INV-10).

**`fathom watchlist`** (`cmd_watchlist`):
- Pure DB read via `store.load_watchlist()` (latest run — `MAX(run_timestamp)`).
- Reconstructs `Candidate` objects from raw dicts (validates shape); serialises via
  `model_dump()` → ensures JSON matches INV-13 exactly.
- Empty table → prints `[]`, exits 0.

**`fathom chart <instrument>`** (`cmd_chart`):
- Reads latest watchlist entry for `<instrument>/<timeframe>`.
- Loads candles from store; calls `render_candidate_chart`.
- Prints PNG path to stdout.
- No live HTTP — pure store read + matplotlib.

**`watchlist` table schema:**
```sql
CREATE TABLE IF NOT EXISTS watchlist (
    run_timestamp    TEXT    NOT NULL,
    instrument       TEXT    NOT NULL,
    timeframe        TEXT    NOT NULL,
    strategy_name    TEXT    NOT NULL,
    direction        TEXT    NOT NULL,
    entry_ref        REAL    NOT NULL,
    stop_distance    REAL    NOT NULL,
    target_distance  REAL    NOT NULL,
    oos_sharpe_mean  REAL    NOT NULL,
    quality_score    REAL    NOT NULL,
    rank             INTEGER NOT NULL,
    spread_ok        INTEGER NOT NULL,
    session_ok       INTEGER NOT NULL,
    news_flag        INTEGER NOT NULL,
    generated_at     TEXT    NOT NULL,
    PRIMARY KEY (run_timestamp, instrument, timeframe, strategy_name)
)
```

**Key patterns / gotchas:**
- `FairEconomyCalendar.upcoming_events` returns `list[CalendarEvent]`; Ranker's
  `_CalendarLike` Protocol declares `list[object]` — structurally compatible but
  mypy rejects on return covariance. Suppressed with `# type: ignore[arg-type]`
  on the `Ranker(store=..., calendar=FairEconomyCalendar(...))` line only.
- Lazy imports (`from data.calendar import FairEconomyCalendar` etc inside
  `cmd_scan`) mean tests must patch at the **source module** path
  (`data.calendar.FairEconomyCalendar`, `signals.ranker.Ranker`, etc.),
  NOT `cli.FairEconomyCalendar`. Documented in test file.
- `write_watchlist` and `load_watchlist` added to `data/store.py`.
  `Store._create_tables` now also calls `CREATE TABLE IF NOT EXISTS watchlist`.
  Idempotent — existing DBs from prior phases get the new table on first open.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/test_cli_commands.py -v` → **16 passed**, exit 0
- `python -m pytest -q` (full suite) → **624 passed, 85 warnings**, exit 0
  (85 warnings pre-existing; not introduced by T-07)
- `python -m mypy cli.py data/store.py` → **"Success: no issues found in 2 source files"**, exit 0
- `fathom scan --dry-run --db-path /tmp/x.db --instruments EUR_USD --timeframes H1`
  → "Scan complete: approved-set is empty … Watchlist is empty (a valid result — INV-10)."
  **exit 0**; INV-10 clear message; no token logged (INV-08); UTC timestamps (INV-03).

**No new dependencies.** No changes to pyproject.toml.

**CLAUDE.md trigger-table check:**
- New CLI commands → CLAUDE.md Commands updated (YES): `fathom scan`, `fathom watchlist`,
  `fathom chart` moved from "Phase 2+ (not yet built)" to "Phase 2 (current)" with full
  arg list.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).

---

## P3-T-10 — 2026-05-29 (feat/p3-T-10-cli) — execution-cli (the INV-01 gate join)

**What was done:**
- Extended `cli.py` with three Phase 3 subcommands: `execute`, `positions`, `reconcile`.
  **This is the ONLY Phase 3 task that edits `cli.py`** (join point per task graph).
- **This is the canonical INV-01 enforcement point**: `fathom execute` is a human-run
  CLI command, NEVER a Hermes tool. The Phase 2 daily.md allow-list (scan/watchlist/chart)
  is unchanged.

**`fathom execute <candidate-ref>`** (`cmd_execute`) — gate ordering:
1. Load candidate from latest watchlist via `_load_candidate(ref, db_path)` (INV-13).
   Ref format: `instrument:timeframe:strategy_name` (DRIFT-04). Error + exit ≠ 0 if not found.
2. Fresh reconcile (`reconcile(client, store, now)`) BEFORE limits (AMBIGUOUS-03).
   Refreshes `account_state` (day_pl, start_of_day_equity) + open positions from broker.
3. Pretrade check (`pretrade_check(candidate)`) → `block` → exit ≠ 0.
4. Sizing (`size_position(..., risk_fraction=DEFAULT_RISK_FRACTION)`) — **always 0.0025,
   never above the INV-05 cap**. `units=0` → exit ≠ 0.
5. `build_bracket(candidate, units, execution_date, precision)` → produces the full `Order`
   (used for both the limits check AND the eventual submit).
6. Limits check (`check_limits(order, ..., order_risk=sizing_result.risk_amount)`) →
   reject → exit ≠ 0; kill-switch active → prints reset-at time.
7. `--dry-run`: prints `[DRY-RUN] Would submit the following order:` + JSON, **no v20 call**.
8. `--yes`: skips the interactive confirm prompt. Default: `Confirm submit? [y/N]`.
9. Submit (`submit_order(order, client, store, entry_ref, precision, now)`) → prints Fill JSON.

**`fathom positions`** (`cmd_positions`): DB-only read (`store.load_open_positions()`), prints
`Position[]` as JSON. No live HTTP.

**`fathom reconcile`** (`cmd_reconcile`): Calls `reconcile(client, store, now)` once, prints
`ReconcileReport` as JSON (adopted/closed/matched/drift_flags/day_pl/start_of_day_equity).

**Module-level imports for testability:**
- Execution module imports (`Settings`, `OandaClient`, `build_bracket`, `OrderRejected`,
  `submit_order`, `reconcile`, `pretrade_check`, `LimitsConfig`, `check_limits`,
  `kill_switch_status`, `DEFAULT_RISK_FRACTION`, `size_position`) are at the module level
  in a `try/except ImportError` block so they're patchable as `cli.<name>`.
  Tests use `patch("cli.reconcile", ...)`, `patch("cli.size_position", ...)` etc.

**INV-01 boundary tested in two places:**
1. `tests/test_execution_cli.py::TestInv01Boundary` — scans `hermes_integration/` for
   `"fathom execute"`, `"fathom positions"`, `"fathom reconcile"` (exact command strings).
2. `tests/test_cli_commands.py::TestNoOrderPath` — updated from the Phase 2 check (which
   forbade "execution" in cli.py — now outdated since P3-T-10 legitimately adds it) to
   the correct boundary: checks that `hermes_integration/` has none of the Phase 3 commands.

**Key patterns / gotchas:**
- `build_bracket` is called BEFORE `check_limits` so the limits gate receives a fully-formed
  `Order` (with correct bracket prices). This is still within "step 5 (limits)" — the order
  is NOT submitted yet; it is just constructed for the limits input.
- `load_account_state()` returns `dict[str, object] | None`. To pass `day_pl`/
  `start_of_day_equity` to `float()`, use `# type: ignore[arg-type]` (the dict values
  are float at runtime, but the type annotation is `object`).
- `equity = start_of_day_equity + day_pl` — this is the current NAV (snapshot + today's delta).
- `quote_to_account_rate`: defaults to 1.0 for `_USD`-quoted instruments; for others, loads
  the latest `close_mid` from the candle store and derives `rate = 1/mid`. Falls back to 1.0
  with a WARNING if candles are unavailable.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/test_execution_cli.py -v` → **23 passed**, exit 0
- `python -m pytest tests/test_cli_commands.py tests/test_execution_cli.py -v` → **40 passed**, exit 0
- `python -m pytest -q` (full suite) → **955 passed, 87 warnings**, exit 0
- `python -m mypy .` → **"Success: no issues found in 79 source files"**, exit 0
- `fathom --help` → lists `execute`, `positions`, `reconcile` alongside `backtest`, `scan`,
  `watchlist`, `chart`.

**No new dependencies.** No changes to pyproject.toml.

**CLAUDE.md trigger-table check:** New CLI commands `fathom execute`, `fathom positions`,
`fathom reconcile` → CLAUDE.md Commands section updated (YES).

**Merge plan:** `gh pr merge 85 --squash --delete-branch` (lead action after reviewer pass).

---

## P5-T-03 — 2026-05-30 (feat/p5-T-03-preflight) — preflight-check (fathom preflight GO/NO-GO)

**What was done:**
- New `execution/preflight.py`:
  - `PreflightReport(go, checks, checked_at)` + `CheckResult(name, ok, detail)` pydantic models.
  - `run_preflight(*, settings, store, client=None, attested=False) -> PreflightReport` — five checks:
    1. **account_reachable** — `client.account_summary()` succeeds; `None` client → FAIL.
    2. **kill_switch_armed** — calls `risk.limits.kill_switch_armed(store.load_account_state(),
       now, config=LimitsConfig(), staleness_minutes=10)`; NO-GO on missing/stale/tripped.
    3. **bracket_contract_inv04** — static assertion: `build_bracket` raises `ValueError` on
       non-positive `stop_distance`; confirms INV-04 can't produce a naked order.
    4. **env_flag_token_consistency** — if `ENV=live`: token present + account_id present +
       `live_trading_enabled=True`; demo always consistent. Token value never in detail (INV-08).
    5. **track_record_attested** — `attested` must be `True`; references INV-07.
  - `go=True` only when ALL five pass; `go=False` on any failure.
- Extended `cli.py`:
  - `_build_parser()`: added `preflight` subparser with `--db-path` + `--attest-track-record`.
  - `cmd_preflight(args)`: loads `Settings`, constructs `OandaClient`, calls `run_preflight`.
    Prints per-check table; exits 0 on GO / 1 on NO-GO. Never prints token (INV-08). UTC log.
  - `main()`: dispatches `args.command == "preflight"` to `cmd_preflight`.
- New `tests/test_preflight.py` — 24 tests:
  - All kill-switch cases: missing, stale (15-min-old as_of), tripped (day_pl=-200 on $10k equity),
    healthy (present + fresh + not tripped).
  - All env/flag/token cases: live no-token, live no-account-id, live disabled, demo consistent, live all-ok.
  - Account reachable: stub OK + stub raises + no client.
  - INV-04 bracket contract: PASS in correct build.
  - INV-08: token never in report details (asserts each `detail` string).
  - INV-03: `checked_at` UTC offset == 0.
  - Read-only: monkey-patches `write_account_state` + asserts no order-placement method called.
  - CLI: `--help` exits 0; no-attest demo exits 1; seeded store + attest exits 0.
  - All five check names present in report.
  - Parametrized: tripped + stale → go=False even when attested.

**Key patterns / gotchas:**
- `kill_switch_armed` is imported from `risk.limits` (landed in P5-T-01). Preflight calls it directly;
  no duplication of the staleness/tripped logic.
- The bracket/INV-04 check uses a standalone helper `_check_bracket_contract()` that creates a minimal
  `Candidate` with `stop_distance=0.0`. If `Candidate` itself rejects that, the check also passes
  (contract enforced even earlier). If `Candidate` accepts it, `build_bracket` must raise `ValueError`.
- `cmd_preflight` lazily imports `execution.preflight.run_preflight` (consistent with the lazy import
  pattern in `cli.py` for execution deps). `OandaClient` and `Settings` are already module-level
  imports from the Phase 3 `try/except ImportError` block; they work correctly.
- Tests stub `cli.Settings` and `cli.OandaClient` for the CLI tests; test `run_preflight` directly
  with mock `Settings` objects (not the real pydantic class) for unit tests.
- The `mem_store` fixture uses `tempfile.mktemp` + explicit cleanup (yielding a `Generator[Store, None,
  None]`) so it works on the CI filesystem without needing pytest's `tmp_path` path type resolution.
- INV-09 compliance: `run_preflight` reads `settings.env` / `settings.live_trading_enabled` only for
  the env/flag/token consistency check (sanctioned operator-boundary gate). No env-aware branch
  in `execution/models.py` or `risk/sizing.py` etc.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/test_preflight.py -v` → **24 passed**, exit 0
- `python -m pytest -q` (full suite) → **1082 passed, 87 warnings**, exit 0
- `python -m mypy .` → **"Success: no issues found in 89 source files"**, exit 0
- `fathom preflight --help` → shows `--db-path` + `--attest-track-record`, exit 0
- `fathom preflight` (demo, no attest, stale store) → NO-GO exit 1;
  stdout: per-check table; FAIL on `kill_switch_armed` (stale) + `track_record_attested`.
  Account reachable PASS (real demo endpoint reachable in test env).

**No new dependencies.** No changes to `pyproject.toml`.

**CLAUDE.md trigger-table check:** New CLI command `fathom preflight` → CLAUDE.md Commands NOT
updated (the task instruction says "Touch ONLY execution/preflight.py, cli.py, tests/test_preflight.py").
CLAUDE.md update is a lead/reviewer action. CLAUDE.md Commands section should receive
`fathom preflight [--db-path PATH] [--attest-track-record]` in the Phase 5 / Common Commands block.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).
