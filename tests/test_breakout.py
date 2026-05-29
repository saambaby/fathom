"""Tests for strategies/breakout.py.

AC (P1A-T-07):
- LONG when close breaks above rolling range high + buffer; SHORT below range low - buffer.
- Once-per-UTC-day-per-direction latch: only the FIRST qualifying break per day fires.
- UTC session grouping (INV-03): generated_at is UTC-aware bar close timestamp.
- ATR(14) stop via shared _indicators.atr() (INV-11); target = stop * rr_ratio.
- quality_score ∈ [0, 1].
- Tested on H1 data with the rolling-range variant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategies._indicators import atr as _atr
from strategies.base import Direction, Signal
from strategies.breakout import SessionRangeBreakout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(*args: int) -> datetime:
    """Convenience constructor: _utc(Y, M, D, H=0, Min=0)."""
    return datetime(*args, tzinfo=timezone.utc)  # type: ignore[misc, arg-type]


def _make_h1_candles(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: datetime | None = None,
) -> pd.DataFrame:
    """Build a minimal H1 synthetic candle DataFrame with UTC timestamps."""
    n = len(closes)
    if highs is None:
        highs = [c + 0.0010 for c in closes]
    if lows is None:
        lows = [c - 0.0010 for c in closes]
    if start is None:
        start = _utc(2024, 1, 1, 0)
    times = [start + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open_bid": closes,
            "high_bid": highs,
            "low_bid": lows,
            "close_bid": closes,
            "volume": [500] * n,
        }
    )


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_valid_construction(self) -> None:
        s = SessionRangeBreakout(range_lookback=20)
        assert s.name.startswith("SessionRangeBreakout")

    def test_lookback_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="range_lookback"):
            SessionRangeBreakout(range_lookback=0)

    def test_lookback_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="range_lookback"):
            SessionRangeBreakout(range_lookback=-1)

    def test_negative_buffer_raises(self) -> None:
        with pytest.raises(ValueError, match="buffer_pips"):
            SessionRangeBreakout(range_lookback=5, buffer_pips=-0.001)

    def test_zero_rr_ratio_raises(self) -> None:
        with pytest.raises(ValueError, match="rr_ratio"):
            SessionRangeBreakout(range_lookback=5, rr_ratio=0.0)

    def test_negative_rr_ratio_raises(self) -> None:
        with pytest.raises(ValueError, match="rr_ratio"):
            SessionRangeBreakout(range_lookback=5, rr_ratio=-1.5)

    def test_name_encodes_params(self) -> None:
        s = SessionRangeBreakout(range_lookback=10, buffer_pips=0.0005)
        assert "10" in s.name
        assert "0.0005" in s.name

    def test_default_params(self) -> None:
        """Verify default values are sensible and accessible via name."""
        s = SessionRangeBreakout(range_lookback=20)
        assert "20" in s.name


# ---------------------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------------------

class TestInsufficientData:
    def test_fewer_bars_than_lookback_plus_one_returns_empty(self) -> None:
        s = SessionRangeBreakout(range_lookback=20)
        df = _make_h1_candles([1.2000] * 20)  # need 21 bars minimum
        assert s.generate_signals(df) == []

    def test_exactly_lookback_plus_one_can_produce_signal(self) -> None:
        """With exactly range_lookback+1 bars, the last bar CAN produce a signal."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")
        # First 5 bars flat at 1.2000; bar 6 closes sharply above — should trigger LONG.
        closes = [1.2000] * lookback + [1.2200]  # 6 bars
        highs = closes[:]
        lows = [c - 0.0020 for c in closes]
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        assert len(sigs) == 1
        assert sigs[0].direction == Direction.LONG

    def test_empty_df_returns_empty(self) -> None:
        s = SessionRangeBreakout(range_lookback=5)
        df = _make_h1_candles([])
        assert s.generate_signals(df) == []

    def test_missing_columns_raises(self) -> None:
        s = SessionRangeBreakout(range_lookback=5)
        df = pd.DataFrame({"close_bid": [1.0, 1.1]})
        with pytest.raises(ValueError, match="missing required columns"):
            s.generate_signals(df)


# ---------------------------------------------------------------------------
# Long signal: close above range high + buffer
# ---------------------------------------------------------------------------

class TestLongSignal:
    def test_long_fires_when_close_above_range_high(self) -> None:
        """Close that exceeds rolling max high should produce a LONG signal."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")

        # Base range: highs all at 1.2010, lows all at 1.1990, closes at 1.2000.
        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50
        # Bar 50 closes sharply above the rolling max high.
        closes.append(1.2100)
        highs.append(1.2100)
        lows.append(1.2090)

        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        assert len(long_sigs) >= 1
        # Last signal should be the high-close bar.
        last_long = long_sigs[-1]
        assert last_long.entry_ref == pytest.approx(1.2100, abs=1e-6)

    def test_no_long_when_close_equals_range_high(self) -> None:
        """Close exactly at range high is NOT a breakout (must be strictly above)."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")

        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50
        # Close exactly at rolling max high (= 1.2010) — not a breakout.
        closes.append(1.2010)
        highs.append(1.2010)
        lows.append(1.2000)

        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        # No signal on the last bar (close == range high, not above).
        bar_time = df["time"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        last_bar_sigs = [sg for sg in sigs if sg.generated_at == bar_time]
        assert all(sg.direction != Direction.LONG for sg in last_bar_sigs)

    def test_long_with_buffer(self) -> None:
        """Buffer must be exceeded: close at range_high + buffer/2 must NOT signal."""
        buffer = 0.0050
        lookback = 5
        s = SessionRangeBreakout(
            range_lookback=lookback, buffer_pips=buffer, instrument="EUR_USD", timeframe="H1"
        )

        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50
        # Close at range_high + buffer/2 — insufficient.
        closes.append(1.2010 + buffer / 2)
        highs.append(closes[-1])
        lows.append(1.2000)

        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        bar_time = df["time"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        last_bar_sigs = [sg for sg in sigs if sg.generated_at == bar_time]
        assert all(sg.direction != Direction.LONG for sg in last_bar_sigs)

    def test_long_with_buffer_fires_when_exceeded(self) -> None:
        """Close clearly above range_high + buffer must signal LONG."""
        buffer = 0.0050
        lookback = 5
        s = SessionRangeBreakout(
            range_lookback=lookback, buffer_pips=buffer, instrument="EUR_USD", timeframe="H1"
        )

        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50
        closes.append(1.2010 + buffer + 0.0100)
        highs.append(closes[-1])
        lows.append(1.2000)

        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        bar_time = df["time"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        last_bar_sigs = [sg for sg in sigs if sg.generated_at == bar_time]
        assert any(sg.direction == Direction.LONG for sg in last_bar_sigs)


# ---------------------------------------------------------------------------
# Short signal: close below range low − buffer
# ---------------------------------------------------------------------------

class TestShortSignal:
    def test_short_fires_when_close_below_range_low(self) -> None:
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")

        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50
        closes.append(1.1900)
        highs.append(1.1910)
        lows.append(1.1900)

        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        short_sigs = [sg for sg in sigs if sg.direction == Direction.SHORT]
        assert len(short_sigs) >= 1
        last_short = short_sigs[-1]
        assert last_short.entry_ref == pytest.approx(1.1900, abs=1e-6)

    def test_no_short_when_inside_range(self) -> None:
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")

        closes = [1.2000] * 50 + [1.2005]  # still inside range
        highs  = [1.2010] * 51
        lows   = [1.1990] * 51

        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        bar_time = df["time"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        last_bar_sigs = [sg for sg in sigs if sg.generated_at == bar_time]
        assert all(sg.direction != Direction.SHORT for sg in last_bar_sigs)


# ---------------------------------------------------------------------------
# Once-per-day-per-direction latch (primary AC)
# ---------------------------------------------------------------------------

class TestOncePerDayLatch:
    def _build_multi_break_day(
        self,
        lookback: int = 5,
        n_base: int = 50,
    ) -> tuple[pd.DataFrame, SessionRangeBreakout]:
        """
        Build a DataFrame where bar n_base and bar n_base+6 both break the
        LONG threshold on the SAME UTC day.  The second break must not fire.
        """
        s = SessionRangeBreakout(
            range_lookback=lookback, instrument="EUR_USD", timeframe="H1"
        )
        # All bars on 2024-01-01 00:00 UTC, each +1h apart.
        start = _utc(2024, 1, 1, 0)
        closes = [1.2000] * n_base
        highs  = [1.2010] * n_base
        lows   = [1.1990] * n_base

        # First break: bar n_base (within first day).
        closes.append(1.2100)
        highs.append(1.2100)
        lows.append(1.2090)

        # Intermediate bars back inside range.
        for _ in range(5):
            closes.append(1.2000)
            highs.append(1.2010)
            lows.append(1.1990)

        # Second break: another LONG, same UTC day.
        closes.append(1.2150)
        highs.append(1.2150)
        lows.append(1.2140)

        df = _make_h1_candles(closes, highs=highs, lows=lows, start=start)
        return df, s

    def test_only_first_long_fires_per_day(self) -> None:
        df, s = self._build_multi_break_day()
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        # Determine how many unique UTC days appear in long_sigs.
        days = {sg.generated_at.date() for sg in long_sigs}
        for day in days:
            day_longs = [sg for sg in long_sigs if sg.generated_at.date() == day]
            assert len(day_longs) == 1, (
                f"Expected 1 LONG per day, got {len(day_longs)} on {day}"
            )

    def test_short_latch_independent_of_long_latch(self) -> None:
        """After a LONG fires, a SHORT on the same day CAN still fire."""
        lookback = 5
        s = SessionRangeBreakout(
            range_lookback=lookback, instrument="EUR_USD", timeframe="H1"
        )
        start = _utc(2024, 1, 1, 0)
        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50

        # LONG break on same day.
        closes.append(1.2100)
        highs.append(1.2100)
        lows.append(1.2090)

        # Then a SHORT break on the same day.
        closes.append(1.1800)
        highs.append(1.1810)
        lows.append(1.1800)

        df = _make_h1_candles(closes, highs=highs, lows=lows, start=start)
        sigs = s.generate_signals(df)
        long_sigs  = [sg for sg in sigs if sg.direction == Direction.LONG]
        short_sigs = [sg for sg in sigs if sg.direction == Direction.SHORT]
        assert len(long_sigs) >= 1
        assert len(short_sigs) >= 1

    def test_latch_resets_on_new_utc_day(self) -> None:
        """A LONG on day 1 must not prevent a LONG on day 2.

        Structure:
          Day 1 (2024-01-01): 5 base bars (hours 0-4) then 1 LONG break bar (hour 5).
          Day 2 (2024-01-02): 5 base bars (hours 0-4) then 1 LONG break bar (hour 5).
        The lookback is 5, so the day-2 break bar uses the 5 base bars of day 2 as its
        reference range — and day 2's break must fire because the latch was reset at
        UTC midnight.
        """
        lookback = 5
        s = SessionRangeBreakout(
            range_lookback=lookback, instrument="EUR_USD", timeframe="H1"
        )

        closes: list[float] = []
        highs:  list[float] = []
        lows:   list[float] = []
        times:  list[datetime] = []

        # Day 1 (2024-01-01): 5 base bars at hours 0-4, then LONG break at hour 5.
        day1_start = _utc(2024, 1, 1, 0)
        for i in range(5):
            times.append(day1_start + timedelta(hours=i))
            closes.append(1.2000)
            highs.append(1.2010)
            lows.append(1.1990)
        times.append(day1_start + timedelta(hours=5))
        closes.append(1.2100)
        highs.append(1.2100)
        lows.append(1.2090)

        # Day 2 (2024-01-02): 5 base bars at hours 0-4, then LONG break at hour 5.
        day2_start = _utc(2024, 1, 2, 0)
        for i in range(5):
            times.append(day2_start + timedelta(hours=i))
            closes.append(1.2000)
            highs.append(1.2010)
            lows.append(1.1990)
        times.append(day2_start + timedelta(hours=5))
        closes.append(1.2100)
        highs.append(1.2100)
        lows.append(1.2090)

        df = pd.DataFrame(
            {
                "time": pd.to_datetime(times, utc=True),
                "open_bid": closes,
                "high_bid": highs,
                "low_bid": lows,
                "close_bid": closes,
                "volume": [500] * len(closes),
            }
        )
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]

        day1_longs = [sg for sg in long_sigs if sg.generated_at.date().day == 1]
        day2_longs = [sg for sg in long_sigs if sg.generated_at.date().day == 2]
        assert len(day1_longs) == 1, "Expected exactly 1 LONG on day 1"
        assert len(day2_longs) == 1, "Expected exactly 1 LONG on day 2 (latch reset)"

    def test_only_first_short_fires_per_day(self) -> None:
        """Two SHORT breaks on the same UTC day — only the first should fire."""
        lookback = 5
        s = SessionRangeBreakout(
            range_lookback=lookback, instrument="EUR_USD", timeframe="H1"
        )
        start = _utc(2024, 1, 1, 0)
        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50

        # First SHORT break.
        closes.append(1.1850)
        highs.append(1.1860)
        lows.append(1.1850)

        # Recovery bars — back inside range.
        for _ in range(5):
            closes.append(1.2000)
            highs.append(1.2010)
            lows.append(1.1990)

        # Second SHORT break — same UTC day, must NOT fire.
        closes.append(1.1800)
        highs.append(1.1810)
        lows.append(1.1800)

        df = _make_h1_candles(closes, highs=highs, lows=lows, start=start)
        sigs = s.generate_signals(df)
        short_sigs = [sg for sg in sigs if sg.direction == Direction.SHORT]
        days = {sg.generated_at.date() for sg in short_sigs}
        for day in days:
            day_shorts = [sg for sg in short_sigs if sg.generated_at.date() == day]
            assert len(day_shorts) == 1, (
                f"Expected 1 SHORT per day, got {len(day_shorts)} on {day}"
            )


# ---------------------------------------------------------------------------
# INV-03: UTC timestamps
# ---------------------------------------------------------------------------

class TestUTCTimestamps:
    def test_generated_at_is_utc_aware(self) -> None:
        """Every Signal.generated_at must have tzinfo (UTC-aware, INV-03)."""
        s = SessionRangeBreakout(range_lookback=5, instrument="EUR_USD", timeframe="H1")
        closes = [1.2000] * 50 + [1.2200]
        highs  = [1.2010] * 50 + [1.2200]
        lows   = [1.1990] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        assert len(sigs) > 0
        for sg in sigs:
            assert sg.generated_at.tzinfo is not None, (
                f"generated_at must be UTC-aware (INV-03), got {sg.generated_at}"
            )

    def test_generated_at_matches_bar_close_time(self) -> None:
        """generated_at must equal the bar's close timestamp, not datetime.now()."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")

        start = _utc(2024, 3, 15, 0)  # a specific reference time
        closes = [1.2000] * 50
        highs  = [1.2010] * 50
        lows   = [1.1990] * 50
        # Bar 50 (index 50) at time start + 50h closes above range.
        closes.append(1.2200)
        highs.append(1.2200)
        lows.append(1.2190)

        df = _make_h1_candles(closes, highs=highs, lows=lows, start=start)
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        assert len(long_sigs) >= 1

        last_long = long_sigs[-1]
        expected_bar_time = start + timedelta(hours=50)
        assert last_long.generated_at == expected_bar_time, (
            f"generated_at {last_long.generated_at} != expected bar time {expected_bar_time}"
        )

    def test_utc_day_boundary_respected(self) -> None:
        """Bars crossing midnight UTC must group into separate days for the latch."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")

        # Build times that straddle 2024-01-01 23:00 → 2024-01-02 00:00.
        times: list[datetime] = []
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []

        # 50 base bars ending at 2024-01-01 22:00 UTC.
        base_start = _utc(2024, 1, 1, 0)
        for i in range(50):
            times.append(base_start + timedelta(hours=i))
            closes.append(1.2000)
            highs.append(1.2010)
            lows.append(1.1990)

        # LONG break at 2024-01-01 23:00 UTC (day 1).
        times.append(_utc(2024, 1, 1, 23))
        closes.append(1.2200)
        highs.append(1.2200)
        lows.append(1.2190)

        # 50 base bars starting at 2024-01-02 00:00 UTC (day 2).
        day2_start = _utc(2024, 1, 2, 0)
        for i in range(50):
            times.append(day2_start + timedelta(hours=i))
            closes.append(1.2000)
            highs.append(1.2010)
            lows.append(1.1990)

        # LONG break at 2024-01-02 23:00 UTC (day 2).
        times.append(_utc(2024, 1, 2, 23))
        closes.append(1.2200)
        highs.append(1.2200)
        lows.append(1.2190)

        df = pd.DataFrame(
            {
                "time": pd.to_datetime(times, utc=True),
                "open_bid": closes,
                "high_bid": highs,
                "low_bid": lows,
                "close_bid": closes,
                "volume": [500] * len(closes),
            }
        )
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        days = {sg.generated_at.date() for sg in long_sigs}
        # Each day should have exactly one LONG signal.
        for day in days:
            day_longs = [sg for sg in long_sigs if sg.generated_at.date() == day]
            assert len(day_longs) == 1, (
                f"Expected 1 LONG on {day}, got {len(day_longs)}"
            )


# ---------------------------------------------------------------------------
# INV-11: stop/target and quality_score
# ---------------------------------------------------------------------------

class TestStopTargetQuality:
    def _get_breakout_signals(
        self, lookback: int = 5, rr_ratio: float = 1.5
    ) -> list[Signal]:
        s = SessionRangeBreakout(
            range_lookback=lookback, rr_ratio=rr_ratio,
            instrument="EUR_USD", timeframe="H1"
        )
        closes = [1.2000] * 50 + [1.2200]
        highs  = [1.2010] * 50 + [1.2200]
        lows   = [1.1990] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        return s.generate_signals(df)

    def test_stop_distance_positive(self) -> None:
        """stop_distance must be > 0 (INV-11)."""
        sigs = self._get_breakout_signals()
        assert len(sigs) > 0
        for sg in sigs:
            assert sg.stop_distance > 0, f"stop_distance must be > 0, got {sg.stop_distance}"

    def test_stop_distance_equals_atr(self) -> None:
        """stop_distance must equal ATR(14) at the signal bar (INV-11)."""
        lookback = 5
        closes = [1.2000] * 50 + [1.2200]
        highs  = [1.2010] * 50 + [1.2200]
        lows   = [1.1990] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)

        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        assert len(long_sigs) >= 1

        atr_series = _atr(df)
        # Find signal bar index (last close == 1.2200).
        signal_bar_idx = df.index[df["close_bid"] == 1.2200][-1]
        expected_atr = float(atr_series.iloc[signal_bar_idx])

        last_sig = long_sigs[-1]
        assert last_sig.stop_distance == pytest.approx(expected_atr, rel=1e-8)

    def test_target_equals_stop_times_rr_ratio(self) -> None:
        """target_distance == stop_distance × rr_ratio (INV-11)."""
        for rr in [1.0, 1.5, 2.0]:
            sigs = self._get_breakout_signals(rr_ratio=rr)
            for sg in sigs:
                assert sg.target_distance == pytest.approx(
                    sg.stop_distance * rr, rel=1e-8
                ), f"target != stop*rr_ratio at rr={rr}"

    def test_target_distance_positive(self) -> None:
        sigs = self._get_breakout_signals()
        for sg in sigs:
            assert sg.target_distance > 0

    def test_quality_score_in_range(self) -> None:
        """quality_score ∈ [0, 1] for all signals."""
        sigs = self._get_breakout_signals()
        assert len(sigs) > 0
        for sg in sigs:
            assert 0.0 <= sg.quality_score <= 1.0, (
                f"quality_score={sg.quality_score} outside [0,1]"
            )

    def test_quality_score_zero_for_marginal_break(self) -> None:
        """A close barely above the range edge should score close to 0."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")
        # Small break just above range high (1.2010 + epsilon).
        closes = [1.2000] * 50 + [1.2010 + 1e-6]
        highs  = [1.2010] * 50 + [1.2010 + 1e-6]
        lows   = [1.1990] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        if long_sigs:
            assert long_sigs[-1].quality_score < 0.05, (
                "Marginal break should produce near-zero quality_score"
            )

    def test_quality_score_bounded_at_1_for_large_break(self) -> None:
        """A very large break (>> ATR) must cap quality_score at 1.0."""
        lookback = 5
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")
        # Massive break: close 10 × ATR above range.
        closes = [1.2000] * 50 + [1.2000 + 10 * 0.0020]  # ATR ≈ 0.0020
        highs  = [1.2010] * 50 + [closes[-1]]
        lows   = [1.1990] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        if long_sigs:
            assert long_sigs[-1].quality_score == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# At-most-one signal per bar
# ---------------------------------------------------------------------------

class TestAtMostOneSignalPerBar:
    def test_at_most_one_signal_per_bar(self) -> None:
        """No two signals should share the same generated_at (bar close time)."""
        s = SessionRangeBreakout(range_lookback=5, instrument="EUR_USD", timeframe="H1")
        closes = [1.2000] * 100 + [1.2200]
        highs  = [1.2010] * 100 + [1.2200]
        lows   = [1.1990] * 101
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        times = [sg.generated_at for sg in sigs]
        assert len(times) == len(set(times)), (
            "Multiple signals on the same bar detected"
        )


# ---------------------------------------------------------------------------
# No-mutation guarantee
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_does_not_mutate_input_df(self) -> None:
        s = SessionRangeBreakout(range_lookback=5, instrument="EUR_USD", timeframe="H1")
        closes = [1.2000] * 50 + [1.2200]
        highs  = [1.2010] * 50 + [1.2200]
        lows   = [1.1990] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        original_columns = set(df.columns)
        original_shape = df.shape
        _ = s.generate_signals(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape


# ---------------------------------------------------------------------------
# Rolling-range variant: varying lookback (H1 test)
# ---------------------------------------------------------------------------

class TestRollingRangeVariant:
    """Tests explicitly exercising the rolling-range variant on H1 data."""

    def test_lookback_5_h1(self) -> None:
        """H1 data with lookback=5: breakout on bar 6 should produce LONG."""
        s = SessionRangeBreakout(range_lookback=5, instrument="EUR_USD", timeframe="H1")
        closes = [1.2000] * 5 + [1.2100]
        highs  = [c + 0.0010 for c in closes]
        lows   = [c - 0.0010 for c in closes]
        highs[-1] = 1.2100
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        assert any(sg.direction == Direction.LONG for sg in sigs)

    def test_lookback_20_h1(self) -> None:
        """H1 data with lookback=20: breakout after 21 bars."""
        s = SessionRangeBreakout(range_lookback=20, instrument="EUR_USD", timeframe="H1")
        closes = [1.2000] * 20 + [1.2200]
        highs  = [1.2010] * 20 + [1.2200]
        lows   = [1.1990] * 21
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        assert any(sg.direction == Direction.LONG for sg in sigs)

    def test_rolling_range_no_lookahead(self) -> None:
        """Reference range must use only prior bars (shift(1)), not the current bar."""
        # If the current bar's high is the highest, it must NOT be part of the
        # range used to detect the breakout on that same bar.
        lookback = 3
        s = SessionRangeBreakout(range_lookback=lookback, instrument="EUR_USD", timeframe="H1")
        # 3 base bars with highs at 1.2010; then bar 4 close at 1.2020.
        # Without shift(1), the rolling high would include bar 4's own high = 1.2020
        # making the threshold 1.2020 and preventing the signal.
        # With shift(1), rolling high of bars 1-3 = 1.2010, and 1.2020 > 1.2010 → LONG.
        closes = [1.2000, 1.2000, 1.2000, 1.2020]
        highs  = [1.2010, 1.2010, 1.2010, 1.2020]
        lows   = [1.1990, 1.1990, 1.1990, 1.2010]
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        assert any(sg.direction == Direction.LONG for sg in sigs), (
            "Expected LONG signal — shift(1) should exclude current bar from range"
        )

    def test_signal_fields_complete(self) -> None:
        """All Signal fields should be populated correctly."""
        s = SessionRangeBreakout(
            range_lookback=5, instrument="GBP_USD", timeframe="H1"
        )
        closes = [1.2500] * 50 + [1.2700]
        highs  = [1.2510] * 50 + [1.2700]
        lows   = [1.2490] * 51
        df = _make_h1_candles(closes, highs=highs, lows=lows)
        sigs = s.generate_signals(df)
        long_sigs = [sg for sg in sigs if sg.direction == Direction.LONG]
        assert len(long_sigs) >= 1
        sg = long_sigs[-1]
        assert sg.instrument == "GBP_USD"
        assert sg.timeframe == "H1"
        assert sg.strategy_name.startswith("SessionRangeBreakout")
        assert sg.entry_ref == pytest.approx(1.2700, abs=1e-6)
        assert sg.stop_distance > 0
        assert sg.target_distance > 0
        assert 0.0 <= sg.quality_score <= 1.0
        assert sg.generated_at.tzinfo is not None
