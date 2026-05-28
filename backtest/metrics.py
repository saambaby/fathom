"""Backtest metrics calculator (POC-T-06).

Computes performance metrics from a :class:`~backtest.engine.BacktestResult`.
All metrics operate on *net* PnL (after costs) — gross figures are available
on individual :class:`~backtest.engine.Trade` objects but are not used here.

Annualisation note
------------------
The Sharpe and Sortino ratios are annualised by multiplying the per-period
ratio by ``√252``.  252 is the conventional number of *trading days* per year
for FX (five-day week, no holidays modelled).  The alternative is 365 (calendar
days).  This divisor is a known source of silent variation between
implementations; it is documented here so any downstream comparison can account
for it.
"""

from __future__ import annotations

import math
import warnings
from typing import Optional

import pandas as pd
from pydantic import BaseModel

from backtest.engine import BacktestResult, Trade


class Metrics(BaseModel):
    """Performance metrics for a single backtest window.

    All fields are computed from *net* PnL (spread + slippage deducted).

    Attributes
    ----------
    sharpe_ratio:
        Annualised Sharpe ratio (mean net return / std × √252).  ``NaN`` when
        std is zero (e.g. flat equity curve).
    sortino_ratio:
        Annualised Sortino ratio (mean / downside-std × √252).  ``NaN`` when
        there are no negative-return bars.
    max_drawdown_pct:
        Largest peak-to-trough drawdown expressed as a percentage of peak
        cumulative equity (negative → 0 % when equity never falls below its
        own peak, i.e. monotonically rising or flat).
    max_drawdown_duration_bars:
        Number of bars in the longest drawdown episode (peak → trough
        inclusive).  0 when no drawdown occurred.
    win_rate:
        Fraction of trades that ended in profit (net PnL > 0).  In [0, 1].
    profit_factor:
        Sum of winning net pips / |sum of losing net pips|.  ``inf`` when
        there are no losing trades; 0.0 when there are no winning trades.
    avg_win_pips:
        Average net pips on winning trades.  0.0 when there are no wins.
    avg_loss_pips:
        Average net pips on losing trades (negative value).  0.0 when there
        are no losses.
    expectancy_pips:
        Expected net pips per trade = ``win_rate × avg_win_pips +
        (1 − win_rate) × avg_loss_pips``.
    trade_count:
        Total number of completed trades in this result.
    swap_modelled:
        Carried through from ``BacktestResult.metadata["swap_modelled"]``
        (INV-06).  ``False`` in the PoC — swap costs are deferred (D-03).
    """

    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_bars: int
    win_rate: float
    profit_factor: float
    avg_win_pips: float
    avg_loss_pips: float
    expectancy_pips: float
    trade_count: int
    swap_modelled: bool


def compute_metrics(
    result: BacktestResult, risk_free_rate: float = 0.0
) -> Metrics:
    """Compute :class:`Metrics` from a completed :class:`BacktestResult`.

    Parameters
    ----------
    result:
        Output of :meth:`BacktestEngine.run`.
    risk_free_rate:
        Per-period (bar) risk-free rate used in the Sharpe / Sortino
        denominators.  Defaults to 0.0 (standard for forex strategies where
        the cash leg earns no yield in the model).

    Returns
    -------
    Metrics
        All fields populated.  If the equity curve is empty (zero bars), all
        ratio fields are NaN / 0 and trade-level fields are 0.

    Warns
    -----
    UserWarning
        If ``result`` contains fewer than 20 completed trades the metrics are
        statistically meaningless at typical confidence levels — a warning is
        emitted.
    """
    trades = result.trades
    trade_count = len(trades)

    if trade_count < 20:
        warnings.warn(
            f"compute_metrics: only {trade_count} trade(s) in result — "
            "fewer than 20 trades; metrics are statistically meaningless.",
            UserWarning,
            stacklevel=2,
        )

    equity = result.equity_curve  # cumulative net pips, UTC DatetimeIndex

    # Per-bar net returns (differences of cumulative equity).
    # An empty or single-bar curve produces an empty returns Series.
    if len(equity) > 1:
        returns: pd.Series = equity.diff().dropna()
    else:
        returns = pd.Series([], dtype="float64")

    sharpe = _sharpe(returns, risk_free_rate)
    sortino = _sortino(returns, risk_free_rate)
    max_dd_pct, max_dd_bars = _max_drawdown(equity)

    wins = [t for t in trades if t.pnl_net_pips > 0]
    losses = [t for t in trades if t.pnl_net_pips <= 0]

    win_rate = len(wins) / trade_count if trade_count > 0 else 0.0

    total_wins = sum(t.pnl_net_pips for t in wins)
    total_losses = sum(t.pnl_net_pips for t in losses)  # negative or zero

    if total_losses != 0.0:
        profit_factor = total_wins / abs(total_losses)
    elif total_wins > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    avg_win_pips = total_wins / len(wins) if wins else 0.0
    avg_loss_pips = total_losses / len(losses) if losses else 0.0

    expectancy_pips = (
        win_rate * avg_win_pips + (1.0 - win_rate) * avg_loss_pips
        if trade_count > 0
        else 0.0
    )

    swap_modelled = bool(result.metadata.get("swap_modelled", False))

    return Metrics(
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_bars=max_dd_bars,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win_pips=avg_win_pips,
        avg_loss_pips=avg_loss_pips,
        expectancy_pips=expectancy_pips,
        trade_count=trade_count,
        swap_modelled=swap_modelled,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ANNUALISE = math.sqrt(252)
# Annualisation factor for per-bar (daily) returns → annual.
# 252 = conventional trading days per year for FX (5-day week, no holidays).
# Alternative: 365 (calendar days) — not used here to match the PoC spec.


def _sharpe(returns: pd.Series, risk_free_rate: float) -> float:
    """Annualised Sharpe ratio = (mean excess return / std) × √252.

    Returns ``float('nan')`` when the standard deviation is zero (no
    variation in returns — flat equity curve).
    """
    if len(returns) == 0:
        return float("nan")
    excess = returns - risk_free_rate
    mean = float(excess.mean())
    std = float(excess.std(ddof=1)) if len(excess) > 1 else 0.0
    if std == 0.0:
        return float("nan")
    # Annualise: per-period ratio × √252  (252 trading days per year, FX)
    return (mean / std) * _ANNUALISE


def _sortino(returns: pd.Series, risk_free_rate: float) -> float:
    """Annualised Sortino ratio = (mean excess return / downside-std) × √252.

    Downside std uses only negative excess returns.  Returns ``float('nan')``
    when there are no negative returns (no downside variation).
    """
    if len(returns) == 0:
        return float("nan")
    excess = returns - risk_free_rate
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("nan")
    mean = float(excess.mean())
    downside_std = float((downside**2).mean() ** 0.5)  # root-mean-square of losses
    if downside_std == 0.0:
        return float("nan")
    # Annualise: per-period ratio × √252  (252 trading days per year, FX)
    return (mean / downside_std) * _ANNUALISE


def _max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """Compute maximum drawdown as (pct, bar_count).

    Parameters
    ----------
    equity:
        Cumulative net pips equity curve.

    Returns
    -------
    (max_drawdown_pct, max_drawdown_duration_bars)
        ``max_drawdown_pct`` is the largest peak-to-trough drop expressed as a
        percentage of the peak value (negative number; 0.0 if no drawdown).
        ``max_drawdown_duration_bars`` is the number of bars from the peak bar
        to the trough bar of the worst drawdown episode (inclusive, so a
        single-bar drop is 1).  0 when no drawdown occurred.

    Notes
    -----
    Duration is measured **peak to trough** (not peak to recovery).  This
    follows the conventional reporting convention used in most performance
    analytics toolkits.

    Drawdown percentage is computed relative to the running peak.  When the
    peak cumulative equity is 0 (or negative — the account started losing from
    bar 1), the pct is not meaningful; we fall back to 0.0 in that edge case
    to avoid division by zero.
    """
    if len(equity) == 0:
        return 0.0, 0

    values = equity.to_numpy(dtype=float)
    n = len(values)

    max_dd_pct = 0.0
    max_dd_bars = 0

    # Scan forward, tracking the running peak index.
    peak_idx = 0
    for i in range(n):
        # Update peak if we have a new high.
        if values[i] > values[peak_idx]:
            peak_idx = i
            continue  # can't be below the new peak on the same bar

        if values[i] < values[peak_idx]:
            # We are below the current peak.
            peak_val = values[peak_idx]
            trough_val = values[i]
            bars = i - peak_idx + 1  # inclusive: peak bar … trough bar

            if peak_val != 0.0:
                dd_pct = (trough_val - peak_val) / abs(peak_val) * 100.0
            else:
                dd_pct = 0.0

            if dd_pct < max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_bars = bars  # bars from peak bar to trough bar (inclusive)

    return max_dd_pct, max_dd_bars
