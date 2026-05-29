"""Fathom backtest package.

Contains the event-driven backtest engine, cost model, metrics calculator, and
walk-forward validation engine.

- ``costs``       : ``apply_costs`` + ``CostResult`` + ``CostParams`` (spread + slippage + commission + swap; all four INV-06 cost categories).
- ``engine``      : ``BacktestEngine`` + ``Trade`` + ``BacktestResult`` — strict chronological,
                    no look-ahead, defensive-copy of the caller's DataFrame.
- ``metrics``     : ``compute_metrics`` + ``Metrics`` — Sharpe, Sortino, max drawdown, win rate, etc.
- ``walkforward`` : ``WalkForwardValidator`` + ``WalkForwardResult`` + ``WindowResult`` + ``ApprovedSetEntry``.
"""

from backtest.costs import CostParams, CostResult, apply_costs
from backtest.engine import BacktestEngine, BacktestResult, Trade
from backtest.metrics import Metrics, compute_metrics
from backtest.walkforward import (
    ApprovedSetEntry,
    WalkForwardResult,
    WalkForwardValidator,
    WindowResult,
)

__all__ = [
    "CostParams",
    "CostResult",
    "apply_costs",
    "BacktestEngine",
    "BacktestResult",
    "Trade",
    "Metrics",
    "compute_metrics",
    "ApprovedSetEntry",
    "WalkForwardResult",
    "WalkForwardValidator",
    "WindowResult",
]
