"""Tests for strategies/momentum.py — ROCMomentum.

AC coverage (P1A-T-06):
- ROC = close.pct_change(roc_period); LONG/SHORT when it crosses ±roc_threshold.
- Volatility-confirmation gate suppresses signals when ATR ≤ rolling-mean ATR.
- No signal when momentum below threshold OR volatility does not confirm.
- stop_distance = ATR(14) at signal bar (>0); target_distance = stop × rr_ratio.
- quality_score ∈ [0, 1].
- At most one signal per bar; generated_at = bar close (UTC-aware, INV-03).
- Tested at (roc_period, roc_threshold) ∈ {(10, 0.005), (20, 0.01)}.
- Volatility filter on vs off provably changes behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategies.base import Direction, Signal
from strategies.momentum import ROCMomentum


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
    times = [start + timedelta(hours=freq_hours * i) for i in range(n)]
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


def _flat_then_surge(
    n_flat: int,
    flat_close: float,
    surge_pct: float,
    *,
    n_after: int = 5,
    spread: float = 0.001,
    atr_flat: float | None = None,
) -> pd.DataFrame:
    """Build a DataFrame with n_flat flat bars followed by a surge.

    If atr_flat is given, the flat bars have that spread (high-low) and
    subsequent bars widen to 3× that spread — so ATR will exceed rolling
    mean at the surge bar, making the volatility gate open.

    If atr_flat is None, spread is used uniformly and the gate is borderline.
    """
    closes_flat = [flat_close] * n_flat
    surge_close = flat_close * (1 + surge_pct)
    closes_after = [surge_close] * n_after

    closes = closes_flat + closes_after

    highs = [c * (1 + spread) for c in closes]
    lows = [c * (1 - spread) for c in closes]

    if atr_flat is not None:
        # Wide spread on surge bars — range expansion
        for i in range(n_flat, n_flat + n_after):
            highs[i] = closes[i] + atr_flat * 3
            lows[i] = closes[i] - atr_flat * 3

    return _make_candles(closes, highs=highs, lows=lows)


_DEFAULT_PARAMS = dict(
    instrument="EUR_USD",
    timeframe="H1",
    roc_period=10,
    roc_threshold=0.005,
    atr_filter_period=20,
)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------

class TestROCMomentumConstruction:
    def test_valid_construction(self) -> None:
        strat = ROCMomentum(**_DEFAULT_PARAMS)
        assert strat._roc_period == 10
        assert strat._roc_threshold == 0.005
        assert strat._atr_filter_period == 20

    def test_name_contains_key_params(self) -> None:
        strat = ROCMomentum(**_DEFAULT_PARAMS)
        assert "ROCMomentum" in strat.name
        assert "10" in strat.name
        assert "0.005" in strat.name

    def test_invalid_roc_period_zero(self) -> None:
        with pytest.raises(ValueError, match="roc_period"):
            ROCMomentum(**{**_DEFAULT_PARAMS, "roc_period": 0})

    def test_invalid_roc_threshold_zero(self) -> None:
        with pytest.raises(ValueError, match="roc_threshold"):
            ROCMomentum(**{**_DEFAULT_PARAMS, "roc_threshold": 0.0})

    def test_invalid_roc_threshold_negative(self) -> None:
        with pytest.raises(ValueError, match="roc_threshold"):
            ROCMomentum(**{**_DEFAULT_PARAMS, "roc_threshold": -0.01})

    def test_invalid_atr_filter_period_zero(self) -> None:
        with pytest.raises(ValueError, match="atr_filter_period"):
            ROCMomentum(**{**_DEFAULT_PARAMS, "atr_filter_period": 0})

    def test_invalid_rr_ratio_zero(self) -> None:
        with pytest.raises(ValueError, match="rr_ratio"):
            ROCMomentum(**{**_DEFAULT_PARAMS, "rr_ratio": 0.0})

    def test_default_rr_ratio_is_1_5(self) -> None:
        strat = ROCMomentum(**_DEFAULT_PARAMS)
        assert strat._rr_ratio == 1.5

    def test_default_volatility_filter_on(self) -> None:
        strat = ROCMomentum(**_DEFAULT_PARAMS)
        assert strat._volatility_filter is True


# ---------------------------------------------------------------------------
# Empty / insufficient data
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_dataframe_returns_no_signals(self) -> None:
        strat = ROCMomentum(**_DEFAULT_PARAMS)
        df = _make_candles([])
        assert strat.generate_signals(df) == []

    def test_too_few_bars_returns_no_signals(self) -> None:
        """Fewer bars than roc_period → ROC is all-NaN → no signals."""
        strat = ROCMomentum(**_DEFAULT_PARAMS)  # roc_period=10
        df = _make_candles([1.0] * 9)
        assert strat.generate_signals(df) == []

    def test_flat_data_no_signal(self) -> None:
        """Constant close → ROC = 0 → always below threshold."""
        strat = ROCMomentum(**_DEFAULT_PARAMS, volatility_filter=False)
        df = _make_candles([1.2000] * 50)
        assert strat.generate_signals(df) == []

    def test_does_not_mutate_input_df(self) -> None:
        strat = ROCMomentum(**_DEFAULT_PARAMS)
        df = _make_candles([1.0 + i * 0.001 for i in range(60)])
        original_columns = set(df.columns)
        original_shape = df.shape
        strat.generate_signals(df)
        assert set(df.columns) == original_columns
        assert df.shape == original_shape


# ---------------------------------------------------------------------------
# Signal direction (vol filter OFF to isolate ROC logic)
# ---------------------------------------------------------------------------

class TestSignalDirection:
    """Tests with volatility_filter=False to test purely the momentum gate."""

    def _make_roc_surge(
        self,
        surge_pct: float,
        roc_period: int = 10,
        roc_threshold: float = 0.005,
    ) -> tuple[ROCMomentum, pd.DataFrame]:
        """Return (strategy, df) where bar index roc_period has a clean ROC == surge_pct."""
        # Build: roc_period flat bars, then one bar at surge, then trailing flat
        n_flat = roc_period
        flat_close = 1.2000
        surge_close = flat_close * (1.0 + surge_pct)
        closes = [flat_close] * n_flat + [surge_close] + [surge_close] * 5
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            atr_filter_period=20,
            volatility_filter=False,
        )
        return strat, df

    def test_long_signal_on_positive_roc_above_threshold(self) -> None:
        strat, df = self._make_roc_surge(surge_pct=0.010, roc_threshold=0.005)
        signals = strat.generate_signals(df)
        long_sigs = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_sigs) >= 1

    def test_short_signal_on_negative_roc_below_threshold(self) -> None:
        # Decline: n_flat bars then drop
        n_flat = 10
        flat_close = 1.2000
        drop_close = flat_close * (1.0 - 0.010)
        closes = [flat_close] * n_flat + [drop_close] + [drop_close] * 5
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        short_sigs = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_sigs) >= 1

    def test_no_signal_when_roc_below_threshold(self) -> None:
        """ROC = 0.2% which is below threshold=0.5% → no signal."""
        strat, df = self._make_roc_surge(surge_pct=0.002, roc_threshold=0.005)
        signals = strat.generate_signals(df)
        assert signals == []

    def test_signal_at_threshold_boundary(self) -> None:
        """ROC just above threshold triggers LONG; just below does not.

        NOTE: floating-point arithmetic means `flat_close * (1 + threshold)` gives
        a ROC fractionally below `threshold` (pct_change uses division, not
        multiplication).  We use surge_pct = threshold * 1.01 to sit safely above
        the boundary and verify the ≥ condition.
        """
        # Surge of 1.01× threshold → ROC just above threshold → LONG
        strat_above, df_above = self._make_roc_surge(
            surge_pct=0.005 * 1.01, roc_threshold=0.005
        )
        signals_above = strat_above.generate_signals(df_above)
        long_sigs = [s for s in signals_above if s.direction == Direction.LONG]
        assert len(long_sigs) >= 1, "ROC just above threshold should produce LONG"

        # Surge of 0.99× threshold → ROC just below threshold → no LONG
        strat_below, df_below = self._make_roc_surge(
            surge_pct=0.005 * 0.99, roc_threshold=0.005
        )
        signals_below = strat_below.generate_signals(df_below)
        assert signals_below == [], "ROC just below threshold should produce no signal"

    def test_instrument_propagated_to_signal(self) -> None:
        strat, df = self._make_roc_surge(surge_pct=0.010)
        signals = strat.generate_signals(df)
        assert len(signals) > 0
        assert all(s.instrument == "EUR_USD" for s in signals)

    def test_timeframe_propagated_to_signal(self) -> None:
        strat, df = self._make_roc_surge(surge_pct=0.010)
        signals = strat.generate_signals(df)
        assert len(signals) > 0
        assert all(s.timeframe == "H1" for s in signals)

    def test_at_most_one_signal_per_bar(self) -> None:
        """Indices of signal generated_at timestamps must be unique."""
        n_flat = 10
        flat_close = 1.2000
        # Multiple consecutive surge bars
        closes = [flat_close] * n_flat + [flat_close * 1.02] * 20
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        timestamps = [s.generated_at for s in signals]
        assert len(timestamps) == len(set(timestamps)), "Duplicate bar timestamps in signals"


# ---------------------------------------------------------------------------
# INV-11: stop and target distances
# ---------------------------------------------------------------------------

class TestInv11:
    def _get_signals_no_vol_filter(
        self,
        roc_period: int = 10,
        roc_threshold: float = 0.005,
        rr_ratio: float = 1.5,
    ) -> list[Signal]:
        n_flat = roc_period
        flat_close = 1.2000
        surge_close = flat_close * (1.0 + roc_threshold * 3)
        closes = [flat_close] * n_flat + [surge_close] * 20
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            atr_filter_period=20,
            rr_ratio=rr_ratio,
            volatility_filter=False,
        )
        return strat.generate_signals(df)

    def test_stop_distance_positive(self) -> None:
        signals = self._get_signals_no_vol_filter()
        assert len(signals) > 0
        assert all(s.stop_distance > 0 for s in signals)

    def test_target_distance_equals_stop_times_rr_ratio(self) -> None:
        signals = self._get_signals_no_vol_filter(rr_ratio=1.5)
        assert len(signals) > 0
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 1.5) < 1e-12

    def test_custom_rr_ratio(self) -> None:
        signals = self._get_signals_no_vol_filter(rr_ratio=2.0)
        assert len(signals) > 0
        for sig in signals:
            assert abs(sig.target_distance - sig.stop_distance * 2.0) < 1e-12

    def test_target_distance_positive(self) -> None:
        signals = self._get_signals_no_vol_filter()
        assert all(s.target_distance > 0 for s in signals)


# ---------------------------------------------------------------------------
# INV-03: UTC-aware generated_at
# ---------------------------------------------------------------------------

class TestInv03:
    def test_generated_at_is_utc_aware(self) -> None:
        """generated_at must carry timezone info (UTC, INV-03)."""
        n_flat = 10
        flat_close = 1.2000
        surge_close = flat_close * 1.020
        closes = [flat_close] * n_flat + [surge_close] * 5
        df = _make_candles(closes, start=_utc(2025, 6, 1))
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        assert len(signals) > 0
        for sig in signals:
            assert sig.generated_at.tzinfo is not None, "generated_at must be UTC-aware"

    def test_generated_at_matches_bar_close_time(self) -> None:
        """generated_at must equal the bar's time column value, not datetime.now()."""
        n_flat = 10
        flat_close = 1.2000
        surge_close = flat_close * 1.020
        closes = [flat_close] * n_flat + [surge_close] + [surge_close] * 4
        start = _utc(2025, 3, 10, 9)
        df = _make_candles(closes, start=start)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        assert len(signals) > 0
        # All generated_at timestamps must appear in the df time column
        df_times = set(df["time"].dt.to_pydatetime().tolist())
        for sig in signals:
            assert sig.generated_at in df_times, (
                f"generated_at {sig.generated_at} not found in df time column"
            )


# ---------------------------------------------------------------------------
# quality_score
# ---------------------------------------------------------------------------

class TestQualityScore:
    def test_quality_score_in_range(self) -> None:
        n_flat = 10
        flat_close = 1.2000
        # Vary surge magnitude
        for surge_pct in [0.006, 0.010, 0.020, 0.050]:
            closes = [flat_close] * n_flat + [flat_close * (1 + surge_pct)] * 10
            df = _make_candles(closes)
            strat = ROCMomentum(
                instrument="EUR_USD",
                timeframe="H1",
                roc_period=10,
                roc_threshold=0.005,
                atr_filter_period=20,
                volatility_filter=False,
            )
            for sig in strat.generate_signals(df):
                assert 0.0 <= sig.quality_score <= 1.0, (
                    f"quality_score {sig.quality_score} out of [0,1] for surge_pct={surge_pct}"
                )

    def test_quality_score_increases_with_roc_magnitude(self) -> None:
        """Larger ROC → higher (or equal) quality score."""
        def _get_qs(surge_pct: float) -> float:
            n_flat = 10
            flat_close = 1.2000
            closes = [flat_close] * n_flat + [flat_close * (1 + surge_pct)] * 10
            df = _make_candles(closes)
            strat = ROCMomentum(
                instrument="EUR_USD",
                timeframe="H1",
                roc_period=10,
                roc_threshold=0.005,
                atr_filter_period=20,
                volatility_filter=False,
            )
            signals = strat.generate_signals(df)
            return max(s.quality_score for s in signals) if signals else 0.0

        qs_small = _get_qs(0.006)
        qs_large = _get_qs(0.020)
        assert qs_large >= qs_small, "Larger surge should produce >= quality score"

    def test_quality_score_capped_at_1(self) -> None:
        """A very large ROC should be clamped to 1.0."""
        n_flat = 10
        flat_close = 1.2000
        closes = [flat_close] * n_flat + [flat_close * 2.0] * 10  # 100% surge
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        assert len(signals) > 0
        assert all(s.quality_score <= 1.0 for s in signals)


# ---------------------------------------------------------------------------
# Volatility-confirmation gate — prove filter changes behaviour
# ---------------------------------------------------------------------------

class TestVolatilityGate:
    """AC requirement: test with filter ON and OFF to prove it changes behaviour."""

    def _make_low_vol_drift(
        self,
        roc_period: int = 10,
        roc_threshold: float = 0.005,
        atr_filter_period: int = 20,
    ) -> pd.DataFrame:
        """Create a DataFrame where ROC exceeds threshold BUT ATR does NOT exceed mean.

        Strategy: flat warm-up bars, then a *gradual* steady drift (constant per-bar
        step with a fixed H-L spread).  Because spread never changes, ATR converges
        to the constant spread value and then equals its own rolling mean exactly
        (ATR > mean is False → volatility gate stays CLOSED).

        ROC exceeds threshold because the cumulative 10-bar drift exceeds
        roc_threshold.  The per-bar step is chosen to produce ROC ≈ 1.5×threshold
        after roc_period bars.
        """
        n_flat = roc_period + atr_filter_period + 10  # generous warm-up
        flat_close = 1.2000

        # per-bar step so that 10-bar cumulative ROC ≈ 1.5 × roc_threshold
        # ROC = (close_N - close_{N-roc_period}) / close_{N-roc_period}
        # drift_per_bar * roc_period / flat_close ≈ roc_threshold * 1.5
        drift_per_bar = flat_close * roc_threshold * 1.5 / roc_period

        # constant spread — ATR will equal the rolling mean exactly (never above)
        spread = 0.0010

        closes = [flat_close] * n_flat
        for _ in range(20):  # enough drift bars
            closes.append(closes[-1] + drift_per_bar)

        highs = [c + spread for c in closes]
        lows = [c - spread for c in closes]

        return _make_candles(closes, highs=highs, lows=lows)

    def _make_expanding_vol_drift(
        self,
        roc_period: int = 10,
        roc_threshold: float = 0.005,
        atr_filter_period: int = 20,
    ) -> pd.DataFrame:
        """Create a DataFrame where ROC exceeds threshold AND ATR expands above mean.

        Strategy: warm-up with moderate spread; then gradual drift bars where the
        H-L spread widens to 4× the warm-up spread.  The ATR rises while rolling
        mean lags → ATR > mean holds → volatility gate OPENS.
        """
        n_flat = roc_period + atr_filter_period + 10
        flat_close = 1.2000
        drift_per_bar = flat_close * roc_threshold * 1.5 / roc_period

        moderate_spread = 0.0010
        wide_spread = 0.0040  # 4× wider

        closes = [flat_close] * n_flat
        for _ in range(20):
            closes.append(closes[-1] + drift_per_bar)

        highs = []
        lows = []
        for i, c in enumerate(closes):
            if i >= n_flat:
                highs.append(c + wide_spread)
                lows.append(c - wide_spread)
            else:
                highs.append(c + moderate_spread)
                lows.append(c - moderate_spread)

        return _make_candles(closes, highs=highs, lows=lows)

    def test_vol_filter_on_suppresses_constant_spread_signals(self) -> None:
        """With filter ON: signals suppressed when ATR equals (not exceeds) rolling mean.

        When H-L spread is constant, ATR converges to that constant and its rolling
        mean is identical — ATR > mean is False → gate stays closed.
        """
        df = self._make_low_vol_drift()
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=True,
        )
        signals = strat.generate_signals(df)
        assert signals == [], (
            "Volatility gate (ON) should suppress signals when ATR == rolling-mean ATR "
            "(constant spread, no range expansion)"
        )

    def test_vol_filter_off_allows_constant_spread_signals(self) -> None:
        """With filter OFF: same constant-spread data that was suppressed now produces signals."""
        df = self._make_low_vol_drift()
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        assert len(signals) > 0, (
            "Volatility gate (OFF) should allow signals when ROC exceeds threshold"
        )

    def test_vol_filter_on_allows_expanding_vol_signals(self) -> None:
        """With filter ON: signals produced when ATR genuinely expands above mean."""
        df = self._make_expanding_vol_drift()
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=True,
        )
        signals = strat.generate_signals(df)
        assert len(signals) > 0, (
            "Volatility gate (ON) should allow signals when ATR expands above rolling mean"
        )

    def test_filter_changes_signal_count(self) -> None:
        """Key behavioural-difference test: filter ON vs OFF on identical data.

        Uses constant-spread drift data (gate closed when ON, open when OFF).
        filter=OFF produces signals; filter=ON suppresses them.
        """
        df = self._make_low_vol_drift()

        strat_on = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=True,
        )
        strat_off = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )

        count_on = len(strat_on.generate_signals(df))
        count_off = len(strat_off.generate_signals(df))

        assert count_off > count_on, (
            f"Filter should reduce signal count: off={count_off}, on={count_on}"
        )

    def test_filter_changes_signal_count_expanding_vol(self) -> None:
        """On expanding-vol data, both filter ON and OFF should produce signals."""
        df = self._make_expanding_vol_drift()

        strat_on = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=True,
        )
        strat_off = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=10,
            roc_threshold=0.005,
            atr_filter_period=20,
            volatility_filter=False,
        )

        signals_on = strat_on.generate_signals(df)
        signals_off = strat_off.generate_signals(df)

        assert len(signals_on) > 0, "Filter ON: should produce signals on expanding vol"
        assert len(signals_off) > 0, "Filter OFF: should produce signals on expanding vol"


# ---------------------------------------------------------------------------
# Parameter combinations: (10, 0.005) and (20, 0.01)
# ---------------------------------------------------------------------------

class TestParameterCombinations:
    """Tested at (roc_period, roc_threshold) ∈ {(10, 0.005), (20, 0.01)} per spec."""

    @pytest.mark.parametrize(
        "roc_period,roc_threshold,surge_pct",
        [
            (10, 0.005, 0.012),  # 1.2% surge vs 0.5% threshold
            (20, 0.010, 0.025),  # 2.5% surge vs 1.0% threshold
        ],
    )
    def test_long_signal_produced(
        self, roc_period: int, roc_threshold: float, surge_pct: float
    ) -> None:
        n_flat = roc_period
        flat_close = 1.2000
        surge_close = flat_close * (1.0 + surge_pct)
        closes = [flat_close] * n_flat + [surge_close] * 10
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        long_sigs = [s for s in signals if s.direction == Direction.LONG]
        assert len(long_sigs) >= 1, (
            f"Expected LONG signal for roc_period={roc_period}, "
            f"roc_threshold={roc_threshold}, surge_pct={surge_pct}"
        )

    @pytest.mark.parametrize(
        "roc_period,roc_threshold,drop_pct",
        [
            (10, 0.005, 0.012),
            (20, 0.010, 0.025),
        ],
    )
    def test_short_signal_produced(
        self, roc_period: int, roc_threshold: float, drop_pct: float
    ) -> None:
        n_flat = roc_period
        flat_close = 1.2000
        drop_close = flat_close * (1.0 - drop_pct)
        closes = [flat_close] * n_flat + [drop_close] * 10
        df = _make_candles(closes)
        strat = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            atr_filter_period=20,
            volatility_filter=False,
        )
        signals = strat.generate_signals(df)
        short_sigs = [s for s in signals if s.direction == Direction.SHORT]
        assert len(short_sigs) >= 1, (
            f"Expected SHORT signal for roc_period={roc_period}, "
            f"roc_threshold={roc_threshold}, drop_pct={drop_pct}"
        )

    @pytest.mark.parametrize(
        "roc_period,roc_threshold",
        [(10, 0.005), (20, 0.010)],
    )
    def test_vol_filter_on_vs_off_at_param_set(
        self, roc_period: int, roc_threshold: float
    ) -> None:
        """For each canonical param set, verify filter ON vs OFF changes behaviour.

        Uses constant-spread gradual drift so ROC exceeds threshold but ATR never
        exceeds its rolling mean (no range expansion) → filter=ON suppresses.
        """
        atr_filter_period = 20
        n_flat = roc_period + atr_filter_period + 10
        flat_close = 1.2000

        # Gradual per-bar drift: 10-bar cumulative ROC ≈ 1.5 × roc_threshold
        drift_per_bar = flat_close * roc_threshold * 1.5 / roc_period
        # Fixed spread: ATR converges to constant → ATR == rolling mean (not > )
        spread = 0.0010

        closes = [flat_close] * n_flat
        for _ in range(20):
            closes.append(closes[-1] + drift_per_bar)

        highs = [c + spread for c in closes]
        lows = [c - spread for c in closes]
        df = _make_candles(closes, highs=highs, lows=lows)

        strat_on = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            atr_filter_period=atr_filter_period,
            volatility_filter=True,
        )
        strat_off = ROCMomentum(
            instrument="EUR_USD",
            timeframe="H1",
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            atr_filter_period=atr_filter_period,
            volatility_filter=False,
        )

        count_on = len(strat_on.generate_signals(df))
        count_off = len(strat_off.generate_signals(df))

        assert count_off > count_on, (
            f"roc_period={roc_period}, roc_threshold={roc_threshold}: "
            f"filter=OFF ({count_off}) should exceed filter=ON ({count_on})"
        )
