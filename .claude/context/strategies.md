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
**CLAUDE.md trigger-table:** not edited (no new CLI command or stack change).
**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)
