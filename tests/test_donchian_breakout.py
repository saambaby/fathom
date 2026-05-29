"""Tests for DonchianBreakout strategy in strategies/trend.py.

Covers:
- Construction guards (channel_period, rr_ratio)
- LONG signal on close above prior channel_period-bar rolling max high
- SHORT signal on close below prior channel_period-bar rolling min low
- No signal inside the channel
- stop_distance = ATR(14) at signal bar (> 0); target_distance = stop * rr_ratio
- quality_score ∈ [0, 1]; derived from breakout strength
- generated_at is the bar's close timestamp (UTC-aware, INV-03), never datetime.now()
- At most one signal per bar
- No look-ahead: signal uses channel from prior N bars (shifted by 1)
- Tested at channel_period ∈ {20, 55} (classic Donchian / Turtle values)
- No mutation of input DataFrame
- Instrument and timeframe propagated to signals
- Insufficient data returns empty list
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from strategies.base import Direction, Signal
from strategies.trend import DonchianBreakout


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


def _upward_breakout_df(channel_period: int = 20, warmup_extra: int = 10) -> pd.DataFrame:
    """Synthetic data producing a clear upward Donchian breakout.

    Steps:
    1. ``channel_period + warmup_extra`` bars at a flat level (1.2000) — the
       channel high settles at the flat level, i.e. max(high) ≈ 1.2012.
    2. One final bar with a sharply higher close that exceeds the channel high.
    """
    n_flat = channel_period + warmup_extra
    flat_price = 1.2000
    # Keep highs and lows tight so the channel is well-defined
    flat_closes = [flat_price] * n_flat
    flat_highs = [flat_price * 1.001] * n_flat
    flat_lows = [flat_price * 0.999] * n_flat

    # Breakout bar: close well above the channel high
    breakout_close = flat_price * 1.010  # +1% above flat, well above channel high
    breakout_high = breakout_close * 1.001
    breakout_low = breakout_close * 0.999

    closes = flat_closes + [breakout_close]
    highs = flat_highs + [breakout_high]
    lows = flat_lows + [breakout_low]
    return _make_candles(closes, highs=highs, lows=lows)


def _downward_breakout_df(channel_period: int = 20, warmup_extra: int = 10) -> pd.DataFrame:
    """Synthetic data producing a clear downward Donchian breakout."""
    n_flat = channel_period + warmup_extra
    flat_price = 1.2000
    flat_closes = [flat_price] * n_flat
    flat_highs = [flat_price * 1.001] * n_flat
    flat_lows = [flat_price * 0.999] * n_flat

    # Breakout bar: close well below the channel low
    breakout_close = flat_price * 0.990  # -1%, well below channel low
    breakout_high = breakout_close * 1.001
    breakout_low = breakout_close * 0.999

    closes = flat_closes + [breakout_close]
    highs = flat_highs + [breakout_high]
    lows = flat_lows + [breakout_low]
    return _make_candles(closes, highs=highs, lows=lows)


def _flat_channel_df(channel_period: int = 20, n: int = 200) -> pd.DataFrame:
    """Flat data: price stays exactly at channel midpoint — no breakout fires."""
    flat_price = 1.2000
    closes = [flat_price] * n
    return _make_candles(closes)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestDonchianBreakoutConstruction:
    def test_valid_construction_period_20(self) -> None:
        s = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        assert s.name == "DonchianBreakout(20)"

    def test_valid_construction_period_55(self) -> None:
        s = DonchianBreakout(55, instrument="EUR_USD", timeframe="H1")
        assert s.name == "DonchianBreakout(55)"

    def test_zero_channel_period_raises(self) -> None:
        with pytest.raises(ValueError, match="channel_period"):
            DonchianBreakout(0)

    def test_negative_channel_period_raises(self) -> None:
        with pytest.raises(ValueError, match="channel_period"):
            DonchianBreakout(-5)

    def test_zero_rr_ratio_raises(self) -> None:
        with pytest.raises(ValueError, match="rr_ratio"):
            DonchianBreakout(20, rr_ratio=0.0)

    def test_negative_rr_ratio_raises(self) -> None:
        with pytest.raises(ValueError, match="rr_ratio"):
            DonchianBreakout(20, rr_ratio=-1.0)

    def test_name_contains_period(self) -> None:
        for period in [20, 55]:
            s = DonchianBreakout(period)
            assert str(period) in s.name


# ---------------------------------------------------------------------------
# Signal generation — direction
# ---------------------------------------------------------------------------


class TestDonchianBreakoutSignals:
    def test_upward_breakout_produces_long(self) -> None:
        """Close above prior channel high → LONG signal."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        assert len(signals) >= 1, f"Expected LONG signal on upward breakout, got {signals}"
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1

    def test_downward_breakout_produces_short(self) -> None:
        """Close below prior channel low → SHORT signal."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _downward_breakout_df(20)
        signals = strategy.generate_signals(df)
        assert len(signals) >= 1, f"Expected SHORT signal on downward breakout, got {signals}"
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1

    def test_no_signal_inside_channel(self) -> None:
        """Price staying inside the channel must produce no signal."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _flat_channel_df(20, n=200)
        signals = strategy.generate_signals(df)
        assert signals == [], f"Expected no signals on flat data, got {len(signals)}"

    def test_only_long_or_short_no_other_directions(self) -> None:
        """Signals must be LONG or SHORT, never FLAT."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.direction in (Direction.LONG, Direction.SHORT)


# ---------------------------------------------------------------------------
# INV-11: stop / target / ATR
# ---------------------------------------------------------------------------


class TestDonchianBreakoutStopTarget:
    def test_stop_distance_positive(self) -> None:
        """stop_distance must be > 0 (ATR-based, INV-11)."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.stop_distance > 0, "stop_distance must be > 0"

    def test_target_distance_positive(self) -> None:
        """target_distance must be > 0."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.target_distance > 0

    def test_target_equals_stop_times_rr_ratio(self) -> None:
        """target_distance = stop_distance * rr_ratio (default 1.5)."""
        strategy = DonchianBreakout(20, rr_ratio=1.5, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 1.5) < 1e-10, (
                f"target={sig.target_distance} != stop={sig.stop_distance} * 1.5"
            )

    def test_custom_rr_ratio_applied(self) -> None:
        strategy = DonchianBreakout(20, rr_ratio=2.0, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 2.0) < 1e-10


# ---------------------------------------------------------------------------
# quality_score
# ---------------------------------------------------------------------------


class TestDonchianBreakoutQualityScore:
    def test_quality_score_in_range(self) -> None:
        """quality_score must be in [0, 1]."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        for df in [_upward_breakout_df(20), _downward_breakout_df(20)]:
            signals = strategy.generate_signals(df)
            for sig in signals:
                assert 0.0 <= sig.quality_score <= 1.0, (
                    f"quality_score {sig.quality_score} out of [0,1]"
                )

    def test_larger_breakout_yields_higher_quality(self) -> None:
        """A bigger close excess beyond the channel edge → higher quality_score."""
        n_flat = 30
        flat_price = 1.2000
        flat_closes = [flat_price] * n_flat
        flat_highs = [flat_price * 1.001] * n_flat
        flat_lows = [flat_price * 0.999] * n_flat

        # Small breakout
        small_break = flat_price * 1.002
        df_small = _make_candles(
            flat_closes + [small_break],
            highs=flat_highs + [small_break * 1.001],
            lows=flat_lows + [small_break * 0.999],
        )
        # Large breakout
        large_break = flat_price * 1.020
        df_large = _make_candles(
            flat_closes + [large_break],
            highs=flat_highs + [large_break * 1.001],
            lows=flat_lows + [large_break * 0.999],
        )

        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        signals_small = strategy.generate_signals(df_small)
        signals_large = strategy.generate_signals(df_large)

        assert signals_small, "Expected signal on small breakout"
        assert signals_large, "Expected signal on large breakout"
        assert signals_large[-1].quality_score > signals_small[-1].quality_score, (
            "Larger breakout should yield higher quality_score"
        )


# ---------------------------------------------------------------------------
# INV-03: generated_at
# ---------------------------------------------------------------------------


class TestDonchianBreakoutTimestamp:
    def test_generated_at_is_utc_aware(self) -> None:
        """generated_at must be UTC-aware (INV-03)."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.generated_at.tzinfo is not None, "generated_at must be UTC-aware"

    def test_generated_at_is_bar_timestamp_not_now(self) -> None:
        """generated_at must be the bar's close timestamp, not datetime.now() (INV-03)."""
        start = _utc(2024, 3, 1)
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        # Rebuild with a known start time
        n = len(df)
        df["time"] = pd.to_datetime(
            [start + timedelta(hours=i) for i in range(n)], utc=True
        )
        signals = strategy.generate_signals(df)
        now = datetime.now(timezone.utc)
        for sig in signals:
            # Must be strictly before test execution time (data is historical)
            assert sig.generated_at < now
            # Must be within the DataFrame's time range
            assert df["time"].min().to_pydatetime() <= sig.generated_at
            assert sig.generated_at <= df["time"].max().to_pydatetime()

    def test_generated_at_matches_signal_bar_in_df(self) -> None:
        """generated_at must exactly match the bar's timestamp in the DataFrame."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        df_times = set(df["time"].dt.to_pydatetime())
        for sig in signals:
            assert sig.generated_at in df_times, (
                f"generated_at {sig.generated_at} not found in DataFrame times"
            )


# ---------------------------------------------------------------------------
# At most one signal per bar
# ---------------------------------------------------------------------------


class TestDonchianBreakoutAtMostOneSignalPerBar:
    def test_no_duplicate_timestamps(self) -> None:
        """No two signals should share the same generated_at timestamp."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        timestamps = [s.generated_at for s in signals]
        assert len(timestamps) == len(set(timestamps)), (
            "Duplicate timestamps detected — multiple signals per bar"
        )


# ---------------------------------------------------------------------------
# No look-ahead check
# ---------------------------------------------------------------------------


class TestDonchianBreakoutNoLookAhead:
    def test_channel_uses_prior_bars_not_current(self) -> None:
        """Verify that removing the last bar (the breakout bar) eliminates the signal.

        If the strategy used the current bar's high in the channel computation,
        a large current bar would inflate the channel and suppress the signal.
        With correct shift-by-1, the channel is fixed before the current bar,
        and a breakout fires reliably.
        """
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df_with_breakout = _upward_breakout_df(20)
        df_without_breakout = df_with_breakout.iloc[:-1].copy()

        signals_with = strategy.generate_signals(df_with_breakout)
        signals_without = strategy.generate_signals(df_without_breakout)

        long_with = [s for s in signals_with if s.direction == Direction.LONG]
        long_without = [s for s in signals_without if s.direction == Direction.SHORT or s.direction == Direction.LONG]

        assert len(long_with) >= 1, "Expected LONG on breakout bar"
        # After removing the breakout bar there should be no new signal at that timestamp
        breakout_time = long_with[-1].generated_at
        without_at_breakout = [s for s in long_without if s.generated_at == breakout_time]
        assert len(without_at_breakout) == 0, (
            "Signal at breakout timestamp should not appear when breakout bar is absent"
        )


# ---------------------------------------------------------------------------
# channel_period parametrize: {20, 55}
# ---------------------------------------------------------------------------


class TestDonchianBreakoutClassicPeriods:
    @pytest.mark.parametrize("channel_period", [20, 55])
    def test_long_signal_at_classic_period(self, channel_period: int) -> None:
        """LONG signal fires at both channel_period=20 and channel_period=55."""
        strategy = DonchianBreakout(channel_period, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(channel_period)
        signals = strategy.generate_signals(df)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_signals) >= 1, (
            f"DonchianBreakout({channel_period}) should produce LONG on upward breakout"
        )
        for sig in long_signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0
            assert 0.0 <= sig.quality_score <= 1.0

    @pytest.mark.parametrize("channel_period", [20, 55])
    def test_short_signal_at_classic_period(self, channel_period: int) -> None:
        """SHORT signal fires at both channel_period=20 and channel_period=55."""
        strategy = DonchianBreakout(channel_period, instrument="EUR_USD", timeframe="H1")
        df = _downward_breakout_df(channel_period)
        signals = strategy.generate_signals(df)
        short_signals = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_signals) >= 1, (
            f"DonchianBreakout({channel_period}) should produce SHORT on downward breakout"
        )
        for sig in short_signals:
            assert sig.stop_distance > 0
            assert sig.target_distance > 0
            assert 0.0 <= sig.quality_score <= 1.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestDonchianBreakoutEdgeCases:
    def test_insufficient_data_returns_empty(self) -> None:
        """DataFrame shorter than channel_period + 1 must return empty list."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _make_candles([1.2] * 15)  # fewer than 20+1=21
        signals = strategy.generate_signals(df)
        assert signals == []

    def test_exactly_channel_period_plus_one_row_returns_empty_or_signal(self) -> None:
        """With exactly channel_period + 1 rows, a breakout bar can produce a signal
        only if the last bar closes above the (single-row) channel high."""
        # With min_periods=channel_period, we need exactly channel_period bars
        # in the rolling window before shift — at row channel_period (0-indexed),
        # the shifted channel covers rows [0, channel_period-1].
        channel_period = 20
        strategy = DonchianBreakout(channel_period, instrument="EUR_USD", timeframe="H1")
        df = _make_candles([1.2] * (channel_period + 1))
        signals = strategy.generate_signals(df)
        # Flat data: close == channel_high (the flat level), so no strict breakout.
        assert signals == []

    def test_does_not_mutate_input_df(self) -> None:
        """generate_signals must not modify the caller's DataFrame."""
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        original_columns = set(df.columns)
        original_shape = df.shape
        _ = strategy.generate_signals(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape

    def test_instrument_and_timeframe_propagated(self) -> None:
        strategy = DonchianBreakout(20, instrument="GBP_USD", timeframe="D")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.instrument == "GBP_USD"
            assert sig.timeframe == "D"

    def test_strategy_name_in_signal(self) -> None:
        strategy = DonchianBreakout(20, instrument="EUR_USD", timeframe="H1")
        df = _upward_breakout_df(20)
        signals = strategy.generate_signals(df)
        for sig in signals:
            assert sig.strategy_name == "DonchianBreakout(20)"

    def test_missing_required_columns_raises(self) -> None:
        strategy = DonchianBreakout(20)
        df = pd.DataFrame({"time": [], "close_bid": []})
        with pytest.raises(ValueError, match="missing required columns"):
            strategy.generate_signals(df)
