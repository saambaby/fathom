"""ROC Momentum strategy.

Generates LONG/SHORT signals when the rate-of-change of the close price
crosses ±roc_threshold AND a volatility-confirmation gate passes (current
ATR exceeds its rolling mean — range expansion).

INV-03: generated_at = bar close timestamp (UTC-aware).
INV-11: stop_distance = ATR(14) at the signal bar (>0);
        target_distance = stop_distance × rr_ratio (default 1.5).
D-02: DataFrame columns include time (UTC-aware), open_bid, high_bid,
      low_bid, close_bid, volume.

Volatility confirmation:
    current ATR > rolling_mean(ATR, atr_filter_period)

    The gate uses k=1.0 (i.e. strictly above the rolling mean).  A bar
    where ATR == rolling mean does NOT confirm (range-neutral, not expansion).

ROC threshold units: percent (instrument-agnostic).
    roc_threshold=0.005 means ±0.5 % change over roc_period bars.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from strategies._indicators import atr as _atr
from strategies.base import Direction, Signal, Strategy


class ROCMomentum(Strategy):
    """Rate-of-change momentum strategy with ATR volatility-confirmation gate.

    Parameters
    ----------
    instrument:
        OANDA instrument identifier, e.g. ``"EUR_USD"``.
    timeframe:
        Granularity string, e.g. ``"H1"`` or ``"D"``.
    roc_period:
        Number of bars over which ROC is computed.
        ROC = close.pct_change(roc_period).
    roc_threshold:
        Minimum absolute ROC required for a signal (fraction, not percent).
        E.g. 0.005 = 0.5 %.
    atr_filter_period:
        Rolling window (bars) for the ATR rolling mean used in the
        volatility-confirmation gate.  Typical: 20–50.
    atr_stop_period:
        ATR period used for stop_distance (INV-11).  Default 14.
    rr_ratio:
        Reward-to-risk ratio.  target_distance = stop_distance × rr_ratio.
        Default 1.5.
    volatility_filter:
        If True (default), apply the ATR-expansion gate — suppress signals
        when ATR ≤ rolling-mean ATR.  Set False to disable and test the
        gate's impact.
    """

    def __init__(
        self,
        instrument: str,
        timeframe: str,
        roc_period: int,
        roc_threshold: float,
        atr_filter_period: int,
        atr_stop_period: int = 14,
        rr_ratio: float = 1.5,
        volatility_filter: bool = True,
    ) -> None:
        if roc_period < 1:
            raise ValueError(f"roc_period must be >= 1, got {roc_period}")
        if roc_threshold <= 0:
            raise ValueError(f"roc_threshold must be > 0, got {roc_threshold}")
        if atr_filter_period < 1:
            raise ValueError(f"atr_filter_period must be >= 1, got {atr_filter_period}")
        if atr_stop_period < 1:
            raise ValueError(f"atr_stop_period must be >= 1, got {atr_stop_period}")
        if rr_ratio <= 0:
            raise ValueError(f"rr_ratio must be > 0, got {rr_ratio}")

        self._instrument = instrument
        self._timeframe = timeframe
        self._roc_period = roc_period
        self._roc_threshold = roc_threshold
        self._atr_filter_period = atr_filter_period
        self._atr_stop_period = atr_stop_period
        self._rr_ratio = rr_ratio
        self._volatility_filter = volatility_filter

    # ------------------------------------------------------------------
    # Strategy protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return (
            f"ROCMomentum(roc={self._roc_period},"
            f"thr={self._roc_threshold},"
            f"atr_filter={self._atr_filter_period},"
            f"vol_filter={self._volatility_filter})"
        )

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Generate ROC momentum signals with optional volatility gate.

        Parameters
        ----------
        df:
            Candle DataFrame (D-02).  At minimum: ``time`` (UTC-aware),
            ``high_bid``, ``low_bid``, ``close_bid``.

        Returns
        -------
        list[Signal]
            At most one signal per bar.  Returns an empty list when no bar
            in ``df`` satisfies both the momentum and (if enabled) the
            volatility-confirmation criteria.
        """
        if len(df) == 0:
            return []

        work = df.copy()

        # ---- ROC = percentage change of close over roc_period bars ----
        close = work["close_bid"].astype(float)
        roc = close.pct_change(self._roc_period)

        # ---- ATR for stop (INV-11) ----
        atr_stop = _atr(work, self._atr_stop_period)

        # ---- ATR for volatility gate ----
        atr_filter = _atr(work, self._atr_stop_period)
        # Rolling mean uses atr_filter_period; min_periods=1 avoids leading NaN
        atr_rolling_mean = atr_filter.rolling(
            window=self._atr_filter_period, min_periods=1
        ).mean()

        signals: list[Signal] = []

        for i in range(len(work)):
            roc_val = roc.iloc[i]
            atr_stop_val = atr_stop.iloc[i]
            atr_filter_val = atr_filter.iloc[i]
            atr_mean_val = atr_rolling_mean.iloc[i]

            # Skip if any required value is NaN or ATR stop is non-positive
            if (
                pd.isna(roc_val)
                or pd.isna(atr_stop_val)
                or pd.isna(atr_filter_val)
                or pd.isna(atr_mean_val)
                or atr_stop_val <= 0
            ):
                continue

            # ---- Volatility-confirmation gate ----
            # current ATR strictly > rolling mean → range expansion
            volatility_confirmed = (not self._volatility_filter) or (
                atr_filter_val > atr_mean_val
            )
            if not volatility_confirmed:
                continue

            # ---- Momentum gate ----
            if roc_val >= self._roc_threshold:
                direction = Direction.LONG
            elif roc_val <= -self._roc_threshold:
                direction = Direction.SHORT
            else:
                continue  # below threshold — no signal

            # ---- Build signal ----
            stop_distance = float(atr_stop_val)
            target_distance = stop_distance * self._rr_ratio

            # quality_score: normalise excess momentum to [0, 1]
            # excess = |ROC| - threshold; scaled by threshold so 2× threshold = 1.0
            excess = abs(roc_val) - self._roc_threshold
            quality_score = min(1.0, excess / self._roc_threshold)

            # generated_at = bar close timestamp (INV-03)
            bar_time = work["time"].iloc[i]
            if isinstance(bar_time, pd.Timestamp):
                generated_at: datetime = bar_time.to_pydatetime()
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=timezone.utc)
            else:
                generated_at = bar_time

            entry_ref = float(close.iloc[i])

            signals.append(
                Signal(
                    instrument=self._instrument,
                    direction=direction,
                    entry_ref=entry_ref,
                    stop_distance=stop_distance,
                    target_distance=target_distance,
                    strategy_name=self.name,
                    timeframe=self._timeframe,
                    quality_score=quality_score,
                    generated_at=generated_at,
                )
            )

        return signals

    # ----------------------------------------------------------------
    # Make subclass concrete (abc.ABC already enforced via Strategy)
    # ----------------------------------------------------------------
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
