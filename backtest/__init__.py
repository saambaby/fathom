"""Fathom backtest package.

Contains the event-driven backtest engine and the cost model.

- ``costs``  : ``apply_costs`` + ``CostResult`` + ``CostParams`` (spread + slippage; swap deferred, D-03).
- ``engine`` : ``BacktestEngine`` + ``Trade`` + ``BacktestResult`` — strict chronological,
               no look-ahead, defensive-copy of the caller's DataFrame.
"""

from backtest.costs import CostParams, CostResult, apply_costs
from backtest.engine import BacktestEngine, BacktestResult, Trade

__all__ = [
    "CostParams",
    "CostResult",
    "apply_costs",
    "BacktestEngine",
    "BacktestResult",
    "Trade",
]
