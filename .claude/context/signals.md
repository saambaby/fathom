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
