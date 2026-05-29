"""Walk-forward validation engine (POC-T-06).

Splits a historical data range into a series of rolling train/test windows and
runs the :class:`~backtest.engine.BacktestEngine` on each.  In-sample metrics
(on the training window) and out-of-sample metrics (on the test window) are
computed for each window.  When all windows pass the approval criteria the
strategy is added to the approved set; otherwise ``approved_set_entry`` is
``None``.

Window layout (default: train_months=12, test_months=3, step=test_months):

    start        end
    |<-12m train->|<-3m test->|
                  |<-12m train->|<-3m test->|
                               ...

With 2 years of data this yields 5 test windows covering months 13–15, 16–18,
19–21, 22–24, 25–27 (the 25th month wraps within the data, so the last window
may be shorter).

Approval criteria
-----------------
``approved_set_entry`` is non-None **only when all of**:

* Every OOS window individually has ``sharpe_ratio > 0`` (not NaN).
* Every OOS window individually has ``trade_count ≥ 5``.

An empty approved set is a valid, non-error result — the caller must handle it
gracefully (INV-10: no strategy runs live without a passed validation).

INV-06 swap label propagation
---------------------------------
``ApprovedSetEntry.swap_modelled`` is sourced unchanged from the underlying
``BacktestResult.metadata`` of the most recent OOS window — ``True`` when
financing rates were supplied (the normal Phase-1 path, P1A-T-03), ``False``
only on a spread-only run.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel

from backtest.engine import BacktestEngine
from backtest.metrics import Metrics, compute_metrics
from strategies.base import Strategy


class WindowResult(BaseModel):
    """Metrics for one train/test split.

    Attributes
    ----------
    train_start, train_end:
        Inclusive bounds of the training window (UTC-aware).
    test_start, test_end:
        Inclusive bounds of the test (out-of-sample) window (UTC-aware).
    in_sample_metrics:
        Metrics computed on the training window backtest.
    out_of_sample_metrics:
        Metrics computed on the test window backtest.
    """

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    in_sample_metrics: Metrics
    out_of_sample_metrics: Metrics


class ApprovedSetEntry(BaseModel):
    """A strategy × instrument × granularity combination that passed walk-forward.

    Attributes
    ----------
    instrument, granularity, strategy_name:
        Identifiers for the combination that was validated.
    oos_sharpe_mean:
        Average OOS Sharpe ratio across all test windows.
    oos_trade_count_total:
        Sum of OOS trade counts across all test windows.
    swap_modelled:
        Propagated unchanged from the underlying backtest metadata (INV-06).
        ``True`` when financing was modelled (the normal Phase-1 path,
        P1A-T-03); ``False`` only on a spread-only run.
    """

    instrument: str
    granularity: str
    strategy_name: str
    oos_sharpe_mean: float
    oos_trade_count_total: int
    swap_modelled: bool


class WalkForwardResult(BaseModel):
    """Result of a complete walk-forward run.

    Attributes
    ----------
    windows:
        Ordered list of :class:`WindowResult` objects (one per test window).
    approved_set_entry:
        Non-None iff the strategy passed all approval criteria across all OOS
        windows.  ``None`` is a valid, non-error result.
    """

    windows: list[WindowResult]
    approved_set_entry: Optional[ApprovedSetEntry]


class WalkForwardValidator:
    """Rolling walk-forward validator.

    Parameters
    ----------
    engine:
        A configured :class:`~backtest.engine.BacktestEngine` (already
        initialised with a :class:`~data.store.Store` and
        :class:`~backtest.costs.CostParams`).
    strategy:
        The strategy instance to validate.
    """

    def __init__(self, engine: BacktestEngine, strategy: Strategy) -> None:
        self._engine = engine
        self._strategy = strategy

    def run(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
        train_months: int = 12,
        test_months: int = 3,
    ) -> WalkForwardResult:
        """Execute the walk-forward validation.

        Parameters
        ----------
        instrument, granularity:
            Passed directly to the underlying engine.
        start, end:
            UTC-aware bounds of the full historical range to split.
        train_months:
            Length of each training window in calendar months.
        test_months:
            Length of each test window in calendar months; also the step size
            (the windows advance by ``test_months`` at each iteration).

        Returns
        -------
        WalkForwardResult
            Contains all :class:`WindowResult` objects and, when the strategy
            passes, an :class:`ApprovedSetEntry`.  Returns a result with an
            empty ``windows`` list and ``approved_set_entry=None`` when the
            date range is too short to form a single window — this is not an
            error.
        """
        windows: list[WindowResult] = []

        train_delta = relativedelta(months=train_months)
        test_delta = relativedelta(months=test_months)

        window_start = start
        while True:
            train_start = window_start
            train_end = window_start + train_delta
            test_start = train_end
            test_end = test_start + test_delta

            # Stop if the test window exceeds the available data.
            if test_end > end:
                break

            # Run in-sample backtest on the training window.
            is_result = self._engine.run(
                self._strategy, instrument, granularity, train_start, train_end
            )
            is_metrics = compute_metrics(is_result)

            # Run out-of-sample backtest on the test window.
            oos_result = self._engine.run(
                self._strategy, instrument, granularity, test_start, test_end
            )
            oos_metrics = compute_metrics(oos_result)

            windows.append(
                WindowResult(
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    in_sample_metrics=is_metrics,
                    out_of_sample_metrics=oos_metrics,
                )
            )

            # Step the window forward by one test period.
            window_start = window_start + test_delta

        approved_set_entry = self._evaluate_approval(
            windows, instrument, granularity
        )

        return WalkForwardResult(
            windows=windows,
            approved_set_entry=approved_set_entry,
        )

    def _evaluate_approval(
        self,
        windows: list[WindowResult],
        instrument: str,
        granularity: str,
    ) -> Optional[ApprovedSetEntry]:
        """Return an :class:`ApprovedSetEntry` iff approval criteria are met.

        Both criteria are evaluated **per window** — every individual OOS window
        must satisfy each gate independently.  A strategy that passes in the
        aggregate but fails in any single window is rejected.

        Criteria (per window):
        1. Every OOS window has ``sharpe_ratio > 0`` (not NaN).
        2. Every OOS window has ``trade_count >= 5``.

        Returns ``None`` when windows is empty (not enough data) or when any
        criterion is not satisfied.  An empty approved set is a valid, non-error
        result.
        """
        if not windows:
            return None

        oos_metrics = [w.out_of_sample_metrics for w in windows]

        # Criterion 1: every OOS window must have a positive (non-NaN) Sharpe.
        for m in oos_metrics:
            if math.isnan(m.sharpe_ratio) or m.sharpe_ratio <= 0.0:
                return None

        # Criterion 2: every OOS window must have at least 5 trades.
        # Per-window gate — a strategy that spreads 5 trades across many windows
        # (e.g. 1 per window) does NOT satisfy this criterion.
        if any(m.trade_count < 5 for m in oos_metrics):
            return None

        oos_sharpe_mean = sum(m.sharpe_ratio for m in oos_metrics) / len(
            oos_metrics
        )
        total_trades = sum(m.trade_count for m in oos_metrics)

        # swap_modelled propagated from the last OOS window's backtest metadata
        # (INV-06).  True when financing was modelled (P1A-T-03).
        swap_modelled = oos_metrics[-1].swap_modelled

        return ApprovedSetEntry(
            instrument=instrument,
            granularity=granularity,
            strategy_name=self._strategy.name,
            oos_sharpe_mean=oos_sharpe_mean,
            oos_trade_count_total=total_trades,
            swap_modelled=swap_modelled,
        )
