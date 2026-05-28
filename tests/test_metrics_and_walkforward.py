"""Tests for backtest/metrics.py and backtest/walkforward.py (POC-T-06).

Covers:
1. Sharpe formula matches manual calculation (exact to several dp).
2. Max drawdown matches a known equity curve.
3. Walk-forward produces the correct number of windows for a known date range.
4. Empty approved-set path returns None without raising.
5. trade_count < 20 emits a UserWarning (pytest.warns).

All dates are UTC-aware (INV-03).
"""

from __future__ import annotations

import math
import warnings
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.engine import BacktestResult, Trade
from backtest.metrics import Metrics, _ANNUALISE, compute_metrics
from backtest.walkforward import (
    ApprovedSetEntry,
    WalkForwardResult,
    WalkForwardValidator,
    WindowResult,
)
from strategies.base import Direction


# ---------------------------------------------------------------------------
# Helpers — build minimal BacktestResult objects without needing the full
# engine / store stack.
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _equity_series(values: list[float], base_date: datetime | None = None) -> pd.Series:
    """Build a UTC-indexed equity Series from a list of values."""
    if base_date is None:
        base_date = _utc(2024, 1, 1)
    idx = pd.date_range(
        start=base_date, periods=len(values), freq="D", tz="UTC"
    )
    return pd.Series(values, index=idx, dtype="float64", name="equity_pips")


def _make_trade(
    pnl_net_pips: float,
    pnl_pips: float | None = None,
    cost_pips: float = 1.0,
    exit_reason: str = "target",
) -> Trade:
    """Build a minimal Trade with the given PnL."""
    if pnl_pips is None:
        pnl_pips = pnl_net_pips + cost_pips
    return Trade(
        entry_time=_utc(2024, 1, 1),
        exit_time=_utc(2024, 1, 2),
        entry_price_gross=1.10000,
        entry_price_net=1.10005,
        exit_price_gross=1.10150,
        exit_price_net=1.10145,
        direction=Direction.LONG,
        pnl_pips=pnl_pips,
        pnl_net_pips=pnl_net_pips,
        cost_pips=cost_pips,
        exit_reason=exit_reason,
    )


def _make_result(
    trades: list[Trade],
    equity_values: list[float],
    swap_modelled: bool = False,
) -> BacktestResult:
    """Build a BacktestResult with the given trades and equity curve."""
    return BacktestResult(
        trades=trades,
        equity_curve=_equity_series(equity_values),
        metadata={"swap_modelled": swap_modelled},
    )


# ---------------------------------------------------------------------------
# 1. Sharpe formula — manual calculation
# ---------------------------------------------------------------------------

class TestSharpeFormula:
    """Verify the Sharpe ratio formula matches a hand-computed value."""

    def test_sharpe_known_series(self) -> None:
        """Sharpe of a known return series must equal the manual result.

        Return series: [1, 2, 3, 4, 5, -1, -2, -3, -4, -5] (net pips per bar).
        Equity curve starts at 0 (cumulative), so the diff of cumulative = the
        returns themselves.

        Manual calculation:
            returns = [1, 2, 3, 4, 5, -1, -2, -3, -4, -5]
            excess  = returns - 0 (rfr=0)
            mean    = 0.0
            std     = std([1,2,3,4,5,-1,-2,-3,-4,-5], ddof=1)
                    = sqrt( sum((x-0)^2) / 9 )
                    = sqrt((1+4+9+16+25+1+4+9+16+25)/9)
                    = sqrt(110/9)
                    = sqrt(12.2222...)
                    ≈ 3.496
            Sharpe  = (0.0 / 3.496) × √252 = 0.0
        """
        returns = [1.0, 2.0, 3.0, 4.0, 5.0, -1.0, -2.0, -3.0, -4.0, -5.0]
        # Build cumulative equity so that equity.diff() reproduces `returns`.
        cumulative = [sum(returns[:i+1]) for i in range(len(returns))]
        # Prepend a 0 so the first diff gives returns[0].
        equity_vals = [0.0] + cumulative
        result = _make_result(
            trades=[_make_trade(r) for r in returns],
            equity_values=equity_vals,
        )
        metrics = compute_metrics(result)
        # Mean return is 0, so Sharpe should be 0.0.
        assert abs(metrics.sharpe_ratio) < 1e-9, (
            f"Expected Sharpe ≈ 0.0, got {metrics.sharpe_ratio}"
        )

    def test_sharpe_positive_series(self) -> None:
        """Sharpe of an all-positive return series should be positive and match manual."""
        # returns = [2, 2, 2, 2, 2] — all same → std=0 → NaN or well-defined?
        # Use varying returns to get a meaningful std.
        returns = [1.0, 3.0, 2.0, 4.0, 2.0]
        cumulative = [sum(returns[:i+1]) for i in range(len(returns))]
        equity_vals = [0.0] + cumulative
        result = _make_result(
            trades=[_make_trade(r) for r in returns],
            equity_values=equity_vals,
        )
        metrics = compute_metrics(result)

        # Manual:
        # excess = [1,3,2,4,2], mean=12/5=2.4
        # std(ddof=1) of [1,3,2,4,2]:
        #   deviations = [-1.4, 0.6, -0.4, 1.6, -0.4]
        #   sum_sq = 1.96 + 0.36 + 0.16 + 2.56 + 0.16 = 5.20
        #   std = sqrt(5.20/4) = sqrt(1.3) ≈ 1.14018
        # Sharpe = (2.4 / 1.14018) × √252 ≈ 2.10497 × 15.8745 ≈ 33.411
        import math as _math
        mean_val = 2.4
        std_val = _math.sqrt(5.20 / 4.0)
        expected = (mean_val / std_val) * _math.sqrt(252)
        assert abs(metrics.sharpe_ratio - expected) < 1e-6, (
            f"Sharpe mismatch: got {metrics.sharpe_ratio}, expected {expected}"
        )

    def test_sharpe_nan_when_flat(self) -> None:
        """Flat equity curve (zero std) should produce NaN Sharpe."""
        equity_vals = [0.0, 0.0, 0.0, 0.0]
        result = _make_result(
            trades=[_make_trade(0.0) for _ in range(3)],
            equity_values=equity_vals,
        )
        metrics = compute_metrics(result)
        assert math.isnan(metrics.sharpe_ratio), (
            f"Expected NaN Sharpe for flat curve, got {metrics.sharpe_ratio}"
        )

    def test_sharpe_empty_equity(self) -> None:
        """Empty equity curve should produce NaN Sharpe (not raise)."""
        result = _make_result(trades=[], equity_values=[])
        metrics = compute_metrics(result)
        assert math.isnan(metrics.sharpe_ratio)

    def test_sharpe_annualisation_factor(self) -> None:
        """The annualisation factor must be √252 (not 365 or 260)."""
        assert abs(_ANNUALISE - math.sqrt(252)) < 1e-12, (
            f"Expected √252 = {math.sqrt(252)}, got {_ANNUALISE}"
        )


# ---------------------------------------------------------------------------
# 2. Max drawdown — known equity curves
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    """Verify max drawdown against hand-computed known curves."""

    def test_max_drawdown_known_curve(self) -> None:
        """Known curve: peak=10 → trough=4 → 40% drawdown, 3 bars."""
        # Equity: 0, 5, 10, 8, 4, 7, 9, 10
        # Peak hits 10 at index 2.  Trough 4 at index 4.
        # DD% = (4 - 10) / 10 * 100 = -60%
        # Duration: bars 2,3,4 → 3 bars
        equity_vals = [0.0, 5.0, 10.0, 8.0, 4.0, 7.0, 9.0, 10.0]
        result = _make_result(trades=[], equity_values=equity_vals)
        metrics = compute_metrics(result)
        assert abs(metrics.max_drawdown_pct - (-60.0)) < 1e-6, (
            f"Expected -60.0%, got {metrics.max_drawdown_pct}"
        )
        assert metrics.max_drawdown_duration_bars == 3, (
            f"Expected 3 bars, got {metrics.max_drawdown_duration_bars}"
        )

    def test_max_drawdown_no_drawdown(self) -> None:
        """Monotonically rising equity → zero drawdown."""
        equity_vals = [0.0, 1.0, 2.0, 3.0, 4.0]
        result = _make_result(trades=[], equity_values=equity_vals)
        metrics = compute_metrics(result)
        assert metrics.max_drawdown_pct == 0.0
        assert metrics.max_drawdown_duration_bars == 0

    def test_max_drawdown_all_losing(self) -> None:
        """Continuously declining equity: 5→4→3→2→1 (peak=5, trough=1)."""
        equity_vals = [5.0, 4.0, 3.0, 2.0, 1.0]
        result = _make_result(trades=[], equity_values=equity_vals)
        metrics = compute_metrics(result)
        # Peak=5 at index 0, trough=1 at index 4; DD% = (1-5)/5*100 = -80%
        # Duration (peak bar to trough bar inclusive): indices 0..4 = 5 bars.
        assert abs(metrics.max_drawdown_pct - (-80.0)) < 1e-6, (
            f"Expected -80%, got {metrics.max_drawdown_pct}"
        )
        assert metrics.max_drawdown_duration_bars == 5

    def test_max_drawdown_empty_curve(self) -> None:
        """Empty equity curve → 0 drawdown (no raise)."""
        result = _make_result(trades=[], equity_values=[])
        metrics = compute_metrics(result)
        assert metrics.max_drawdown_pct == 0.0
        assert metrics.max_drawdown_duration_bars == 0

    def test_max_drawdown_from_zero_declining_curve(self) -> None:
        """A curve starting at 0 and only declining must NOT report 0% drawdown."""
        # [0, -5, -10, -15] — peak is 0, curve only declines.
        # The old code short-circuited to 0.0 when peak_val == 0; now it must
        # report the actual loss magnitude using the absolute decline as basis.
        equity_vals = [0.0, -5.0, -10.0, -15.0]
        result = _make_result(trades=[], equity_values=equity_vals)
        metrics = compute_metrics(result)
        assert metrics.max_drawdown_pct != 0.0, (
            "A purely-losing curve starting at 0 must not report 0% drawdown"
        )
        assert metrics.max_drawdown_pct < 0.0, (
            f"Drawdown must be negative (a loss), got {metrics.max_drawdown_pct}"
        )

    def test_max_drawdown_two_episodes_picks_worst(self) -> None:
        """Two drawdown episodes; we should pick the larger one."""
        # 0→10→8→10→20→12→20 : first DD is -20% (2 bars), second is -40% (2 bars)
        equity_vals = [0.0, 10.0, 8.0, 10.0, 20.0, 12.0, 20.0]
        result = _make_result(trades=[], equity_values=equity_vals)
        metrics = compute_metrics(result)
        # Second episode: peak=20, trough=12 → (12-20)/20*100 = -40%
        assert abs(metrics.max_drawdown_pct - (-40.0)) < 1e-6, (
            f"Expected -40%, got {metrics.max_drawdown_pct}"
        )


# ---------------------------------------------------------------------------
# 3. Walk-forward window count
# ---------------------------------------------------------------------------

class TestWalkForwardWindowCount:
    """Verify the correct number of windows is produced for a known date range."""

    def _make_engine_stub(self) -> MagicMock:
        """Return a BacktestEngine stub whose .run() returns an empty result."""
        engine = MagicMock()
        empty_result = BacktestResult(
            trades=[],
            equity_curve=_equity_series([]),
            metadata={"swap_modelled": False},
        )
        engine.run.return_value = empty_result
        return engine

    def _make_strategy_stub(self, name: str = "test_strategy") -> MagicMock:
        strategy = MagicMock()
        strategy.name = name
        return strategy

    def test_two_year_range_produces_four_windows(self) -> None:
        """2 years, train=12m, test=3m, step=3m → 4 OOS windows.

        Windows:
        1. train: Jan24–Jan25  | test: Jan25–Apr25
        2. train: Apr24–Apr25  | test: Apr25–Jul25
        3. train: Jul24–Jul25  | test: Jul25–Oct25
        4. train: Oct24–Oct25  | test: Oct25–Jan26
        5. train: Jan25–Jan26  | test: Jan26–Apr26
        6. Would need: train Apr25–Apr26 | test Apr26–Jul26  > end(Jan26+12m)
           ... actually the 6th test_end would be Jul26 which exceeds Jan26+12m=Jan27
           Let's use exactly 2y of history: Jan 2024 → Jan 2026.
        """
        start = _utc(2024, 1, 1)
        end = _utc(2026, 1, 1)  # exactly 2 years

        engine = self._make_engine_stub()
        strategy = self._make_strategy_stub()
        validator = WalkForwardValidator(engine=engine, strategy=strategy)

        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
            train_months=12,
            test_months=3,
        )

        # start=Jan-24, end=Jan-26 (24 months).
        # Window 1: train Jan24→Jan25, test Jan25→Apr25  (step 0)
        # Window 2: train Apr24→Apr25, test Apr25→Jul25  (step 1)
        # Window 3: train Jul24→Jul25, test Jul25→Oct25  (step 2)
        # Window 4: train Oct24→Oct25, test Oct25→Jan26  (step 3)
        # Window 5: train Jan25→Jan26, test Jan26→Apr26  — test_end Apr26 > end Jan26 → STOP
        # So 4 windows fit (steps 0..3).
        assert len(result.windows) == 4, (
            f"Expected 4 windows for 2y range (12m/3m), got {len(result.windows)}"
        )

    def test_longer_range_produces_more_windows(self) -> None:
        """27 months of data → 5 windows (spec example)."""
        from dateutil.relativedelta import relativedelta
        start = _utc(2024, 1, 1)
        end = start + relativedelta(months=27)  # Apr 2026

        engine = self._make_engine_stub()
        strategy = self._make_strategy_stub()
        validator = WalkForwardValidator(engine=engine, strategy=strategy)

        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
            train_months=12,
            test_months=3,
        )
        # 27 months total; train=12, test=3, step=3.
        # Window 1: train 0-12m, test 12-15m  (test_end=15 ≤ 27 ✓)
        # Window 2: train 3-15m, test 15-18m  (test_end=18 ≤ 27 ✓)
        # Window 3: train 6-18m, test 18-21m  (test_end=21 ≤ 27 ✓)
        # Window 4: train 9-21m, test 21-24m  (test_end=24 ≤ 27 ✓)
        # Window 5: train 12-24m, test 24-27m (test_end=27 ≤ 27 ✓)
        # Window 6: train 15-27m, test 27-30m (test_end=30 > 27 ✗)
        assert len(result.windows) == 5, (
            f"Expected 5 windows for 27-month range (12m/3m), got {len(result.windows)}"
        )

    def test_too_short_range_produces_no_windows(self) -> None:
        """Range shorter than train+test → 0 windows, no error."""
        start = _utc(2024, 1, 1)
        end = _utc(2024, 6, 1)  # only 5 months

        engine = self._make_engine_stub()
        strategy = self._make_strategy_stub()
        validator = WalkForwardValidator(engine=engine, strategy=strategy)

        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
            train_months=12,
            test_months=3,
        )
        assert len(result.windows) == 0
        assert result.approved_set_entry is None

    def test_window_boundary_dates(self) -> None:
        """First window's train_start/test_end must match the step calculation."""
        start = _utc(2024, 1, 1)
        end = _utc(2026, 4, 1)  # 27m

        engine = self._make_engine_stub()
        strategy = self._make_strategy_stub()
        validator = WalkForwardValidator(engine=engine, strategy=strategy)

        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
            train_months=12,
            test_months=3,
        )

        first = result.windows[0]
        assert first.train_start == start
        assert first.train_end == _utc(2025, 1, 1)
        assert first.test_start == _utc(2025, 1, 1)
        assert first.test_end == _utc(2025, 4, 1)


# ---------------------------------------------------------------------------
# 4. Empty approved-set path returns None without raising
# ---------------------------------------------------------------------------

class TestEmptyApprovedSet:
    """Empty approved set is a valid result, not an error."""

    def _make_engine_stub_negative_sharpe(self) -> MagicMock:
        """Return a stub that produces results making all Sharpe ratios negative."""
        engine = MagicMock()

        def make_result(*args: Any, **kwargs: Any) -> BacktestResult:
            # One trade with a small loss — produces negative OOS Sharpe.
            trades = [
                _make_trade(-5.0, pnl_pips=-4.0, cost_pips=1.0),
            ]
            # Declining equity → negative Sharpe.
            equity = _equity_series([0.0, -5.0, -10.0])
            return BacktestResult(
                trades=trades,
                equity_curve=equity,
                metadata={"swap_modelled": False},
            )

        engine.run.side_effect = make_result
        return engine

    def test_negative_oos_sharpe_gives_none(self) -> None:
        """When OOS Sharpe ≤ 0 for any window, approved_set_entry is None."""
        start = _utc(2024, 1, 1)
        end = _utc(2026, 4, 1)  # 27m → 5 windows

        engine = self._make_engine_stub_negative_sharpe()
        strategy = MagicMock()
        strategy.name = "losing_strategy"

        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
        )

        # Must be a valid result object, not an exception.
        assert isinstance(result, WalkForwardResult)
        assert result.approved_set_entry is None
        assert len(result.windows) == 5  # windows still populated

    def test_no_windows_gives_none(self) -> None:
        """No windows at all → approved_set_entry is None, no raise."""
        start = _utc(2024, 1, 1)
        end = _utc(2024, 3, 1)  # 2 months — too short for any window

        engine = MagicMock()
        strategy = MagicMock()
        strategy.name = "any"

        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
        )

        assert isinstance(result, WalkForwardResult)
        assert result.approved_set_entry is None
        assert result.windows == []

    def test_too_few_total_trades_gives_none(self) -> None:
        """OOS positive Sharpe but 1 trade per window → per-window gate rejects."""
        engine = MagicMock()

        # Result with 1 trade, positive return → positive Sharpe.
        def make_result(*args: Any, **kwargs: Any) -> BacktestResult:
            trades = [_make_trade(10.0, pnl_pips=11.0, cost_pips=1.0)]
            equity = _equity_series([0.0, 5.0, 10.0])
            return BacktestResult(
                trades=trades,
                equity_curve=equity,
                metadata={"swap_modelled": False},
            )

        engine.run.side_effect = make_result
        strategy = MagicMock()
        strategy.name = "few_trades"

        start = _utc(2024, 1, 1)
        end = _utc(2025, 4, 1)  # 15m → 1 window (train=12, test=3)

        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
        )

        # 1 trade per window < 5 per-window threshold → None.
        assert result.approved_set_entry is None

    def test_one_trade_per_window_five_windows_rejected(self) -> None:
        """1 OOS trade per window × 5 windows (total=5, all Sharpe>0) → REJECTED.

        This is the motivating case for the per-window ruling: aggregate total
        passes (5 >= 5) but each individual window has only 1 trade < 5, so
        the per-window gate must reject it.
        """
        engine = MagicMock()

        def make_result(*args: Any, **kwargs: Any) -> BacktestResult:
            # Exactly 1 trade, positive equity → positive Sharpe.
            trades = [_make_trade(10.0, pnl_pips=11.0, cost_pips=1.0)]
            equity = _equity_series([0.0, 5.0, 10.0])
            return BacktestResult(
                trades=trades,
                equity_curve=equity,
                metadata={"swap_modelled": False},
            )

        engine.run.side_effect = make_result
        strategy = MagicMock()
        strategy.name = "one_trade_per_window"

        # 27m → 5 OOS windows; 1 trade per window, total=5 which would pass
        # the old aggregate gate — must be REJECTED by the per-window gate.
        start = _utc(2024, 1, 1)
        end = _utc(2026, 4, 1)

        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
        )

        assert len(result.windows) == 5
        assert result.approved_set_entry is None, (
            "Per-window gate must reject: 1 trade/window < 5 even though total == 5"
        )

    def test_five_trades_per_window_all_windows_approved(self) -> None:
        """Every window has ≥5 OOS trades and positive Sharpe → APPROVED."""
        engine = MagicMock()

        def make_result(*args: Any, **kwargs: Any) -> BacktestResult:
            # 5 winning trades, varying equity bars so per-bar std is non-zero
            # (all-equal diffs would produce std=0 → NaN Sharpe).
            trades = [_make_trade(10.0, pnl_pips=11.0, cost_pips=1.0) for _ in range(5)]
            equity = _equity_series([0.0, 8.0, 19.0, 29.0, 42.0, 50.0])
            return BacktestResult(
                trades=trades,
                equity_curve=equity,
                metadata={"swap_modelled": False},
            )

        engine.run.side_effect = make_result
        strategy = MagicMock()
        strategy.name = "five_per_window"

        # 27m → 5 OOS windows; 5 trades per window satisfies the per-window gate.
        start = _utc(2024, 1, 1)
        end = _utc(2026, 4, 1)

        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
        )

        entry = result.approved_set_entry
        assert entry is not None, (
            "Per-window gate must approve: every window has 5 trades and positive Sharpe"
        )
        assert isinstance(entry, ApprovedSetEntry)
        assert entry.instrument == "EUR_USD"
        assert entry.granularity == "H1"
        assert entry.strategy_name == "five_per_window"
        assert entry.swap_modelled is False  # INV-06 / D-03
        assert entry.oos_trade_count_total == 25  # 5 windows × 5 trades

    def test_approved_set_returns_entry_when_criteria_met(self) -> None:
        """When every OOS window has Sharpe > 0 and trade_count >= 5, entry returned."""
        engine = MagicMock()

        def make_result(*args: Any, **kwargs: Any) -> BacktestResult:
            # 6 winning trades, strongly positive equity → positive Sharpe.
            trades = [
                _make_trade(10.0, pnl_pips=11.0, cost_pips=1.0),
                _make_trade(8.0, pnl_pips=9.0, cost_pips=1.0),
                _make_trade(12.0, pnl_pips=13.0, cost_pips=1.0),
                _make_trade(9.0, pnl_pips=10.0, cost_pips=1.0),
                _make_trade(11.0, pnl_pips=12.0, cost_pips=1.0),
                _make_trade(7.0, pnl_pips=8.0, cost_pips=1.0),
            ]
            equity = _equity_series([0.0, 10.0, 18.0, 30.0, 39.0, 50.0, 57.0])
            return BacktestResult(
                trades=trades,
                equity_curve=equity,
                metadata={"swap_modelled": False},
            )

        engine.run.side_effect = make_result
        strategy = MagicMock()
        strategy.name = "winning_strategy"

        # 27m range → 5 windows; 6 OOS trades per window, all Sharpe > 0.
        start = _utc(2024, 1, 1)
        end = _utc(2026, 4, 1)

        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        result = validator.run(
            instrument="EUR_USD",
            granularity="H1",
            start=start,
            end=end,
        )

        entry = result.approved_set_entry
        assert entry is not None
        assert isinstance(entry, ApprovedSetEntry)
        assert entry.instrument == "EUR_USD"
        assert entry.granularity == "H1"
        assert entry.strategy_name == "winning_strategy"
        assert entry.swap_modelled is False  # INV-06 / D-03
        assert entry.oos_trade_count_total == 30  # 5 windows × 6 trades


# ---------------------------------------------------------------------------
# 5. trade_count < 20 emits UserWarning
# ---------------------------------------------------------------------------

class TestTradeCountWarning:
    """compute_metrics must warn when trade count < 20."""

    def test_warn_below_20_trades(self) -> None:
        """Fewer than 20 trades should trigger a UserWarning."""
        trades = [_make_trade(1.0) for _ in range(5)]
        result = _make_result(
            trades=trades,
            equity_values=[float(i) for i in range(6)],
        )
        with pytest.warns(UserWarning, match="statistically meaningless"):
            compute_metrics(result)

    def test_warn_zero_trades(self) -> None:
        """Zero trades should also trigger the warning."""
        result = _make_result(trades=[], equity_values=[])
        with pytest.warns(UserWarning, match="statistically meaningless"):
            compute_metrics(result)

    def test_no_warn_at_20_trades(self) -> None:
        """Exactly 20 trades should NOT trigger a warning."""
        trades = [_make_trade(1.0) for _ in range(20)]
        equity_vals = [float(i) for i in range(21)]
        result = _make_result(trades=trades, equity_values=equity_vals)
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            # Should not raise — if it does the test fails.
            compute_metrics(result)

    def test_no_warn_above_20_trades(self) -> None:
        """More than 20 trades should NOT trigger a warning."""
        trades = [_make_trade(1.0) for _ in range(25)]
        equity_vals = [float(i) for i in range(26)]
        result = _make_result(trades=trades, equity_values=equity_vals)
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            compute_metrics(result)


# ---------------------------------------------------------------------------
# 6. swap_modelled propagation (INV-06)
# ---------------------------------------------------------------------------

class TestSwapModelledPropagation:
    """swap_modelled must be carried through from BacktestResult.metadata."""

    def test_swap_modelled_false_propagates(self) -> None:
        """swap_modelled=False in metadata → Metrics.swap_modelled is False."""
        result = _make_result(
            trades=[_make_trade(1.0) for _ in range(25)],
            equity_values=[float(i) for i in range(26)],
            swap_modelled=False,
        )
        metrics = compute_metrics(result)
        assert metrics.swap_modelled is False

    def test_swap_modelled_true_propagates(self) -> None:
        """swap_modelled=True in metadata → Metrics.swap_modelled is True."""
        result = BacktestResult(
            trades=[_make_trade(1.0) for _ in range(25)],
            equity_curve=_equity_series([float(i) for i in range(26)]),
            metadata={"swap_modelled": True},
        )
        metrics = compute_metrics(result)
        assert metrics.swap_modelled is True


# ---------------------------------------------------------------------------
# 7. Trade-level metrics — win_rate, profit_factor, expectancy
# ---------------------------------------------------------------------------

class TestTradeMetrics:
    """Validate win_rate, profit_factor, avg_win/loss, expectancy."""

    def test_all_winning_trades(self) -> None:
        trades = [_make_trade(5.0) for _ in range(20)]
        result = _make_result(
            trades=trades,
            equity_values=[5.0 * i for i in range(21)],
        )
        metrics = compute_metrics(result)
        assert metrics.win_rate == 1.0
        assert math.isinf(metrics.profit_factor)
        assert abs(metrics.avg_win_pips - 5.0) < 1e-9
        assert metrics.avg_loss_pips == 0.0

    def test_all_losing_trades(self) -> None:
        trades = [_make_trade(-5.0, pnl_pips=-4.0, cost_pips=1.0) for _ in range(20)]
        result = _make_result(
            trades=trades,
            equity_values=[-5.0 * i for i in range(21)],
        )
        metrics = compute_metrics(result)
        assert metrics.win_rate == 0.0
        assert metrics.profit_factor == 0.0
        assert metrics.avg_win_pips == 0.0
        assert abs(metrics.avg_loss_pips - (-5.0)) < 1e-9

    def test_mixed_trades_expectancy(self) -> None:
        """5 wins of +10, 5 losses of -5 → win_rate=0.5, expectancy=+2.5."""
        wins = [_make_trade(10.0) for _ in range(5)]
        losses = [_make_trade(-5.0, pnl_pips=-4.0, cost_pips=1.0) for _ in range(5)]
        trades = wins + losses
        equity_vals = [0.0] + list(range(1, 11))
        result = _make_result(
            trades=trades,
            equity_values=equity_vals,
        )
        metrics = compute_metrics(result)
        assert abs(metrics.win_rate - 0.5) < 1e-9
        assert abs(metrics.expectancy_pips - (0.5 * 10.0 + 0.5 * (-5.0))) < 1e-9
        assert abs(metrics.profit_factor - (50.0 / 25.0)) < 1e-9
