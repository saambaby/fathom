"""Session/range breakout strategy.

SessionRangeBreakout
--------------------
Generates signals when price closes beyond a reference range:
- LONG  when close breaks ABOVE the prior N-bar rolling max high + buffer
- SHORT when close breaks BELOW the prior N-bar rolling min low  − buffer

The "reference range" is computed as a rolling look-back:
  rolling_high[i] = max(high_bid[i-range_lookback : i])   (excludes current bar)
  rolling_low[i]  = min(low_bid[i-range_lookback : i])    (excludes current bar)

Once-per-day latch (INV-03 / spec):
  Within a UTC day, once a LONG signal fires the LONG latch is closed for that
  day; the SHORT latch is independent.  Latches reset at UTC midnight.

Stop / target (INV-11):
  stop_distance  = ATR(14) at signal bar via strategies._indicators.atr()
  target_distance = stop_distance × rr_ratio   (default 1.5)

Quality score ∈ [0, 1]:
  Normalised break distance = (close − range_edge) / atr_value, clamped to [0, 1].
  Larger breaks relative to ATR score higher.

INV-03:
  All day grouping is by UTC date.  generated_at = bar close timestamp (UTC-aware).
  No local-time assumptions.

library_defaults:
  pandas.rolling() with min_periods=range_lookback (require full window).
  shift(1) to produce a strictly-prior-bar range (no look-ahead).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from strategies._indicators import atr as _atr
from strategies.base import Direction, Signal, Strategy


class SessionRangeBreakout(Strategy):
    """Rolling N-bar range breakout strategy.

    Computes a rolling high/low over the prior ``range_lookback`` bars (look-ahead
    free via ``shift(1)``) and signals on the first close that clears the range
    edge (plus optional buffer) for each direction within a UTC day.

    Parameters
    ----------
    range_lookback:
        Number of bars used to define the reference range (must be >= 1).
        The range is built from bars *before* the signal bar (no look-ahead).
    buffer_pips:
        Optional price distance added to the range edge before signalling.
        Filters marginal breaks.  Default 0.0.
    rr_ratio:
        Reward-to-risk ratio for target_distance.  Default 1.5.
    instrument:
        OANDA instrument identifier (e.g. ``"EUR_USD"``).
    timeframe:
        Granularity string (e.g. ``"H1"`` or ``"D"``).

    Notes
    -----
    ATR period is unconditionally **14** (INV-11 mandates this so stops are
    comparable across strategies for ranking and position-sizing).  The period
    is not a constructor parameter to prevent callers from producing a
    non-conforming stop.
    """

    def __init__(
        self,
        range_lookback: int,
        *,
        buffer_pips: float = 0.0,
        rr_ratio: float = 1.5,
        instrument: str = "",
        timeframe: str = "",
    ) -> None:
        if range_lookback < 1:
            raise ValueError(f"range_lookback must be >= 1, got {range_lookback}")
        if buffer_pips < 0:
            raise ValueError(f"buffer_pips must be >= 0, got {buffer_pips}")
        if rr_ratio <= 0:
            raise ValueError(f"rr_ratio must be > 0, got {rr_ratio}")

        self._range_lookback = range_lookback
        self._buffer_pips = buffer_pips
        self._rr_ratio = rr_ratio
        self._instrument = instrument
        self._timeframe = timeframe

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"SessionRangeBreakout(lookback={self._range_lookback},buf={self._buffer_pips})"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Scan the DataFrame for range breakout events and return Signals.

        Applies a once-per-UTC-day-per-direction latch so only the first
        qualifying breakout in each direction fires per day.  At most one
        signal per bar (LONG and SHORT cannot both fire on the same bar).

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
        # Need at least range_lookback + 1 bars: range_lookback to build the
        # reference range and 1 bar to signal against it.
        if n < self._range_lookback + 1:
            return []

        high = df["high_bid"].astype(float)
        low = df["low_bid"].astype(float)
        close = df["close_bid"].astype(float)

        # Rolling range (prior bars only — shift(1) removes look-ahead).
        # min_periods=range_lookback ensures NaN until a full window is available.
        rolling_high: pd.Series[float] = (
            high.shift(1)
            .rolling(self._range_lookback, min_periods=self._range_lookback)
            .max()
        )
        rolling_low: pd.Series[float] = (
            low.shift(1)
            .rolling(self._range_lookback, min_periods=self._range_lookback)
            .min()
        )

        # ATR(14) for stop/target (INV-11 — period unconditionally 14).
        atr_series = _atr(df, 14)

        # Per-UTC-day latches: track which directions have already fired today.
        # Key = UTC date string; Value = set of Direction strings already fired.
        day_latches: dict[str, set[Direction]] = {}

        signals: list[Signal] = []

        for i in range(n):
            range_high = rolling_high.iloc[i]
            range_low = rolling_low.iloc[i]

            # Skip bars where the reference range is not yet available.
            if pd.isna(range_high) or pd.isna(range_low):
                continue

            close_val = float(close.iloc[i])
            atr_val = float(atr_series.iloc[i])

            if atr_val <= 0 or pd.isna(atr_val):
                # ATR not yet stable — skip (INV-11 requires stop_distance > 0).
                continue

            # UTC date key for the latch (INV-03).
            bar_time = df["time"].iloc[i]
            if hasattr(bar_time, "to_pydatetime"):
                generated_at: datetime = bar_time.to_pydatetime()
            else:
                generated_at = bar_time

            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)

            utc_day = generated_at.strftime("%Y-%m-%d")
            latched = day_latches.setdefault(utc_day, set())

            # Determine breakout direction (LONG takes priority when both edges
            # would be broken simultaneously — edge case on very wide bars).
            direction: Direction | None = None

            long_threshold = float(range_high) + self._buffer_pips
            short_threshold = float(range_low) - self._buffer_pips

            if close_val > long_threshold and Direction.LONG not in latched:
                direction = Direction.LONG
            elif close_val < short_threshold and Direction.SHORT not in latched:
                direction = Direction.SHORT

            if direction is None:
                continue

            # Fire the signal and latch this direction for today.
            latched.add(direction)

            stop_distance = atr_val
            target_distance = stop_distance * self._rr_ratio

            # Quality score: normalised break magnitude relative to ATR, clamped [0, 1].
            if direction == Direction.LONG:
                break_distance = close_val - long_threshold
            else:
                break_distance = short_threshold - close_val
            quality_score = float(min(max(break_distance / atr_val, 0.0), 1.0))

            signals.append(
                Signal(
                    instrument=self._instrument,
                    direction=direction,
                    entry_ref=close_val,
                    stop_distance=stop_distance,
                    target_distance=target_distance,
                    strategy_name=self.name,
                    timeframe=self._timeframe,
                    quality_score=quality_score,
                    generated_at=generated_at,
                )
            )

        return signals
