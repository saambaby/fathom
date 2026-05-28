"""Trend-following strategies.

MACrossover
-----------
Generates signals on EMA crossovers:
- Golden cross (fast EMA crosses ABOVE slow EMA) → LONG signal
- Death cross  (fast EMA crosses BELOW slow EMA) → SHORT signal

library_defaults (from poc-taskgraph.md):
  pandas.ewm(span=..., adjust=False)   — recursive EMA formulation.
  Default adjust=True gives a different result to the standard recursive EMA
  used by most charting tools; adjust=False is required here.

INV-03: Signal.generated_at is set to the bar's close timestamp (UTC-aware),
        never datetime.now().
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from strategies.base import Direction, Signal, Strategy


class MACrossover(Strategy):
    """EMA crossover strategy.

    Produces one signal per bar on a crossover event:
    - LONG  on golden cross (fast EMA crosses above slow EMA)
    - SHORT on death cross  (fast EMA crosses below slow EMA)

    Parameters
    ----------
    fast_period:
        Span for the fast EMA (e.g. 10, 20).
    slow_period:
        Span for the slow EMA (e.g. 50, 100, 200).
    rr_ratio:
        Reward-to-risk ratio used to derive target_distance from stop_distance.
        Default 1.5 (i.e. target = stop * 1.5).
    instrument:
        OANDA instrument identifier (e.g. ``"EUR_USD"``).
    timeframe:
        Granularity string (e.g. ``"H1"`` or ``"D"``).
    atr_period:
        Look-back for ATR calculation. Default 14.
    """

    def __init__(
        self,
        fast_period: int,
        slow_period: int,
        *,
        rr_ratio: float = 1.5,
        instrument: str = "",
        timeframe: str = "",
        atr_period: int = 14,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be < slow_period ({slow_period})"
            )
        if fast_period < 1 or slow_period < 1:
            raise ValueError("Both periods must be >= 1")
        if rr_ratio <= 0:
            raise ValueError(f"rr_ratio must be > 0, got {rr_ratio}")

        self._fast_period = fast_period
        self._slow_period = slow_period
        self._rr_ratio = rr_ratio
        self._instrument = instrument
        self._timeframe = timeframe
        self._atr_period = atr_period

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"MACrossover({self._fast_period},{self._slow_period})"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Scan the DataFrame for EMA crossover events and return Signals.

        At most one signal is produced per bar.  The DataFrame is never mutated.

        Parameters
        ----------
        df:
            Candle data with columns: ``time``, ``high_bid``, ``low_bid``,
            ``close_bid`` (minimum required).  ``time`` must be UTC-aware.

        Returns
        -------
        list[Signal]
        """
        df = df.copy()  # defensive copy — never mutate the caller's DataFrame

        required = {"time", "high_bid", "low_bid", "close_bid"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required columns: {missing}")

        n = len(df)
        # Need at least slow_period rows to produce a meaningful EMA, plus 1
        # for the previous-bar comparison.
        if n < self._slow_period + 1:
            return []

        close = df["close_bid"].astype(float)

        # Recursive EMA (adjust=False matches most charting-tool implementations).
        fast_ema: pd.Series[float] = close.ewm(span=self._fast_period, adjust=False).mean()
        slow_ema: pd.Series[float] = close.ewm(span=self._slow_period, adjust=False).mean()

        # ATR(14) — standard True Range average.
        atr = self._compute_atr(df, self._atr_period)

        signals: list[Signal] = []

        for i in range(1, n):
            prev_fast = fast_ema.iloc[i - 1]
            prev_slow = slow_ema.iloc[i - 1]
            curr_fast = fast_ema.iloc[i]
            curr_slow = slow_ema.iloc[i]

            # Detect crossover
            was_above = prev_fast > prev_slow
            is_above = curr_fast > curr_slow

            if was_above == is_above:
                # No crossover on this bar — skip
                continue

            # Determine direction
            direction = Direction.LONG if is_above else Direction.SHORT

            # stop_distance = ATR(14) at signal bar — must be > 0
            atr_value = float(atr.iloc[i])
            if atr_value <= 0 or pd.isna(atr_value):
                # Not enough history to compute ATR — skip
                continue

            stop_distance = atr_value
            target_distance = stop_distance * self._rr_ratio

            # quality_score: normalised EMA separation at crossover (0-1).
            # Use the ratio of |fast - slow| to slow EMA as a relative measure,
            # then clamp to [0, 1].
            separation = abs(curr_fast - curr_slow)
            quality_score = float(min(separation / slow_ema.iloc[i], 1.0)) if slow_ema.iloc[i] != 0 else 0.0

            # generated_at: bar's close timestamp (INV-03 — never datetime.now()).
            bar_time = df["time"].iloc[i]
            if hasattr(bar_time, "to_pydatetime"):
                generated_at: datetime = bar_time.to_pydatetime()
            else:
                generated_at = bar_time

            # Ensure UTC-aware (INV-03)
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series[float]:
        """Compute Average True Range using Wilder's smoothing (ewm, adjust=False).

        True Range = max(
            high - low,
            |high - prev_close|,
            |low  - prev_close|
        )
        """
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

        # Wilder's ATR: ewm with com = period - 1 (equiv. alpha = 1/period), adjust=False
        atr: pd.Series[float] = tr.ewm(com=period - 1, adjust=False).mean()
        return atr
