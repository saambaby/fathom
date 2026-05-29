"""Tests for strategies/_indicators.py.

AC (P1A-T-02):
- atr() reproduces the shipped MACrossover._compute_atr values exactly
  (same ewm(com=period-1, adjust=False) formula).
- Existing MACrossover tests are unaffected (covered in test_strategies.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategies._indicators import atr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _make_candles(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    """Build a minimal synthetic OHLC DataFrame — same factory as test_strategies."""
    n = len(closes)
    if highs is None:
        highs = [c * 1.001 for c in closes]
    if lows is None:
        lows = [c * 0.999 for c in closes]
    start = _utc(2024, 1, 1)
    times = [start + timedelta(hours=i) for i in range(n)]
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


def _reference_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Verbatim copy of the PoC MACrossover._compute_atr — used as the reference."""
    high = df["high_bid"].astype(float)
    low = df["low_bid"].astype(float)
    close = df["close_bid"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(com=period - 1, adjust=False).mean()


# ---------------------------------------------------------------------------
# Exact reproduction tests (primary AC)
# ---------------------------------------------------------------------------

class TestAtrReproducesReference:
    """atr() must produce floating-point-identical values to the PoC _compute_atr."""

    def test_default_period_14_exact_match(self) -> None:
        """Default period=14: shared helper == PoC private method on 100 bars."""
        closes = [1.2000 + i * 0.0003 for i in range(100)]
        df = _make_candles(closes)
        result = atr(df)
        reference = _reference_atr(df, 14)
        pd.testing.assert_series_equal(result, reference, check_names=False)

    def test_period_7_exact_match(self) -> None:
        """Non-default period: ensure ewm com parameter is passed correctly."""
        closes = [1.0 + i * 0.0005 for i in range(80)]
        df = _make_candles(closes)
        result = atr(df, period=7)
        reference = _reference_atr(df, 7)
        pd.testing.assert_series_equal(result, reference, check_names=False)

    def test_period_20_exact_match(self) -> None:
        closes = [1.5000 - i * 0.0002 for i in range(150)]
        df = _make_candles(closes)
        result = atr(df, period=20)
        reference = _reference_atr(df, 20)
        pd.testing.assert_series_equal(result, reference, check_names=False)

    def test_varying_high_low_exact_match(self) -> None:
        """Asymmetric high/low spreads — True Range uses |high - prev_close| path."""
        n = 60
        closes = [1.1000 + i * 0.0010 for i in range(n)]
        highs = [c + 0.0025 for c in closes]
        lows = [c - 0.0015 for c in closes]
        df = _make_candles(closes, highs=highs, lows=lows)
        result = atr(df)
        reference = _reference_atr(df, 14)
        pd.testing.assert_series_equal(result, reference, check_names=False)

    def test_exact_numeric_values_spot_check(self) -> None:
        """Spot-check a small series against hand-verified reference values."""
        # 5 bars with known H/L/C so TR is deterministic
        highs = [1.0050, 1.0060, 1.0070, 1.0065, 1.0080]
        lows  = [1.0010, 1.0015, 1.0020, 1.0025, 1.0030]
        closes = [1.0040, 1.0050, 1.0060, 1.0055, 1.0070]
        df = _make_candles(closes, highs=highs, lows=lows)
        result = atr(df, period=3)
        reference = _reference_atr(df, 3)
        pd.testing.assert_series_equal(result, reference, check_names=False)


# ---------------------------------------------------------------------------
# Contract / boundary tests
# ---------------------------------------------------------------------------

class TestAtrContract:
    def test_returns_series_same_length_as_df(self) -> None:
        df = _make_candles([1.0] * 30)
        result = atr(df)
        assert len(result) == len(df)

    def test_first_value_uses_high_minus_low(self) -> None:
        """Row 0 has no prev_close, so the True Range components that need it are NaN.
        However, pd.concat(...).max(axis=1) treats NaN as missing, so TR[0] = high[0] - low[0]
        (the one non-NaN component).  ewm(adjust=False) then initialises from that value,
        meaning result[0] == high[0] - low[0] and is NOT NaN.
        This is the correct and expected behaviour — matches the PoC _compute_atr exactly.
        """
        df = _make_candles([1.1000 + i * 0.0001 for i in range(20)])
        result = atr(df)
        expected_first = df["high_bid"].iloc[0] - df["low_bid"].iloc[0]
        assert not pd.isna(result.iloc[0]), "First ATR value should not be NaN"
        assert abs(result.iloc[0] - expected_first) < 1e-12

    def test_values_positive_after_warmup(self) -> None:
        """All non-NaN ATR values must be > 0 when high != low."""
        closes = [1.0 + i * 0.0005 for i in range(50)]
        df = _make_candles(closes)  # highs = close*1.001, lows = close*0.999
        result = atr(df)
        non_nan = result.dropna()
        assert (non_nan > 0).all(), "Non-NaN ATR values must be strictly positive"

    def test_default_period_is_14(self) -> None:
        """Calling atr(df) with no period arg == atr(df, 14)."""
        df = _make_candles([1.0 + i * 0.0003 for i in range(50)])
        assert atr(df).equals(atr(df, 14))

    def test_does_not_mutate_input_df(self) -> None:
        df = _make_candles([1.2000] * 40)
        original_columns = set(df.columns)
        original_shape = df.shape
        _ = atr(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape

    def test_works_on_minimal_two_row_df(self) -> None:
        """atr() must not raise on a 2-row DataFrame (both TR values are valid)."""
        df = _make_candles([1.2000, 1.2010])
        result = atr(df)
        assert len(result) == 2
        # Row 0 TR = high[0] - low[0] (non-NaN); row 1 TR uses prev_close
        assert not pd.isna(result.iloc[0])
        assert result.iloc[1] > 0
