# Strategies context

## POC-T-04 — 2026-05-28 (feat/poc-t-04)

**What was done:**
- Created `strategies/__init__.py` — re-exports `Direction`, `Signal`, `Strategy`.
- Created `strategies/base.py`:
  - `Direction(str, Enum)` — `LONG | SHORT | FLAT`.
  - `Signal(BaseModel)` — 9 required fields; validators enforce `stop_distance > 0`,
    `target_distance > 0`, `quality_score in [0,1]`, `generated_at` must be UTC-aware.
  - `Strategy(ABC)` — abstract `name: str` property + abstract `generate_signals(df: pd.DataFrame) -> list[Signal]`.
- Created `strategies/trend.py`:
  - `MACrossover(Strategy)` parameterised by `fast_period`, `slow_period`, `rr_ratio` (default 1.5),
    `instrument`, `timeframe`, `atr_period` (default 14).
  - Golden cross (fast EMA > slow EMA, was ≤) → `Direction.LONG`.
  - Death cross (fast EMA < slow EMA, was ≥) → `Direction.SHORT`.
  - `stop_distance` = ATR(14) at signal bar using Wilder's smoothing (`ewm(com=period-1, adjust=False)`).
  - `target_distance` = `stop_distance * rr_ratio`.
  - `quality_score` = normalised EMA separation = `|fast-slow| / slow_ema`, clamped to [0,1].
  - `generated_at` = bar's close timestamp (UTC-aware) — **not** `datetime.now()` (INV-03).
  - Defensive `df.copy()` at top of `generate_signals` — never mutates caller's DataFrame.
- Created `tests/test_strategies.py` — 46 tests covering:
  - `Direction` enum values.
  - `Signal` validation: all 9 missing-field cases, boundary validators, naive datetime rejection.
  - `Strategy` ABC: cannot instantiate, partial subclass fails, full subclass works.
  - `MACrossover`: construction guards, golden cross → LONG, death cross → SHORT,
    flat/constant data → no signal, stop/target/quality invariants, INV-03 timestamp,
    at-most-one-signal-per-bar, instrument/timeframe propagation, insufficient data,
    no-mutation guarantee, all three parameter combos (10/50, 20/100, 20/200), custom rr_ratio.

**Key patterns / gotchas:**
- `pandas.ewm(span=..., adjust=False)` — use `adjust=False` everywhere for recursive EMA.
  Default `adjust=True` gives different numbers than charting tools expect.
- Wilder's ATR uses `ewm(com=period-1, adjust=False)` not `rolling(period).mean()`.
- `Signal.generated_at` must be the bar timestamp from `df["time"].iloc[i].to_pydatetime()`.
  Never set it from `datetime.now()`.
- `pyproject.toml` `build-backend` was originally `"setuptools.backends.legacy:build"` —
  this path is absent in the installed setuptools 82.x. Fixed to `"setuptools.build_meta"`.

**AC verification results:**
- `pytest tests/test_strategies.py -v` → **46 passed**, exit 0
- `pytest -v` (full suite) → **49 passed**, exit 0
- `mypy strategies/` → **"Success: no issues found in 3 source files"**, exit 0

**No new runtime dependencies added** (uses `pandas` and stdlib only, both already in pyproject.toml).

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)

## P1A-T-02 — 2026-05-29 (feat/p1a-t-02)

**What was done:**
- Created `strategies/_indicators.py`:
  - `atr(df: pd.DataFrame, period: int = 14) -> pd.Series` — shared ATR helper (INV-11).
  - Formula extracted verbatim from PoC `MACrossover._compute_atr`: Wilder's smoothing
    `ewm(com=period-1, adjust=False)` on True Range computed from bid OHLC columns
    (`high_bid`, `low_bid`, `close_bid`).
  - Row 0 TR = `high_bid[0] - low_bid[0]` (no prev_close available); ewm initialises
    from that value — result[0] is NOT NaN (matches the PoC private method exactly).
- Refactored `strategies/trend.py` / `MACrossover`:
  - Removed `_compute_atr` static method.
  - Added `from strategies._indicators import atr as _atr`.
  - Call site in `generate_signals` changed to `_atr(df, self._atr_period)`.
  - No behavioural change — all 46 existing MACrossover tests pass unchanged.
- Created `tests/test_indicators.py` — 11 tests covering:
  - `TestAtrReproducesReference` (5 tests): exact float reproduction against the PoC
    `_compute_atr` reference via `pd.testing.assert_series_equal` — default period 14,
    period 7, period 20, asymmetric H/L spreads, spot-check on a small known series.
  - `TestAtrContract` (6 tests): series length == df length; row 0 == `high-low`; all
    non-NaN values > 0; default period is 14; no mutation of input df; minimal 2-row df.

**Key patterns / gotchas:**
- The shared `atr()` helper must be imported as `from strategies._indicators import atr`
  in every downstream strategy (T-04 through T-07).
- `pd.concat([...]).max(axis=1)` with NaN inputs: `pandas.max(axis=1)` skips NaN by
  default (`skipna=True`), so TR[0] = `high[0] - low[0]` (not NaN) even though
  `prev_close[0]` is NaN. This is the expected behaviour — matches PoC exactly.
- No new runtime dependencies (pandas only, already in pyproject.toml).

**AC verification results:**
- `pytest tests/test_indicators.py tests/test_strategies.py -v` → **57 passed**, exit 0
- `pytest -v` (full suite) → **156 passed**, exit 0
- `mypy strategies/` → **"Success: no issues found in 4 source files"**, exit 0

**pyproject.toml:** untouched (no new dep required).
**CLAUDE.md trigger-table:** not edited (no new CLI command or stack change).
**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)

## P1A-T-04 — 2026-05-29 (feat/p1a-t-04)

**What was done:**
- Extended `strategies/trend.py` with `DonchianBreakout(Strategy)`, parameterised by
  `channel_period: int` (classic values 20 and 55 tested).
- Channel computed via `high_bid.rolling(channel_period).max().shift(1)` and
  `low_bid.rolling(channel_period).min().shift(1)` — shift-by-1 excludes the current
  bar so there is no look-ahead (close-based breakout per spec lean).
- `stop_distance` = `_indicators.atr(df, 14)` at signal bar (shared helper, INV-11).
- `target_distance` = `stop_distance × rr_ratio` (default 1.5).
- `quality_score` = `excess / channel_width` clamped to [0,1], where excess is the
  distance the close has moved beyond the channel edge and channel_width = ch_high − ch_low.
- `generated_at` = bar close timestamp (UTC-aware, INV-03); never `datetime.now()`.
- Updated `strategies/__init__.py` to re-export `DonchianBreakout` and `MACrossover`.
- Created `tests/test_donchian_breakout.py` — 32 tests covering:
  - Construction guards (zero/negative channel_period, rr_ratio).
  - LONG on upward breakout; SHORT on downward breakout; no signal inside channel.
  - ATR stop > 0; target = stop × rr_ratio; custom rr_ratio.
  - quality_score ∈ [0,1]; monotonicity (larger breakout → higher score).
  - INV-03: UTC-aware generated_at; matches bar timestamp in DataFrame.
  - At most one signal per bar (no duplicate timestamps).
  - No look-ahead (removing the breakout bar removes the signal).
  - channel_period ∈ {20, 55} (parametrize).
  - Edge cases: insufficient data, mutation guard, instrument/timeframe propagation,
    missing required columns.

**Key patterns / gotchas:**
- Rolling channel uses `min_periods=channel_period` so the first `channel_period` bars
  never produce NaN-masked-as-valid values; combined with `.shift(1)` the first valid
  channel value is at index `channel_period` (i.e. the `channel_period+1`-th row).
- Quality score denominator `channel_width = ch_high - ch_low` can be 0 on perfectly
  flat data; guard returns 0.0 in that case.
- Do NOT add `# type: ignore[assignment]` for the `bar_time` assignment — mypy rejects
  it as unused on Python 3.12; use a plain assignment instead.

**AC verification results:**
- `pytest tests/test_donchian_breakout.py -v` → **32 passed**, exit 0
- `pytest -v` (full suite) → **214 passed**, exit 0
- `mypy strategies/` → **"Success: no issues found in 4 source files"**, exit 0

**No new runtime dependencies added** (pandas + stdlib only).
**pyproject.toml:** untouched (no new dep required).

## P1A-T-05 — 2026-05-29 (feat/p1a-t-05)

**What was done:**
- Created `strategies/mean_reversion.py` with two strategies:
  - `BollingerReversion(Strategy)` — params `period: int`, `num_std: float = 2.0`,
    `rr_ratio: float = 1.5`, `instrument`, `timeframe`.
    - Band centre: SMA (classic Bollinger — explicit choice over EMA per spec lean).
    - Std: sample std `rolling(period).std(ddof=1)`.
    - z-score = `(close − SMA) / std`. LONG when z ≤ −num_std, SHORT when z ≥ +num_std.
    - No signal within bands (z between thresholds).
    - `quality_score` = `min((|z| − num_std) / num_std, 1.0)` — depth of breach, clamped [0,1].
  - `RSIReversion(Strategy)` — params `period: int = 14`, `oversold: float = 30`,
    `overbought: float = 70`, `rr_ratio: float = 1.5`, `instrument`, `timeframe`.
    - RSI via Wilder's smoothing: `ewm(com=period-1, adjust=False)` on gains/losses —
      same family as `_indicators.atr()`. Division by zero guarded: avg_loss=0 → RSI=100.
    - Cross-out trigger: LONG when prev_rsi ≤ oversold < curr_rsi (exit oversold);
      SHORT when prev_rsi ≥ overbought > curr_rsi (exit overbought). Not level-based.
    - `quality_score` = depth of prev_rsi in the zone / zone width, clamped [0,1].
  - Both strategies: `stop_distance = _indicators.atr(df, 14)` (INV-11);
    `target_distance = stop × rr_ratio` (fixed RR, no midline target per INV-11);
    `generated_at` = bar close UTC-aware (INV-03); `df.copy()` guard; ≤1 signal/bar.
- Created `tests/test_mean_reversion.py` — 49 tests covering:
  - Construction guards (period, num_std, oversold/overbought ordering, rr_ratio).
  - LONG on lower-band breach / oversold cross-out; SHORT on upper-band / overbought cross-out.
  - No signal within bands / mid-range (post-EWM-warmup assertion for RSI).
  - stop_distance > 0; target = stop × rr_ratio; quality_score ∈ [0,1].
  - INV-03 UTC-aware timestamps = bar close, not datetime.now().
  - At-most-one-signal-per-bar (unique timestamps).
  - No mutation of input DataFrame.
  - Insufficient data returns [].
  - Instrument, timeframe, strategy_name propagation.
  - Missing column raises.
  - Both param sets per spec: BollingerReversion (20,2.0), (20,2.5);
    RSIReversion (14,30,70), (14,20,80).
  - INV-11 fixed RR: custom rr_ratio applied correctly (not midline).

**Key patterns / gotchas:**
- `pandas.rolling(period, min_periods=period).std(ddof=1)` — sample std for Bollinger.
  `min_periods=period` ensures NaN until the window is full (no partial-window signals).
- RSI bar-0 NaN fix: `rsi.where(avg_gain.notna(), other=NaN)` replaces the old
  `fillna(100.0)`. `close.diff()` yields NaN at index 0; the old path set RSI[0]=100,
  which caused RSI[1]=0 and a spurious overbought SHORT on bar 1 of every DataFrame.
  With the fix, `float(NaN) >= overbought` is False — no spurious signal.
- Cross-out is strictly one signal per zone exit: RSI must cross the threshold, not
  merely be pinned on the wrong side.
- Band-midline target explicitly NOT used (INV-11 fixed-RR mandate). Both strategies:
  target_distance = stop_distance × rr_ratio only.

**AC verification results (post-fix):**
- `pytest tests/test_mean_reversion.py -v` → **49 passed**, exit 0
- `mypy strategies/` → **"Success: no issues found in 5 source files"**, exit 0

**No new runtime dependencies added** (pandas and stdlib only).
**pyproject.toml:** untouched.
**CLAUDE.md trigger-table:** not edited (no new CLI command or stack change).
**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)

## P1A-T-06 — 2026-05-29 (feat/p1a-t-06)

**What was done:**
- Created `strategies/momentum.py`:
  - `ROCMomentum(Strategy)` parameterised by `roc_period`, `roc_threshold`,
    `atr_filter_period`, `atr_stop_period` (default 14), `rr_ratio` (default 1.5),
    `volatility_filter` (default True).
  - ROC = `close.pct_change(roc_period)`.  LONG when ROC >= +roc_threshold AND
    volatility confirms; SHORT when ROC <= -roc_threshold AND volatility confirms.
  - **Volatility-confirmation gate:** `current ATR > rolling_mean(ATR, atr_filter_period)`.
    Strictly greater — ATR == mean (constant spread) does NOT confirm.  Gate is
    controllable via `volatility_filter` flag for testing and tuning.
  - `stop_distance` = ATR(14) via `strategies._indicators.atr()` (INV-11).
  - `target_distance` = `stop_distance * rr_ratio`.
  - `quality_score` = `min(1.0, (|ROC| - threshold) / threshold)` ∈ [0,1].
  - `generated_at` = bar close timestamp (UTC-aware, INV-03).
  - At most one signal per bar.
- Created `tests/test_momentum.py` — 40 tests covering:
  - Construction guards (roc_period, roc_threshold, atr_filter_period, rr_ratio).
  - Empty/insufficient data edge cases, flat data, no-mutation guarantee.
  - LONG on positive ROC above threshold; SHORT on negative ROC below threshold;
    no signal below threshold; boundary behaviour.
  - INV-11: stop_distance > 0, target = stop × rr_ratio, custom rr_ratio.
  - INV-03: generated_at UTC-aware, matches bar close timestamp.
  - quality_score in [0,1], increases with ROC magnitude, capped at 1.
  - Volatility gate: ON suppresses signals on constant-spread data (ATR == mean);
    OFF allows same signals; ON allows signals when spread widens (ATR > mean).
  - Key AC: filter provably changes signal count at both canonical param sets:
    (roc_period=10, roc_threshold=0.005) and (roc_period=20, roc_threshold=0.01).

**Key patterns / gotchas:**
- Volatility gate condition is `ATR > rolling_mean(ATR, atr_filter_period)` —
  strict inequality. When spread is constant, ATR converges to the constant and
  rolling mean matches exactly → gate is closed (ATR > mean is False).
- Test data for gate-closed scenario: flat warm-up bars + gradual per-bar drift
  with fixed spread.  ROC exceeds threshold; ATR stays flat and equals mean → gate closed.
- Test data for gate-open scenario: same drift but H-L spread widens 4× at drift
  bars → ATR rises while rolling mean lags → ATR > mean holds → gate opens.
- `close.pct_change(roc_period)` uses float division; `flat_close * (1 + threshold)`
  gives ROC fractionally below threshold due to floating-point. Use
  `threshold * 1.01` for clear above-threshold test data.
- The ATR computed for the volatility gate uses `atr_stop_period` (14) not a
  separate period — the same ATR series used for the stop also drives the gate,
  keeping the logic consistent.

**AC verification results:**
- `pytest tests/test_momentum.py -v` → **40 passed**, exit 0
- `pytest -v` (full suite) → **222 passed, 81 warnings**, exit 0
- `mypy strategies/` → **"Success: no issues found in 5 source files"**, exit 0

**No new runtime dependencies added** (pandas + stdlib only).

## P1A-T-07 — 2026-05-29 (feat/p1a-t-07)

**What was done:**
- Created `strategies/breakout.py`:
  - `SessionRangeBreakout(Strategy)` parameterised by `range_lookback`, optional `buffer_pips`
    (default 0.0), `rr_ratio` (default 1.5), `instrument`, `timeframe`.
  - `atr_period` parameter removed; ATR period hard-coded to 14 (INV-11 mandates 14-bar ATR
    unconditionally for cross-strategy ranking/sizing comparability; exposing the param as a
    constructor arg was a policy hole — INV-11/WARN-2 compliance fix).
  - Rolling N-bar range variant (Phase 1 lean): `rolling_high = high.shift(1).rolling(N).max()`,
    `rolling_low = low.shift(1).rolling(N).min()` — look-ahead free via `shift(1)`,
    `min_periods=range_lookback` (NaN until full window available).
  - LONG when `close > rolling_high + buffer_pips`; SHORT when `close < rolling_low - buffer_pips`.
  - Once-per-UTC-day-per-direction latch: `day_latches: dict[str, set[Direction]]` keyed on
    `generated_at.strftime("%Y-%m-%d")` (UTC date — INV-03). Latches are independent per direction
    and reset automatically at UTC midnight.
  - `stop_distance` = `_atr(df, 14)` at signal bar via shared `strategies._indicators.atr()` (INV-11).
  - `target_distance` = `stop_distance * rr_ratio`.
  - `quality_score` = `min(max(break_distance / atr_val, 0.0), 1.0)` — clamped to [0, 1].
  - `generated_at` = bar close timestamp (UTC-aware, INV-03) — never `datetime.now()`.
  - Defensive `df.copy()` at top of `generate_signals`.
- Created `tests/test_breakout.py` — 38 tests covering:
  - Construction guards (lookback/buffer/rr_ratio validation, name encoding).
  - Insufficient data guards (< lookback+1 bars, empty df, missing columns).
  - LONG signal: fires above range high, not on equality, buffer gating (insufficient + sufficient).
  - SHORT signal: fires below range low, silent inside range.
  - Once-per-day latch: only first LONG/SHORT fires per day, latches are independent, latch
    resets on new UTC day.
  - UTC timestamps (INV-03): generated_at is UTC-aware, equals bar close time (not datetime.now()),
    UTC day boundary is respected for latch grouping.
  - INV-11: stop_distance > 0, equals ATR(14) at signal bar, target = stop * rr_ratio, quality
    score in [0, 1], bounded at 0 for marginal breaks and 1 for massive breaks.
  - At-most-one signal per bar.
  - No-mutation guarantee.
  - Rolling-range variant on H1 with lookback=5 and lookback=20; no-lookahead verification.

**Key patterns / gotchas:**
- `shift(1).rolling(N, min_periods=N)` is the canonical look-ahead-free range in pandas.
  Using `rolling(N)` without `shift(1)` would include the current bar's price in the reference
  range — a look-ahead bias. Always shift before rolling for range-based strategies.
- Once-per-day latch is a `dict[str, set[Direction]]` keyed by UTC date string. Using UTC day
  string (`%Y-%m-%d`) avoids DST ambiguity (INV-03). The dict grows across bars so the latch
  state is inherently stateless after each call (each `generate_signals` invocation starts fresh).
- Long and Short latches must be independent. Do NOT use a single bool per day — use a set of
  fired directions so both can fire within the same day.
- `min_periods=range_lookback` on the rolling window is required; without it, `min_periods=1`
  would produce partial-window results (e.g. max of 1 bar called "range high") giving false signals
  during the warm-up phase.
- No new runtime dependencies.

**AC verification results (post INV-11 fix):**
- `pytest tests/test_breakout.py -v` → **38 passed**, exit 0
- `mypy strategies/` → **"Success: no issues found in 5 source files"**, exit 0

**pyproject.toml:** untouched (no new dep required).
**CLAUDE.md trigger-table:** not edited (no new CLI command or stack change).
**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)
