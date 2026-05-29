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
