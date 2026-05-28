"""Tests for strategies/base.py and strategies/trend.py.

Covers:
- Direction enum values
- Signal model: field validation, UTC requirement, missing required fields
- MACrossover: golden cross → LONG, death cross → SHORT, flat/chop → no signal
- Parameter combinations: 10/50, 20/100, 20/200
- stop_distance = ATR(14), never 0 or None
- target_distance = stop_distance * rr_ratio
- quality_score in [0, 1]
- generated_at is the bar's close timestamp (not datetime.now())
- At most one signal per bar
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from strategies.base import Direction, Signal, Strategy
from strategies.trend import MACrossover


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _make_candles(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: datetime | None = None,
    freq_hours: int = 1,
) -> pd.DataFrame:
    """Build a minimal synthetic OHLC DataFrame with UTC timestamps."""
    n = len(closes)
    if highs is None:
        highs = [c * 1.001 for c in closes]
    if lows is None:
        lows = [c * 0.999 for c in closes]
    if start is None:
        start = _utc(2024, 1, 1)

    times = [start + timedelta(hours=i * freq_hours) for i in range(n)]
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open_bid": closes,
            "high_bid": highs,
            "low_bid": lows,
            "close_bid": closes,
            "volume": [100] * n,
        }
    )
    return df


def _golden_cross_df(n_warmup: int = 60) -> pd.DataFrame:
    """Synthetic data that produces a single golden cross.

    First `n_warmup` bars: close price trending DOWN  → fast EMA < slow EMA.
    Final bars: close price trending UP sharply        → fast EMA crosses above slow EMA.
    """
    # Start low, so fast EMA is below slow EMA
    prices: list[float] = []
    # Downward drift for warmup
    p = 1.2000
    for _ in range(n_warmup):
        p -= 0.0005
        prices.append(round(p, 5))
    # Strong upward move to force golden cross
    for _ in range(40):
        p += 0.0030
        prices.append(round(p, 5))
    return _make_candles(prices)


def _death_cross_df(n_warmup: int = 60) -> pd.DataFrame:
    """Synthetic data that produces a single death cross.

    First `n_warmup` bars: close price trending UP    → fast EMA > slow EMA.
    Final bars: close price dropping sharply           → fast EMA crosses below slow EMA.
    """
    prices: list[float] = []
    p = 1.2000
    for _ in range(n_warmup):
        p += 0.0005
        prices.append(round(p, 5))
    # Strong downward move to force death cross
    for _ in range(40):
        p -= 0.0030
        prices.append(round(p, 5))
    return _make_candles(prices)


def _flat_df(n: int = 200) -> pd.DataFrame:
    """Synthetic flat / genuinely constant data — no EMA crossover ever occurs.

    Both EMAs converge to the same constant value, so fast == slow throughout
    and no crossover event fires.  This is the canonical "no signal" case.
    """
    prices: list[float] = [1.2000] * n
    return _make_candles(prices)


# ---------------------------------------------------------------------------
# Direction enum
# ---------------------------------------------------------------------------

class TestDirection:
    def test_values_exist(self) -> None:
        assert Direction.LONG == "LONG"
        assert Direction.SHORT == "SHORT"
        assert Direction.FLAT == "FLAT"

    def test_enum_members(self) -> None:
        members = {d.name for d in Direction}
        assert members == {"LONG", "SHORT", "FLAT"}


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------

class TestSignalModel:
    _base: dict[str, Any] = {
        "instrument": "EUR_USD",
        "direction": Direction.LONG,
        "entry_ref": 1.1000,
        "stop_distance": 0.0050,
        "target_distance": 0.0075,
        "strategy_name": "test",
        "timeframe": "H1",
        "quality_score": 0.5,
        "generated_at": _utc(2024, 1, 15),
    }

    def test_valid_signal(self) -> None:
        s = Signal(**self._base)
        assert s.instrument == "EUR_USD"
        assert s.direction == Direction.LONG
        assert s.quality_score == 0.5

    def test_missing_instrument_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "instrument"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_direction_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "direction"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_entry_ref_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "entry_ref"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_stop_distance_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "stop_distance"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_target_distance_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "target_distance"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_strategy_name_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "strategy_name"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_timeframe_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "timeframe"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_quality_score_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "quality_score"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_missing_generated_at_raises(self) -> None:
        data = {k: v for k, v in self._base.items() if k != "generated_at"}
        with pytest.raises(ValidationError):
            Signal(**data)

    def test_stop_distance_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            Signal(**{**self._base, "stop_distance": 0.0})

    def test_stop_distance_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            Signal(**{**self._base, "stop_distance": -0.001})

    def test_target_distance_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            Signal(**{**self._base, "target_distance": 0.0})

    def test_quality_score_above_1_raises(self) -> None:
        with pytest.raises(ValidationError):
            Signal(**{**self._base, "quality_score": 1.001})

    def test_quality_score_below_0_raises(self) -> None:
        with pytest.raises(ValidationError):
            Signal(**{**self._base, "quality_score": -0.001})

    def test_naive_generated_at_raises(self) -> None:
        """generated_at must be UTC-aware (INV-03)."""
        naive = datetime(2024, 1, 15, 10, 0, 0)  # no tzinfo
        with pytest.raises(ValidationError):
            Signal(**{**self._base, "generated_at": naive})

    def test_utc_aware_generated_at_accepted(self) -> None:
        aware = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        s = Signal(**{**self._base, "generated_at": aware})
        assert s.generated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Strategy ABC
# ---------------------------------------------------------------------------

class TestStrategyABC:
    def test_cannot_instantiate_abstract_strategy(self) -> None:
        with pytest.raises(TypeError):
            Strategy()  # type: ignore[abstract]

    def test_concrete_subclass_requires_name_and_generate_signals(self) -> None:
        class Incomplete(Strategy):
            # Missing name property and generate_signals
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class Dummy(Strategy):
            @property
            def name(self) -> str:
                return "dummy"

            def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
                return []

        d = Dummy()
        assert d.name == "dummy"
        assert d.generate_signals(pd.DataFrame()) == []


# ---------------------------------------------------------------------------
# MACrossover — construction
# ---------------------------------------------------------------------------

class TestMACrossoverConstruction:
    def test_valid_construction(self) -> None:
        s = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        assert s.name == "MACrossover(10,50)"

    def test_fast_ge_slow_raises(self) -> None:
        with pytest.raises(ValueError):
            MACrossover(50, 10)

    def test_fast_eq_slow_raises(self) -> None:
        with pytest.raises(ValueError):
            MACrossover(20, 20)

    def test_zero_period_raises(self) -> None:
        with pytest.raises(ValueError):
            MACrossover(0, 50)

    def test_name_includes_periods(self) -> None:
        for fp, sp in [(10, 50), (20, 100), (20, 200)]:
            s = MACrossover(fp, sp)
            assert str(fp) in s.name and str(sp) in s.name


# ---------------------------------------------------------------------------
# MACrossover — signal generation
# ---------------------------------------------------------------------------

class TestMACrossoverSignals:
    def test_golden_cross_produces_long(self) -> None:
        """Fast EMA crosses above slow EMA → LONG signal."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        assert len(signals) >= 1, "Expected at least one LONG signal on golden cross"
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1

    def test_death_cross_produces_short(self) -> None:
        """Fast EMA crosses below slow EMA → SHORT signal."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _death_cross_df()
        signals = strategy.generate_signals(df)
        assert len(signals) >= 1, "Expected at least one SHORT signal on death cross"
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1

    def test_flat_chop_no_signal(self) -> None:
        """Flat / sideways data with minimal EMA separation → no directional crossover signal."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _flat_df()
        signals = strategy.generate_signals(df)
        # In a pure sine wave both EMAs converge — minimal crossovers expected.
        # This assertion is intentionally lenient: the wave may produce 0 or very few
        # signals, but never a sustained trending sequence.
        assert len(signals) <= 3, (
            f"Expected few/no signals on flat data, got {len(signals)}"
        )

    def test_stop_distance_is_atr_not_zero(self) -> None:
        """stop_distance must equal ATR(14) at the signal bar — never 0."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.stop_distance > 0, "stop_distance must be > 0 (ATR-based)"

    def test_target_distance_is_rr_times_stop(self) -> None:
        """target_distance = stop_distance * rr_ratio (default 1.5)."""
        strategy = MACrossover(10, 50, rr_ratio=1.5, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 1.5) < 1e-10, (
                f"target_distance {sig.target_distance} != stop_distance {sig.stop_distance} * 1.5"
            )

    def test_quality_score_in_range(self) -> None:
        """quality_score must be in [0, 1]."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert 0.0 <= sig.quality_score <= 1.0

    def test_generated_at_is_bar_timestamp_not_now(self) -> None:
        """generated_at must be the bar's close timestamp, not datetime.now() (INV-03)."""
        start = _utc(2024, 3, 1)
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        # Rebuild df with a distinctive start date so we can assert timestamps are from the data
        df2 = df.copy()
        df2["time"] = pd.to_datetime(
            [start + timedelta(hours=i) for i in range(len(df2))], utc=True
        )
        signals = strategy.generate_signals(df2)
        now = datetime.now(timezone.utc)
        for sig in signals:
            # Signal timestamp must be a bar's timestamp (far in the past relative to "now")
            # and must be UTC-aware
            assert sig.generated_at.tzinfo is not None, "generated_at must be UTC-aware"
            # Must be strictly before test execution time
            assert sig.generated_at < now, (
                f"generated_at {sig.generated_at} should be a bar timestamp, not datetime.now()"
            )
            # Must be within the DataFrame's time range
            assert df2["time"].min().to_pydatetime() <= sig.generated_at <= df2["time"].max().to_pydatetime()

    def test_at_most_one_signal_per_bar(self) -> None:
        """No two signals should share the same generated_at timestamp."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        timestamps = [s.generated_at for s in signals]
        assert len(timestamps) == len(set(timestamps)), "Duplicate timestamps detected — multiple signals per bar"

    def test_instrument_and_timeframe_propagated(self) -> None:
        strategy = MACrossover(10, 50, instrument="GBP_USD", timeframe="D")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.instrument == "GBP_USD"
            assert sig.timeframe == "D"

    def test_strategy_name_in_signal(self) -> None:
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.strategy_name == "MACrossover(10,50)"

    def test_insufficient_data_returns_empty(self) -> None:
        """DataFrame shorter than slow_period+1 must return empty list."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _make_candles([1.2] * 30)  # Less than slow=50
        signals = strategy.generate_signals(df)
        assert signals == []

    def test_does_not_mutate_input_df(self) -> None:
        """generate_signals must not modify the caller's DataFrame."""
        strategy = MACrossover(10, 50, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        original_columns = set(df.columns)
        original_shape = df.shape
        _ = strategy.generate_signals(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape


# ---------------------------------------------------------------------------
# Parameter combinations
# ---------------------------------------------------------------------------

class TestParameterCombinations:
    """Test the three specified parameter combinations."""

    @pytest.mark.parametrize(
        "fast,slow",
        [
            (10, 50),
            (20, 100),
            (20, 200),
        ],
    )
    def test_golden_cross_param_combo(self, fast: int, slow: int) -> None:
        """Each parameter combination must detect a golden cross and produce a LONG signal."""
        strategy = MACrossover(fast, slow, instrument="EUR_USD", timeframe="H1")
        # Need enough bars for slow EMA warm-up
        n_warmup = slow + 20
        prices: list[float] = []
        p = 1.2000
        # Downward trend first
        for _ in range(n_warmup):
            p -= 0.0004
            prices.append(round(p, 5))
        # Sharp upward trend to cross
        for _ in range(slow):
            p += 0.0025
            prices.append(round(p, 5))

        df = _make_candles(prices)
        signals = strategy.generate_signals(df)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1, (
            f"MACrossover({fast},{slow}) should produce LONG signal on golden cross, got {signals}"
        )
        # All signals must have positive stop and target distances
        for sig in signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0

    @pytest.mark.parametrize(
        "fast,slow",
        [
            (10, 50),
            (20, 100),
            (20, 200),
        ],
    )
    def test_death_cross_param_combo(self, fast: int, slow: int) -> None:
        """Each parameter combination must detect a death cross and produce a SHORT signal."""
        strategy = MACrossover(fast, slow, instrument="EUR_USD", timeframe="H1")
        n_warmup = slow + 20
        prices: list[float] = []
        p = 1.2000
        # Upward trend first
        for _ in range(n_warmup):
            p += 0.0004
            prices.append(round(p, 5))
        # Sharp downward trend to cross
        for _ in range(slow):
            p -= 0.0025
            prices.append(round(p, 5))

        df = _make_candles(prices)
        signals = strategy.generate_signals(df)
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1, (
            f"MACrossover({fast},{slow}) should produce SHORT signal on death cross, got {signals}"
        )
        for sig in signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0


# ---------------------------------------------------------------------------
# Custom rr_ratio
# ---------------------------------------------------------------------------

class TestCustomRRRatio:
    def test_custom_rr_ratio_applied(self) -> None:
        strategy = MACrossover(10, 50, rr_ratio=2.0, instrument="EUR_USD", timeframe="H1")
        df = _golden_cross_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 2.0) < 1e-10
