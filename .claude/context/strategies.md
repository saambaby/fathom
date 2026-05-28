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
