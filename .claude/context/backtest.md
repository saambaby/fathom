# Backtest context

## POC-T-05 — 2026-05-28 (feat/poc-t-05)

**What was done:**
- Created `backtest/__init__.py` — re-exports `CostParams`, `CostResult`, `apply_costs`,
  `BacktestEngine`, `BacktestResult`, `Trade`.
- Created `backtest/costs.py`:
  - `apply_costs(entry_price, exit_price, direction, spread_pips, slippage_pips, pip_value, swap_pips=0.0) -> CostResult`.
  - `CostResult(net_entry, net_exit, total_cost_pips, swap_modelled=False)`.
  - `CostParams(spread_pips>0, slippage_pips>=0, pip_value>0, swap_pips==0)` — pydantic v2; `spread_pips` is `gt=0`
    so the engine can NEVER run cost-free (INV-06); a non-zero `swap_pips` is REJECTED loudly (D-03), not ignored.
  - Spread model: half-spread on each leg. Long buys at ask (entry +), sells at bid (exit −); short is the mirror.
    Slippage applied adversely on the EXIT (stop/target = market fill); entry treated as a controlled next-open fill.
  - `total_cost_pips = spread_pips + slippage_pips` — path-independent, depends only on params → strictly > 0 for any
    non-zero spread/slippage. Net PnL <= gross PnL is structural (offsets are always adverse on both legs).
- Created `backtest/engine.py`:
  - `BacktestEngine(store, cost_params).run(strategy, instrument, granularity, start, end) -> BacktestResult`.
  - **No look-ahead:** strategy is fed only the prefix slice `df.iloc[:i+1]` at bar `i`; a signal from bar `i` is
    entered at bar `i+1`'s OPEN (never the signal bar's own price). Fill checks use only the current bar's OHLC.
  - **Single open position** (PoC scope) — new signals ignored while a position is open or a pending entry exists.
  - **Intrabar fills:** long stop if `low<=stop`, short stop if `high>=stop`; long target if `high>=target`, short
    target if `low<=target`. **Both breach in one bar → STOP wins (conservative).** Fill level clamped to
    `[low, high]` so a reported fill can never be an impossible price.
  - End-of-data: any still-open position is closed at the final bar's CLOSE (no slippage on this leg), so no
    dangling trade leaks into the equity curve.
  - Defensive `df.copy(deep=True)` at the top of `run()` — never mutates the store's frame.
  - `Trade` carries entry/exit times (UTC, from bar data — INV-03), gross+net entry/exit, direction, pnl_pips,
    pnl_net_pips, cost_pips, and `exit_reason` ("stop"|"target"|"end_of_data").
  - `BacktestResult.metadata` includes `swap_modelled=False` (D-03), plus instrument/granularity/bar_count/etc.
  - `equity_curve` is cumulative NET pips, one point per bar, UTC `DatetimeIndex`.
- Created `tests/test_backtest_engine.py` — 12 tests (the four AC-mandated, named below, plus guards).

**The four AC-mandated tests:**
- (a) `test_no_lookahead_canary_explicit` + `test_no_lookahead_property` (hypothesis) — poison bar K, assert no
  decision at bar < K changes.
- (b) `test_costs_non_zero_multi_trade` + `test_apply_costs_invariants` (hypothesis) — `sum(cost_pips) > 0`;
  gross PnL >= net PnL on every trade / for any spread+slippage.
- (c) `test_known_trade_exact_pnl` — hand-crafted long: entry 1.10000, target 1.10150, spread 2 / slippage 1 pip
  → net PnL 12.00000 pips (gross 15.0, cost 3.0), asserted to 5 decimals. Plus `test_known_short_trade_stop_wins_tie`
  proving the both-breach tie-break.
- (d) `test_stops_fill_within_bar` (hypothesis) — every stop/target fill lies within `[low, high]` of its bar.

**Key patterns / gotchas:**
- `mypy strict` does NOT understand pydantic v2 models without the plugin — constructing a model that omits a
  defaulted field reports a spurious "Missing named argument". Enabled `plugins = ["pydantic.mypy"]` in
  `pyproject.toml [tool.mypy]`. Verified it does NOT regress config/ data/ strategies/ (all 12 src files pass).
- `Signal.generated_at == bar_time` is how the engine matches "is there a NEW signal on the current bar?" — the
  strategy may return its whole signal list for the prefix; only the one timestamped to the current bar is acted on.
- Slippage on EXIT only (stop/target market fills); not on the controlled next-open entry, not on end-of-data close.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/test_backtest_engine.py -q` → 12 passed, exit 0
- `python -m pytest -q` (full suite) → 103 passed, exit 0
- `python -m mypy backtest/` → "Success: no issues found in 3 source files", exit 0
- `python -m mypy backtest/ tests/test_backtest_engine.py` → "Success: no issues found in 4 source files", exit 0
- `python -m mypy config/ data/ strategies/ backtest/` (plugin regression check) → "Success: ... 12 source files", exit 0

**New dependency:** `hypothesis>=6.0` added to `[project.optional-dependencies] dev`. CLAUDE.md Stack updated.
Also added `plugins = ["pydantic.mypy"]` to `[tool.mypy]`.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).

---

## POC-T-06 — 2026-05-28 (feat/poc-t-06)

**What was done:**
- Created `backtest/metrics.py`:
  - `compute_metrics(result: BacktestResult, risk_free_rate: float = 0.0) -> Metrics`.
  - `Metrics` pydantic model: `sharpe_ratio, sortino_ratio, max_drawdown_pct, max_drawdown_duration_bars,
    win_rate, profit_factor, avg_win_pips, avg_loss_pips, expectancy_pips, trade_count, swap_modelled`.
  - **Sharpe = (mean excess return / std, ddof=1) × √252** — annualisation divisor is 252 (trading days/year,
    FX convention). Documented in a one-line comment at the formula. `float('nan')` when std=0 (flat curve).
  - **Sortino** uses root-mean-square of negative excess returns as downside deviation.
  - **Max drawdown pct**: peak-to-running-trough percentage, tracked with running-peak scan.
  - **Max drawdown duration bars**: peak bar to trough bar inclusive (e.g. peak at i=2, trough at i=4 → 3 bars).
  - `trade_count < 20` → `warnings.warn(UserWarning)` ("statistically meaningless").
  - `swap_modelled` carried from `BacktestResult.metadata["swap_modelled"]` (INV-06).
- Created `backtest/walkforward.py`:
  - `WalkForwardValidator(engine, strategy).run(instrument, granularity, start, end, train_months=12, test_months=3)
    -> WalkForwardResult`.
  - Uses `dateutil.relativedelta` for correct calendar-month arithmetic (avoids 30-day approximations).
  - Rolling windows: step = `test_months`; a window is included only when `test_end <= end`.
  - Window count: 2y range (Jan24–Jan26) → 4 windows; 27m range → 5 windows.
  - `ApprovedSetEntry` criterion: ALL OOS `sharpe_ratio > 0` (NaN counts as fail) AND total OOS
    `trade_count >= 5`. Missing either → `approved_set_entry = None`. Empty windows list → `None`.
    **Empty approved set is not an error** — `WalkForwardResult` is a valid model in all cases.
  - `swap_modelled=False` on `ApprovedSetEntry` — sourced from last OOS window's metrics (INV-06, D-03).
- Updated `backtest/__init__.py` to re-export `Metrics, compute_metrics, WalkForwardValidator,
  WalkForwardResult, WindowResult, ApprovedSetEntry`.
- Created `tests/test_metrics_and_walkforward.py` — 27 tests:
  - Sharpe matches hand-computed values (both zero-mean and positive-mean series), √252 factor verified.
  - Max drawdown pct and duration against known curves (including peak-to-trough inclusive semantics).
  - Window count: 2y→4, 27m→5, too-short→0.
  - Empty approved-set paths: negative Sharpe, no windows, too few total trades.
  - Approved-set entry returned when all criteria met.
  - `pytest.warns(UserWarning)` for < 20 trades; no warning at 20 or above.
  - `swap_modelled` propagation (False and True).

**Key patterns / gotchas:**
- `dateutil.relativedelta` not `timedelta(days=30)` — month arithmetic must be calendar-correct or window
  boundaries drift and the 5-window property breaks on real data.
- `Trade` model requires `exit_reason` — test helpers must include it (easy to miss).
- Sharpe returns `nan` (not 0) when std=0 (flat curve). Walk-forward approval treats `nan` Sharpe as a
  failing criterion (same code path as non-positive Sharpe).
- Max drawdown duration: **peak-to-trough inclusive** (i.e. `trough_idx - peak_idx + 1`). Not
  peak-to-recovery; not the number of bars spent below peak.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/test_metrics_and_walkforward.py -v` → 27 passed, exit 0
- `python -m pytest -q` (full suite) → 130 passed, exit 0
- `python -m mypy backtest/` → "Success: no issues found in 5 source files", exit 0
- `python -m mypy backtest/ tests/test_metrics_and_walkforward.py` → "Success: no issues found in 6 source files", exit 0

**New dependency:** `python-dateutil>=2.8` added to `[project.dependencies]` (was a transitive dep via
pandas; now direct because `walkforward.py` imports it explicitly). CLAUDE.md Stack updated.

**Sharpe annualisation divisor used:** 252 (trading days/year, FX 5-day-week convention). Documented
at the formula with a one-line comment in `backtest/metrics.py`.

**Empty approved-set path:** `WalkForwardValidator.run()` returns `WalkForwardResult(windows=...,
approved_set_entry=None)` without raising in all failure modes: no windows, negative/NaN Sharpe on any
window, total OOS trades < 5.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).
