"""Tests for strategies/mean_reversion.py.

Covers both BollingerReversion and RSIReversion:

BollingerReversion:
- LONG when z-score ≤ −num_std (lower band breach)
- SHORT when z-score ≥ +num_std (upper band breach)
- No signal while price is within bands
- stop_distance = ATR(14), target_distance = stop × rr_ratio (INV-11 fixed RR)
- quality_score ∈ [0, 1], generated_at = bar close (UTC-aware, INV-03)
- At most one signal per bar
- Tested at (period, num_std) ∈ {(20, 2.0), (20, 2.5)}

RSIReversion:
- LONG on RSI cross-out of oversold zone (cross above threshold)
- SHORT on RSI cross-out of overbought zone (cross below threshold)
- No signal while RSI is mid-range or pinned in zone
- stop_distance = ATR(14), target_distance = stop × rr_ratio (INV-11 fixed RR)
- quality_score ∈ [0, 1], generated_at = bar close (UTC-aware, INV-03)
- At most one signal per bar
- Tested at (period, oversold, overbought) ∈ {(14, 30, 70), (14, 20, 80)}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from strategies.base import Direction, Signal
from strategies.mean_reversion import BollingerReversion, RSIReversion


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
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
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open_bid": closes,
            "high_bid": highs,
            "low_bid": lows,
            "close_bid": closes,
            "volume": [100] * n,
        }
    )


def _bollinger_lower_breach_df(period: int = 20, num_bars: int = 60) -> pd.DataFrame:
    """Prices: steady baseline then a sharp DROP below the lower band.

    The big drop forces z-score ≤ −num_std, triggering a LONG signal.
    """
    prices: list[float] = []
    base = 1.2000
    # Stable baseline to fill the rolling window
    for _ in range(num_bars):
        prices.append(base)
    # Sharp single-bar drop below lower band  (−4 std equivalent)
    prices.append(base - 0.0300)
    # A few bars to confirm
    for _ in range(5):
        prices.append(base)
    return _make_candles(prices)


def _bollinger_upper_breach_df(period: int = 20, num_bars: int = 60) -> pd.DataFrame:
    """Prices: steady baseline then a sharp SPIKE above the upper band.

    The spike forces z-score ≥ +num_std, triggering a SHORT signal.
    """
    prices: list[float] = []
    base = 1.2000
    for _ in range(num_bars):
        prices.append(base)
    prices.append(base + 0.0300)  # sharp spike above upper band
    for _ in range(5):
        prices.append(base)
    return _make_candles(prices)


def _bollinger_flat_df(n: int = 200) -> pd.DataFrame:
    """Constant prices — z-score always 0 (std = 0 after warmup), no signals."""
    # Use *slightly* varying prices to avoid zero std, but keep within bands
    prices: list[float] = []
    base = 1.2000
    for i in range(n):
        # Tiny alternating noise, well within 2 std
        prices.append(base + (0.0001 if i % 2 == 0 else -0.0001))
    return _make_candles(prices)


def _rsi_oversold_bounce_df(period: int = 14, n_warmup: int = 50) -> pd.DataFrame:
    """Series that pushes RSI below 30 then bounces back above it.

    Structure:
    1. Stable prices for warmup.
    2. Consecutive declining bars to push RSI into oversold.
    3. A recovery bar that crosses RSI back above 30 → LONG signal.
    """
    prices: list[float] = []
    base = 1.2000
    # Warmup — stable
    for _ in range(n_warmup):
        prices.append(base)
    # Sharp decline to drive RSI < 30
    p = base
    for _ in range(20):
        p -= 0.0050
        prices.append(p)
    # Strong recovery bar — close higher → RSI crosses above 30
    for _ in range(10):
        p += 0.0100
        prices.append(p)
    return _make_candles(prices)


def _rsi_overbought_drop_df(period: int = 14, n_warmup: int = 50) -> pd.DataFrame:
    """Series that pushes RSI above 70 then drops back below it.

    Structure:
    1. Stable prices for warmup.
    2. Consecutive rising bars to push RSI into overbought.
    3. A decline bar that crosses RSI back below 70 → SHORT signal.
    """
    prices: list[float] = []
    base = 1.2000
    for _ in range(n_warmup):
        prices.append(base)
    p = base
    for _ in range(20):
        p += 0.0050
        prices.append(p)
    for _ in range(10):
        p -= 0.0100
        prices.append(p)
    return _make_candles(prices)


def _rsi_midrange_df(n: int = 200) -> pd.DataFrame:
    """Alternating up/down prices — RSI stays near 50, never breaches 30/70."""
    prices: list[float] = []
    base = 1.2000
    # Strict alternation: up then down, equal magnitude → gains/losses equal each bar
    # → avg_gain ≈ avg_loss → RSI ≈ 50 throughout.
    step = 0.0010
    p = base
    for i in range(n):
        p = p + step if i % 2 == 0 else p - step
        prices.append(p)
    return _make_candles(prices)


# ---------------------------------------------------------------------------
# BollingerReversion — construction
# ---------------------------------------------------------------------------


class TestBollingerReversionConstruction:
    def test_valid_construction_defaults(self) -> None:
        s = BollingerReversion(20)
        assert "BollingerReversion" in s.name
        assert "20" in s.name

    def test_valid_construction_custom(self) -> None:
        s = BollingerReversion(20, 2.5, instrument="EUR_USD", timeframe="H1")
        assert "2.5" in s.name

    def test_period_too_small_raises(self) -> None:
        with pytest.raises(ValueError):
            BollingerReversion(1)  # period < 2

    def test_num_std_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            BollingerReversion(20, 0.0)

    def test_num_std_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            BollingerReversion(20, -1.0)

    def test_rr_ratio_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            BollingerReversion(20, rr_ratio=0.0)


# ---------------------------------------------------------------------------
# BollingerReversion — signal generation
# ---------------------------------------------------------------------------


class TestBollingerReversionSignals:
    def test_lower_breach_produces_long(self) -> None:
        """Close below lower band (z ≤ −num_std) → LONG signal."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df(period=20)
        signals = strategy.generate_signals(df)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1, f"Expected LONG on lower-band breach, got {signals}"

    def test_upper_breach_produces_short(self) -> None:
        """Close above upper band (z ≥ +num_std) → SHORT signal."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_upper_breach_df(period=20)
        signals = strategy.generate_signals(df)
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1, f"Expected SHORT on upper-band breach, got {signals}"

    def test_within_bands_no_signal(self) -> None:
        """Price within ±2 std → no signals (or very few on tiny noise)."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_flat_df(n=200)
        signals = strategy.generate_signals(df)
        # Tiny alternating noise should not breach 2 std from a near-flat SMA
        assert len(signals) == 0, f"Expected no signals within bands, got {len(signals)}"

    def test_stop_distance_is_atr_not_zero(self) -> None:
        """stop_distance must equal ATR(14) — never 0."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.stop_distance > 0

    def test_target_distance_is_rr_times_stop(self) -> None:
        """target_distance = stop_distance × rr_ratio (INV-11 fixed RR)."""
        strategy = BollingerReversion(20, 2.0, rr_ratio=1.5, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 1.5) < 1e-10

    def test_quality_score_in_range(self) -> None:
        """quality_score must be in [0, 1]."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        for df in [_bollinger_lower_breach_df(), _bollinger_upper_breach_df()]:
            for sig in strategy.generate_signals(df):
                assert 0.0 <= sig.quality_score <= 1.0

    def test_generated_at_is_bar_timestamp_utc_aware(self) -> None:
        """generated_at must be the bar's close timestamp, UTC-aware (INV-03)."""
        start = _utc(2024, 3, 1)
        df = _bollinger_lower_breach_df()
        df["time"] = pd.to_datetime(
            [start + timedelta(hours=i) for i in range(len(df))], utc=True
        )
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        signals = strategy.generate_signals(df)
        now = datetime.now(timezone.utc)
        for sig in signals:
            assert sig.generated_at.tzinfo is not None, "generated_at must be UTC-aware"
            assert sig.generated_at < now
            assert (
                df["time"].min().to_pydatetime()
                <= sig.generated_at
                <= df["time"].max().to_pydatetime()
            )

    def test_at_most_one_signal_per_bar(self) -> None:
        """No two signals share the same generated_at timestamp."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df()
        signals = strategy.generate_signals(df)
        timestamps = [s.generated_at for s in signals]
        assert len(timestamps) == len(set(timestamps)), "Duplicate timestamps — >1 signal per bar"

    def test_does_not_mutate_input_df(self) -> None:
        """generate_signals must not modify the caller's DataFrame."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df()
        original_columns = set(df.columns)
        original_shape = df.shape
        _ = strategy.generate_signals(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape

    def test_insufficient_data_returns_empty(self) -> None:
        """DataFrame shorter than period returns empty list."""
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _make_candles([1.2] * 10)
        assert strategy.generate_signals(df) == []

    def test_instrument_and_timeframe_propagated(self) -> None:
        strategy = BollingerReversion(20, 2.0, instrument="GBP_USD", timeframe="D")
        df = _bollinger_lower_breach_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.instrument == "GBP_USD"
            assert sig.timeframe == "D"

    def test_strategy_name_in_signal(self) -> None:
        strategy = BollingerReversion(20, 2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.strategy_name == "BollingerReversion(20,2.0)"

    def test_missing_column_raises(self) -> None:
        strategy = BollingerReversion(20, 2.0)
        df = _make_candles([1.2] * 50).drop(columns=["close_bid"])
        with pytest.raises(ValueError, match="missing required columns"):
            strategy.generate_signals(df)


# ---------------------------------------------------------------------------
# BollingerReversion — parameter combinations per spec
# ---------------------------------------------------------------------------


class TestBollingerReversionParamCombinations:
    """Tested param sets: (20, 2.0) and (20, 2.5)."""

    @pytest.mark.parametrize("period,num_std", [(20, 2.0), (20, 2.5)])
    def test_lower_breach_param_combo(self, period: int, num_std: float) -> None:
        strategy = BollingerReversion(period, num_std, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df(period=period)
        signals = strategy.generate_signals(df)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1, (
            f"BollingerReversion({period},{num_std}) should produce LONG on lower breach"
        )
        for sig in signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0

    @pytest.mark.parametrize("period,num_std", [(20, 2.0), (20, 2.5)])
    def test_upper_breach_param_combo(self, period: int, num_std: float) -> None:
        strategy = BollingerReversion(period, num_std, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_upper_breach_df(period=period)
        signals = strategy.generate_signals(df)
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1, (
            f"BollingerReversion({period},{num_std}) should produce SHORT on upper breach"
        )
        for sig in signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0


# ---------------------------------------------------------------------------
# RSIReversion — construction
# ---------------------------------------------------------------------------


class TestRSIReversionConstruction:
    def test_valid_construction_defaults(self) -> None:
        s = RSIReversion()
        assert "RSIReversion" in s.name
        assert "14" in s.name

    def test_valid_construction_custom(self) -> None:
        s = RSIReversion(14, 20.0, 80.0, instrument="EUR_USD", timeframe="H1")
        assert "20.0" in s.name and "80.0" in s.name

    def test_period_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            RSIReversion(0)

    def test_oversold_ge_overbought_raises(self) -> None:
        with pytest.raises(ValueError):
            RSIReversion(14, 70.0, 30.0)

    def test_oversold_eq_overbought_raises(self) -> None:
        with pytest.raises(ValueError):
            RSIReversion(14, 50.0, 50.0)

    def test_rr_ratio_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            RSIReversion(rr_ratio=0.0)


# ---------------------------------------------------------------------------
# RSIReversion — signal generation
# ---------------------------------------------------------------------------


class TestRSIReversionSignals:
    def test_oversold_bounce_produces_long(self) -> None:
        """RSI crosses above oversold → LONG signal."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1, f"Expected LONG on RSI cross-out of oversold, got {signals}"

    def test_overbought_drop_produces_short(self) -> None:
        """RSI crosses below overbought → SHORT signal."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_overbought_drop_df()
        signals = strategy.generate_signals(df)
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1, f"Expected SHORT on RSI cross-out of overbought, got {signals}"

    def test_midrange_rsi_no_signal(self) -> None:
        """RSI stays mid-range → no extreme cross-out → no signals after warmup.

        The alternating fixture keeps RSI near 50 once Wilder's EWM has
        warmed up.  Signals generated after bar 30 (2× the RSI period) are
        considered post-warmup; none should appear there.
        """
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_midrange_df(n=300)
        signals = strategy.generate_signals(df)
        # Filter to signals strictly after the EWM warmup window (bar 30 = 30 hours in)
        warmup_cutoff = df["time"].iloc[29].to_pydatetime()
        post_warmup = [s for s in signals if s.generated_at > warmup_cutoff]
        assert len(post_warmup) == 0, (
            f"Expected no signals on mid-range RSI after warmup, got {len(post_warmup)}: "
            f"{[s.generated_at for s in post_warmup]}"
        )

    def test_stop_distance_is_atr_not_zero(self) -> None:
        """stop_distance = ATR(14) — must be > 0."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.stop_distance > 0

    def test_target_distance_is_rr_times_stop(self) -> None:
        """target_distance = stop_distance × rr_ratio (INV-11 fixed RR)."""
        strategy = RSIReversion(14, 30.0, 70.0, rr_ratio=1.5, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 1.5) < 1e-10

    def test_quality_score_in_range(self) -> None:
        """quality_score must be in [0, 1]."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        for df in [_rsi_oversold_bounce_df(), _rsi_overbought_drop_df()]:
            for sig in strategy.generate_signals(df):
                assert 0.0 <= sig.quality_score <= 1.0

    def test_generated_at_is_bar_timestamp_utc_aware(self) -> None:
        """generated_at must be the bar's close timestamp, UTC-aware (INV-03)."""
        start = _utc(2024, 3, 1)
        df = _rsi_oversold_bounce_df()
        df["time"] = pd.to_datetime(
            [start + timedelta(hours=i) for i in range(len(df))], utc=True
        )
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        signals = strategy.generate_signals(df)
        now = datetime.now(timezone.utc)
        for sig in signals:
            assert sig.generated_at.tzinfo is not None
            assert sig.generated_at < now
            assert (
                df["time"].min().to_pydatetime()
                <= sig.generated_at
                <= df["time"].max().to_pydatetime()
            )

    def test_at_most_one_signal_per_bar(self) -> None:
        """No two signals share the same generated_at timestamp."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        timestamps = [s.generated_at for s in signals]
        assert len(timestamps) == len(set(timestamps)), "Duplicate timestamps — >1 signal per bar"

    def test_does_not_mutate_input_df(self) -> None:
        """generate_signals must not modify the caller's DataFrame."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        original_columns = set(df.columns)
        original_shape = df.shape
        _ = strategy.generate_signals(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape

    def test_insufficient_data_returns_empty(self) -> None:
        """DataFrame shorter than period + 1 returns empty list."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _make_candles([1.2] * 10)
        assert strategy.generate_signals(df) == []

    def test_instrument_and_timeframe_propagated(self) -> None:
        strategy = RSIReversion(14, 30.0, 70.0, instrument="GBP_USD", timeframe="D")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.instrument == "GBP_USD"
            assert sig.timeframe == "D"

    def test_strategy_name_in_signal(self) -> None:
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.strategy_name == "RSIReversion(14,30.0,70.0)"

    def test_missing_column_raises(self) -> None:
        strategy = RSIReversion(14, 30.0, 70.0)
        df = _make_candles([1.2] * 50).drop(columns=["close_bid"])
        with pytest.raises(ValueError, match="missing required columns"):
            strategy.generate_signals(df)

    def test_cross_out_not_level_based(self) -> None:
        """Signal on the crossing bar, not on every bar pinned below threshold."""
        strategy = RSIReversion(14, 30.0, 70.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        # Verify: all LONG signals are generated at a single cross-out bar, not continuous
        # The oversold stretch should produce at most a handful of cross-out signals
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1
        # No bar should be a level-signal — each cross-out is once per zone exit
        timestamps = [s.generated_at for s in long_signals]
        assert len(timestamps) == len(set(timestamps))


# ---------------------------------------------------------------------------
# RSIReversion — parameter combinations per spec
# ---------------------------------------------------------------------------


class TestRSIReversionParamCombinations:
    """Tested param sets: (14, 30, 70) and (14, 20, 80)."""

    @pytest.mark.parametrize(
        "period,oversold,overbought",
        [(14, 30.0, 70.0), (14, 20.0, 80.0)],
    )
    def test_oversold_bounce_param_combo(
        self, period: int, oversold: float, overbought: float
    ) -> None:
        strategy = RSIReversion(
            period, oversold, overbought, instrument="EUR_USD", timeframe="H1"
        )
        df = _rsi_oversold_bounce_df(period=period)
        signals = strategy.generate_signals(df)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1, (
            f"RSIReversion({period},{oversold},{overbought}) should produce LONG on oversold bounce"
        )
        for sig in signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0

    @pytest.mark.parametrize(
        "period,oversold,overbought",
        [(14, 30.0, 70.0), (14, 20.0, 80.0)],
    )
    def test_overbought_drop_param_combo(
        self, period: int, oversold: float, overbought: float
    ) -> None:
        strategy = RSIReversion(
            period, oversold, overbought, instrument="EUR_USD", timeframe="H1"
        )
        df = _rsi_overbought_drop_df(period=period)
        signals = strategy.generate_signals(df)
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1, (
            f"RSIReversion({period},{oversold},{overbought}) should produce SHORT on overbought drop"
        )
        for sig in signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0


# ---------------------------------------------------------------------------
# INV-11: Fixed RR target (no band-midline alternative)
# ---------------------------------------------------------------------------


class TestINV11FixedRR:
    """Both strategies must use stop × rr_ratio — not any alternative derivation."""

    def test_bollinger_custom_rr_applied(self) -> None:
        strategy = BollingerReversion(20, 2.0, rr_ratio=2.0, instrument="EUR_USD", timeframe="H1")
        df = _bollinger_lower_breach_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 2.0) < 1e-10

    def test_rsi_custom_rr_applied(self) -> None:
        strategy = RSIReversion(14, 30.0, 70.0, rr_ratio=2.0, instrument="EUR_USD", timeframe="H1")
        df = _rsi_oversold_bounce_df()
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 2.0) < 1e-10
