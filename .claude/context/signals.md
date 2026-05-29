# Signals context

New area (`signals/` package) — the Phase 2 watchlist pipeline. Owns the
INV-13 `Candidate` wire contract and the INV-10 gate.

## P2-T-01 — 2026-05-29 (feat/p2-t-01)

**What was done:**
- Created `signals/` package (`__init__.py` re-exports `Candidate`, `Ranker`).
- `signals/ranker.py`:
  - **`Candidate`** — flat pydantic v2 model, the FROZEN INV-13 wire contract.
    Fields, in this exact order: `instrument, timeframe, strategy_name,
    direction, entry_ref, stop_distance, target_distance, oos_sharpe_mean,
    quality_score, rank, spread_ok, session_ok, news_flag, generated_at`.
    `direction` is `str` ("LONG"/"SHORT"), `generated_at` is a UTC RFC-3339
    `...Z` `str`. No nested `signal` object — relevant `Signal` fields are
    flattened. Downstream consumers (T-02 portfolio, T-03 charts, T-05
    narration, T-07 cli, Hermes job) build against this — do not rename/retype
    without an INV-13 amendment.
  - **`Ranker(store, calendar, *, strategy_builder=, spread_ok=, session_ok=,
    eval_lookback_bars=)`** with `rank(now: datetime) -> list[Candidate]`.
    Pipeline as 6 pure stages: gate → evaluate → filter (spread/session) →
    news → conflict → rank.
  - **INV-10 gate:** `_gate()` calls `store.load_approved_set()`; empty → return
    `[]` and log "Approved-set is empty …".
  - **Gate join (DRIFT-01):** the approved row keys the dimension as
    `row['granularity']`; `Signal` calls it `timeframe`. SAME dimension. Match is
    `signal.instrument==row['instrument'] AND
    signal.strategy_name==row['strategy_name'] AND
    signal.timeframe==row['granularity']`. Candidate surfaces it as `timeframe`.
  - **Evaluate:** loads recent candles (`load_candles`, generous day-span
    lookback), builds the strategy via `strategy_builder`, runs
    `generate_signals`, takes the most-recent bar's `Signal` (max by
    `generated_at`). FLAT signals → no candidate.
  - **News gate:** for either leg-currency, high-impact within `NEWS_WINDOW_HIGH`
    (4h) → drop; medium within `NEWS_WINDOW_MEDIUM` (1h) → `news_flag=True`;
    else False. Uses `calendar.upcoming_events(currencies, window)` +
    `data.calendar.Impact`.
  - **Conflict (D-P2-1):** group by `(instrument, timeframe)`; if both LONG and
    SHORT present in a group, suppress ALL members. Cross-timeframe independent.
  - **Rank:** sort by `oos_sharpe_mean` desc, then `quality_score` desc, then
    `(instrument, strategy_name)` asc as a stable final tie-break. 1-based `rank`.
  - **INV-01:** no `execution`/`risk`/`orders` import anywhere — candidates only.
  - **INV-03:** `rank(now)` rejects naive `now`; `generated_at` formatted UTC `Z`.
- `tests/test_ranker.py` — 23 tests, all mocked (NO live HTTP): empty-set→[];
  naive-now reject; only-approved-emit; gate-join-uses-granularity; FLAT→none;
  empty-candles skip; news high-drop/medium-flag/low-none; spread+session fail
  drop; conflict suppress-both / same-dir keep / cross-tf independent; rank
  primary+tiebreak+stable-final; and the **INV-13 serialisation round-trip**
  (field names + order + types + flat-shape + JSON round-trip).

**Key patterns / gotchas:**
- **Spread/session are INJECTED hooks** (`spread_ok`, `session_ok`), defaulting
  to permissive (`True`) because no live spread feed / session schedule is wired
  in this task. The spec leans on `InstrumentMeta.typical_spread × k`; a later
  task can inject the real check without touching the pipeline. Default does NOT
  fabricate a filter result.
- **`strategy_builder` is injected** (`(strategy_name, instrument, timeframe) ->
  Strategy`). Default `_default_builder` mirrors the runner's registry, routing
  on the `strategy_name` prefix (`macrossover`/`donchian`/`bollinger`/`rsi`/
  `roc`/`session`) and building with documented default params. The approved-set
  stores the strategy's full `name` (param-encoded), so the exact instance can't
  be rebuilt from the registry key alone — the prefix-routed default is the
  pragmatic reuse; T-07 may pass a richer builder if it wants exact params.
- `Candidate.model_fields.keys()` order IS the contract — the round-trip test
  asserts the exact list. Adding/reordering a field is a visible test failure.
- `data.calendar.Impact` is imported lazily inside `_news_gate` so the module
  stays importable in mock-only tests; the `Impact` enum identity check
  (`is Impact.high`) is what the news gate keys on.

**Packaging note (BLOCKER, flagged to lead):**
- `signals/` is NOT in `pyproject.toml`'s `[tool.setuptools.packages.find]
  include` list. I attempted to add `"signals*"` but the edit was DENIED by the
  task constraint "do NOT touch pyproject". Empirically `import signals` works
  under `pip install -e '.[dev]'` + pytest/mypy from the project root (cwd
  resolution + the editable finder pick it up), so tests + mypy pass. BUT a
  consumer importing `signals` from outside the repo root (e.g. an installed
  wheel, or a different cwd) would NOT find it. **The coordinator should add
  `"signals*"` to the include list** (same serialized-edit pattern as the
  matplotlib dep) before/with T-07 wires `signals` into `cli.py`. This is a
  packaging declaration, not a dependency.

**AC verification results:**
- `python -m pytest tests/test_ranker.py -q` → **23 passed**, exit 0
- `python -m pytest -q` (full suite) → **446 passed, 85 warnings**, exit 0
  (warnings pre-existing, from backtest/metrics tests — not new)
- `python -m mypy signals/` → **"Success: no issues found in 2 source files"**, exit 0

**No new runtime dependencies** (pydantic + pandas + stdlib, all already in
pyproject). `pyproject.toml` NOT modified (constraint + edit denied — see
packaging note above).

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after
reviewer pass). Hold the Phase 2 fan-out (T-02/03/05/07) until this PR's INV-13
round-trip test is green on main.

## P2-T-02 — 2026-05-29 (feat/p2-t-02)

**What was done:**
- Created `signals/portfolio.py`:
  - **`PortfolioLimiterConfig`** — pydantic v2 model with `correlation_threshold`
    (default 0.7), `max_per_currency` (default 2), `max_concurrent` (default 5),
    `lookback_days` (default 90).
  - **`PortfolioLimiter(store, config)`** with `apply(candidates: list[Candidate])
    -> list[Candidate]`: greedy highest-score-first admission enforcing three caps.
  - Admission sort key is identical to the ranker: `oos_sharpe_mean` desc →
    `quality_score` desc → `(instrument, strategy_name)` asc (stable tie-break).
    Re-sorts defensively regardless of input order.
  - **Correlation gate:** for each candidate vs every already-admitted instrument,
    loads daily ("D") candles via `store.load_candles`, computes mid returns
    `(close_bid + close_ask)/2`, then Pearson ρ on the shared timestamp index.
    If `|ρ| > correlation_threshold` → drop (lower-scored always loses, because
    greedy processes highest first). If `< MIN_CORRELATION_OBS=20` observations
    available → skip check (conservative: not dropped on insufficient data).
  - **Per-currency cap:** splits instrument on `_` (e.g. `EUR_USD` → `EUR`,
    `USD`); tracks a `currency_counts` dict; drops if any leg currency has already
    reached `max_per_currency`.
  - **Max-concurrent cap:** drops if `len(admitted) >= max_concurrent` (checked
    before currency/correlation so the message is accurate).
  - **Each drop logged** at INFO with `DROP <instrument> (...): <limit> reason`.
  - **INV-01:** no `execution`/`risk` import — filtering only.
  - **INV-03:** candles returned by `load_candles` are `datetime64[ns, UTC]`;
    the method signature requires UTC-aware `start`/`end` (passed in from
    `datetime.now(timezone.utc) - timedelta(days=lookback_days)`).
  - `_StoreLike` Protocol (structural) — `data.store.Store` satisfies without
    modification; anything with `load_candles` works.
  - `_pearson_corr(a, b)` returns `float | None` — `None` = skip drop.
  - `_split_currencies(instrument)` splits on `_`; defensive against malformed.
- `tests/test_portfolio.py` — 30 tests, all mocked (NO live HTTP):
  - `TestSplitCurrencies` (4): standard pair, triple, malformed, empty.
  - `TestPearsonCorr` (5): empty, insufficient obs, high-positive, no overlap,
    float in range.
  - `TestEmptyInput` (1): empty→empty.
  - `TestMaxConcurrent` (3): bounds output, max=1 keeps highest, exact=N admitted.
  - `TestMaxPerCurrency` (3): enforced, 1 drops second, non-shared all pass.
  - `TestCorrelation` (5): correlated pair → higher admitted; greedy re-sort
    means submitted order doesn't matter; uncorrelated → both; missing candles →
    both; below threshold → both.
  - `TestGreedyOrder` (3): highest-score first deterministic; stable tie-break
    same regardless of input order; output preserves score order.
  - `TestCombinedLimits` (2): all three limits active simultaneously;
    INV-01 module attribute check.
  - `TestLogging` (2): DROP logged for currency cap; DROP logged for concurrent.
  - `TestConfigDefaults` (2): defaults match constants; custom config round-trips.

**Key patterns / gotchas:**
- The correlation check is **skipped** (not failing-safe-drop) when candle data
  is unavailable or insufficient. Conservative: you cannot prove correlation =
  you cannot drop on correlation grounds.
- `_pearson_corr` aligns on shared timestamps (`pd.concat(..., sort=True).dropna()`)
  — time-index alignment is critical for cross-instrument returns.
- Correlation is computed from **daily** candles (`granularity="D"`) regardless
  of the candidate's trading timeframe. This gives a stable estimate; sub-daily
  alignment would require the instruments' bars to be exactly co-timed.
- The `_StoreLike` Protocol does NOT include `load_approved_set` — only
  `load_candles` is needed, keeping the mock surface minimal.
- `PortfolioLimiter` does not re-rank output (returns admitted candidates in
  the greedy admission order, which is also score order).

**AC verification results:**
- `python -m pytest tests/test_portfolio.py -q` → **30 passed**, exit 0
- `python -m pytest -q` (full suite) → **526 passed, 85 warnings**, exit 0
  (warnings all pre-existing — not new)
- `python -m mypy signals/` → **"Success: no issues found in 3 source files"**, exit 0

**No new runtime dependencies** (pydantic + pandas + numpy already in pyproject;
`numpy` used in test helpers only). `pyproject.toml` NOT modified.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after
reviewer pass). Expect rebase if T-03/T-05 land first (distinct files —
no conflict risk on `signals/portfolio.py`).

**CLAUDE.md trigger-table check:** NOT edited (no new dep, no new CLI command).

## P2-T-03 — 2026-05-29 (feat/p2-t-03)

**What was done:**
- Created `signals/charts.py` — `render_candidate_chart(candidate: Candidate,
  candles: pd.DataFrame, out_dir: str) -> str`.
- `matplotlib.use("Agg")` forced at module import (before pyplot) — headless,
  safe under cron/Hermes with no display server (AC-6).
- OHLC candlesticks drawn with matplotlib primitives (`vlines` for body,
  `plot` for wick) — no extra deps (no mplfinance).
- Three `axhline` overlays: entry (blue dashed), stop (red dotted), target
  (green dotted). Sign per `candidate.direction`:
  - LONG: stop = `entry_ref - stop_distance`, target = `entry_ref + target_distance`
  - SHORT: stop = `entry_ref + stop_distance`, target = `entry_ref - target_distance`
- Signal marker (`^`/`v`) at the `generated_at` bar — exact match first, then
  nearest within ±2 bar spacings; returns `None` (no marker) if outside window.
- UTC x-axis: `mdates.DateFormatter` with `tz=timezone.utc` (INV-03).
- Deterministic path: `{out_dir}/{instrument}_{timeframe}_{generated_at_safe}.png`
  (colons stripped for filesystem safety). Re-render overwrites.
- `plt.close(fig)` in `finally` block — no figure leak across a batch.
- Reads flat `Candidate` fields only — no `.signal.*` nesting (INV-13).
- `tests/test_charts.py` — 23 tests covering all 7 AC + extras (out_dir
  creation, large-candle trimming, different paths for different timestamps).

**Key patterns / gotchas:**
- `matplotlib.use("Agg")` must precede `import matplotlib.pyplot` — order
  enforced by module-level placement with `# noqa: E402` on the subsequent imports.
- `mdates.DateFormatter` is untyped in matplotlib stubs → `# type: ignore[no-untyped-call]`.
- Candlestick body drawn with `vlines` (not `Rectangle`) — avoids needing to
  compute axis-unit bar width; works at any zoom level.
- `_find_signal_bar_index`: nearest-bar fallback uses the first-bar-spacing
  heuristic (works for uniform timeframes like H1/H4; may annotate a slightly
  off bar for irregular data — cosmetic only).
- `DEFAULT_CANDLE_WINDOW = 100` bars; `MIN_CANDLES = 1` (single-bar renders
  cleanly, as tested in AC-7 boundary test).

**AC verification results:**
- `python -m pytest tests/test_charts.py -v` → **23 passed**, exit 0
- `python -m pytest -q` (full suite) → **519 passed, 85 warnings**, exit 0
  (warnings pre-existing, from backtest/metrics tests — not new)
- `python -m mypy signals/` → **"Success: no issues found in 3 source files"**, exit 0

**No new runtime dependencies** (`matplotlib>=3.7` already added by coordinator
in the P2 coordinator branch; `pyproject.toml` NOT modified here).

**Packaging note (inherited from T-01):** `signals/` + `signals*` are now in
`pyproject.toml` include list (coordinator branch, commit e2a0803) — resolved.

**Merge plan:** `gh pr merge 63 --squash --delete-branch` (lead action after
reviewer pass).

## P3-T-02 — 2026-05-29 (feat/p3-T-02-correlation)

**What was done:**
- Created `signals/correlation.py` — shared Pearson correlation primitive extracted
  from `signals/portfolio.py` (behaviour-preserving refactor, no logic change).
  Exports:
  - `MIN_CORRELATION_OBS: int = 20` (shared constant)
  - `split_currencies(instrument: str) -> list[str]` (public name)
  - `_split_currencies` (alias = same object, backward-compat)
  - `mid_returns(df: pd.DataFrame) -> pd.Series` (public name)
  - `_mid_returns` (alias)
  - `pearson_corr(a: pd.Series, b: pd.Series) -> float | None` (public name)
  - `_pearson_corr` (alias)
- Edited `signals/portfolio.py` to remove the three function definitions and
  `MIN_CORRELATION_OBS` declaration; replaced with explicit re-exports:
  `from signals.correlation import X as X` (the `as X` form satisfies mypy's
  "explicit re-export" requirement so `from signals.portfolio import _pearson_corr`
  etc. remain valid for existing callers without mypy errors).
- Created `tests/test_correlation.py` — 21 focused tests directly on
  `signals.correlation`: `split_currencies` (5), `mid_returns` (7 including NaN
  regression guard), `pearson_corr` (8), `MIN_CORRELATION_OBS` sanity (1).

**Key patterns / gotchas:**
- The `as X` re-export idiom (`from signals.correlation import _pearson_corr as
  _pearson_corr`) is required by mypy in strict mode to distinguish an intentional
  public re-export from an accidental private import. Without it mypy raises
  `[attr-defined]` for any downstream `from signals.portfolio import _pearson_corr`.
- Public names (`pearson_corr`, `mid_returns`, `split_currencies`) are preferred for
  Phase 3 `risk/limits.py` use; the underscore aliases exist only for backward
  compatibility.
- `signals/portfolio.py` internal behaviour is byte-identical — `_pearson_corr` and
  `_split_currencies` are still called directly inside `PortfolioLimiter.apply()`
  and `_load_returns`; the source of those names just moved one level up the import
  chain.

**AC verification results:**
- `python -m pytest tests/test_portfolio.py -q` → **35 passed** (unchanged — test
  file NOT modified), exit 0
- `python -m pytest tests/test_correlation.py -q` → **21 passed**, exit 0
- `python -m pytest -q` (full suite) → **772 passed, 87 warnings**, exit 0
- `python -m mypy .` (whole repo) → **0 errors, 68 source files**, exit 0

**No new runtime dependencies** (pandas + stdlib only). `pyproject.toml` NOT
modified. `CLAUDE.md` trigger-table check: NOT edited (no new dep, no new CLI).

**Merge plan:** `gh pr merge 91 --squash --delete-branch` (lead action after
reviewer pass).

## P4-T-01 — 2026-05-29 (feat/p4-T-01-runscan)

**What was done:**
- Created `signals/scan.py` — `run_scan(*, db_path, instruments, timeframes,
  history_years, dry_run) -> list[Candidate]`. This is the **order-free** scan
  entrypoint that the admin panel (and any always-on surface) must use instead
  of `cli.cmd_scan`.
  - Imports ONLY `signals.ranker`, `signals.portfolio`, `data.*`, `config.settings`
    (lazily, live-path only). NEVER imports `execution.orders`, `execution.models`,
    `risk.sizing`, `risk.limits`, or `cli`.
  - `Candidate` is imported at module top-level from `signals.ranker` (order-free;
    no circular import). The function return type is `list[Candidate]`.
  - `_build_date_range`, `_discover_instruments`, `_fetch_candles_for_instruments`
    are private helpers (duplicated from cli.py's scan-specific subset — cannot
    import from cli.py because that module carries the execution imports at module
    level). `_discover_instruments` is cache-only (reads SQLite, never OANDA API)
    — the live API path is in `cli._discover_universe` only.
  - `run_scan` propagates exceptions to the caller (unlike the CLI which returns
    exit codes). `cli.cmd_scan` catches them and returns 1.
- Refactored `cli.cmd_scan` to a **thin argparse adapter**: maps `args.*` →
  `run_scan(**kwargs)`, then does the stdout JSON printing + exit-code
  conversion. The scan logic (candle refresh → Ranker → PortfolioLimiter →
  persist) now lives entirely in `signals/scan.py`.
- Created `tests/test_scan.py` — 7 tests:
  - `TestRunScan` (5): returns candidates + persists watchlist; empty approved-set
    → []; multiple candidates in order; ranker exception propagates; INV-13 fields
    present in return value.
  - `TestTransitiveImportBoundary` (2):
    - **Subprocess test** (load-bearing INV-01 AC): imports `signals.scan` in a
      clean subprocess, walks `sys.modules`, asserts `execution.orders`,
      `execution.models`, `risk.sizing`, `risk.limits`, `cli` are all absent.
    - **In-process importability test**: confirms no top-level import errors.

**Key patterns / gotchas:**
- The subprocess boundary test is the critical one — an in-process patch is not
  sufficient since other tests may have already loaded execution modules. The
  subprocess starts with a clean `sys.modules`.
- `_discover_instruments` is intentionally cache-only. The live OANDA API call for
  universe discovery (when `instruments="ALL"` in non-dry-run mode) is still in
  `cli._discover_universe` and is invoked via the `_fetch_candles_for_instruments`
  path; for `run_scan`, live discovery uses the cached instruments table. This is
  correct for the panel's use case (same instruments that were previously fetched).
- Existing `test_cli_commands.py` scan tests still patch at canonical source paths
  (`signals.ranker.Ranker`, `signals.portfolio.PortfolioLimiter`,
  `data.calendar.FairEconomyCalendar`) — these patches work correctly because
  `run_scan` imports those modules lazily with the same canonical paths.

**AC verification results:**
- `python -m pytest tests/test_scan.py -v` → **7 passed**, exit 0
- `python -m pytest tests/test_cli_commands.py -v` → **17 passed** (unchanged), exit 0
- `python -m pytest -q` (full suite) → **962 passed, 87 warnings**, exit 0
- `python -m mypy .` (whole repo) → **0 errors, 81 source files**, exit 0
- `fathom scan --dry-run --db-path data/fathom.db` → exits 0, prints `Candidate[]`
  JSON with INV-13 shape (same output as before the refactor)

**No new runtime dependencies** (`signals.ranker.Candidate` already in scope).
`pyproject.toml` NOT modified. `CLAUDE.md` trigger-table check: NOT edited (no new
dep, no new CLI command — `fathom scan` is unchanged).

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after
reviewer pass). T-05 (admin-panel) depends on this — it must be on main before T-05
dispatches.

## P3-T-04 — 2026-05-29 (feat/p3-T-04-limits)

**What was done:**
- Created `risk/limits.py` — the pure, deterministic **book-level admission gate +
  daily-loss kill switch** (INV-05 backstop, safety-critical). Like `risk/sizing.py`
  it can only reject, never green-light. Builds the *bucket-grouping* shape on the
  shared `signals/correlation.py` primitive (DRIFT-09), distinct from portfolio's
  per-currency cap.
- `check_limits(order, *, open_positions, day_pl, equity, start_of_day_equity,
  config, now, order_risk, returns=None) -> LimitDecision`. Four checks,
  most-global-first (first breach wins, short-circuits the rest):
  1. **Kill switch** — `day_pl <= -(daily_loss_cap × start_of_day_equity)` →
     `kill_switch_active=True`, reject all until next 00:00 UTC (computed from
     injected `now`, INV-03). `day_pl`/`start_of_day_equity` come from the
     `account_state` row (DRIFT-02). Non-positive/non-finite SOD-equity is treated
     as *tripped* (fail-safe — never green-light against an untrustworthy baseline).
  2. **Max concurrent** — `len(open_positions) >= max_concurrent`.
  3. **Book risk** — `current_book_risk + order_risk > max_book_risk × equity`.
     `current_book_risk = Σ position_risk(p)` where
     `position_risk = |units| × |entry_price − stop_loss_price|` (stop-distance, NOT
     notional). `order_risk` is the injected `SizingResult.risk_amount` — limits does
     NOT re-derive it (avoids drift from sizing's own maths).
  4. **Correlation bucket** — connected-component grouping over
     `|pearson_corr(ra, rb)| > correlation_threshold`; bucket size >
     `max_per_correlation_group` rejects. `pearson_corr` returning `None`
     (insufficient/empty data) creates NO edge (conservative — missing data never
     forces a grouping, mirrors PortfolioLimiter).
- `LimitsConfig` (pydantic v2) with APPROVED defaults (D-P3-A/B): `daily_loss_cap=0.01`,
  `max_concurrent=5`, `max_book_risk=0.01`, `max_per_correlation_group=2`,
  `correlation_threshold=0.7`. `LimitDecision(allowed, reason, kill_switch_active)`.
  Read-only `kill_switch_status(...) -> KillSwitchStatus(active, day_pl, cap_amount,
  reset_at)` — side-effect-free, shares no state with `check_limits`.
- Exported the new names via `risk/__init__.py` (alongside `sizing`).

**Key patterns / gotchas:**
- **Purity by injection.** Limits is pure: no store/network/clock. The correlation
  source is injected as `returns: Mapping[str, pd.Series]` (the shape
  `signals.correlation.mid_returns` produces) — the CLI/orchestrator loads candles
  and passes the return map in. This is the bridge between the store-bound
  `PortfolioLimiter` and the pure book gate. `order_risk` is likewise injected.
- **Bucketing uses the absolute |ρ|** — a strongly *anti*-correlated pair shares
  exposure too (a hedge is still one concentrated bet for this cap).
- Touched ONLY `risk/limits.py`, `risk/__init__.py`, `tests/test_limits.py`. Did NOT
  edit `sizing.py`, `execution/`, `signals/`, `data/`, `cli.py`.

**AC verification results:**
- `./.venv/bin/python -m pytest tests/test_limits.py -q` → **27 passed**, exit 0
- `./.venv/bin/python -m pytest -q` (full suite) → **799 passed, 87 warnings**, exit 0
  (was 772 before + the new 27)
- `./.venv/bin/python -m mypy .` (whole repo) → **0 errors, 70 source files**, exit 0

**No new runtime dependencies** (pandas + pydantic + stdlib only). `pyproject.toml`
NOT modified. `CLAUDE.md` trigger-table check: NOT edited (no new dep, no new CLI,
no new doc/invariant).

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after
reviewer pass).
