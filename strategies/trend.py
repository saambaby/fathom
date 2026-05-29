"""Trend-following strategies.

MACrossover
-----------
Generates signals on EMA crossovers:
- Golden cross (fast EMA crosses ABOVE slow EMA) → LONG signal
- Death cross  (fast EMA crosses BELOW slow EMA) → SHORT signal

DonchianBreakout
----------------
Generates signals on Donchian channel breakouts:
- Close above the prior channel_period-bar rolling max high → LONG signal
- Close below the prior channel_period-bar rolling min low  → SHORT signal
- No signal while price stays inside the channel.

library_defaults (from poc-taskgraph.md):
  pandas.ewm(span=..., adjust=False)   — recursive EMA formulation.
  Default adjust=True gives a different result to the standard recursive EMA
  used by most charting tools; adjust=False is required here.

INV-03: Signal.generated_at is set to the bar's close timestamp (UTC-aware),
        never datetime.now().
INV-11: ATR is computed via the shared strategies._indicators.atr() helper —
        no per-file ATR copy.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from strategies._indicators import atr as _atr
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

        close = df["close_bid"].astype(float)

        # Recursive EMA (adjust=False matches most charting-tool implementations).
        fast_ema: pd.Series[float] = close.ewm(span=self._fast_period, adjust=False).mean()
        slow_ema: pd.Series[float] = close.ewm(span=self._slow_period, adjust=False).mean()

        # ATR(14) — standard True Range average (shared helper, INV-11).
        atr = _atr(df, self._atr_period)

        signals: list[Signal] = []

        for i in range(1, n):
            # Per-bar warm-up guard: no signal until slow EMA has had at least
            # slow_period bars to converge — matches the old per-prefix path
            # where a prefix of length < slow_period + 1 returned [] entirely.
            if i < self._slow_period:
                continue
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


class DonchianBreakout(Strategy):
    """Donchian channel breakout strategy.

    Produces a signal when price closes outside the prior ``channel_period``-bar
    Donchian channel (the rolling max of highs / rolling min of lows, each
    shifted by 1 bar so the current bar is excluded — no look-ahead).

    Signal rules (close-based):
    - LONG  when ``close > channel_high_prev``  (close breaks above prior channel high)
    - SHORT when ``close < channel_low_prev``   (close breaks below prior channel low)
    - No signal while close stays inside the channel.
    - At most one signal per bar (LONG takes priority if both conditions fire
      simultaneously, which cannot happen in practice for a well-formed channel).

    Parameters
    ----------
    channel_period:
        Look-back in bars for the rolling high/low channel.  Classic values are
        20 (standard Donchian) and 55 (Turtle system).  Must be >= 1.
    rr_ratio:
        Reward-to-risk ratio used to derive ``target_distance`` from
        ``stop_distance``.  Default 1.5 (target = stop * 1.5).
    instrument:
        OANDA instrument identifier (e.g. ``"EUR_USD"``).
    timeframe:
        Granularity string (e.g. ``"H1"`` or ``"D"``).
    atr_period:
        Look-back for the shared ATR calculation.  Default 14 (INV-11).
    """

    def __init__(
        self,
        channel_period: int,
        *,
        rr_ratio: float = 1.5,
        instrument: str = "",
        timeframe: str = "",
        atr_period: int = 14,
    ) -> None:
        if channel_period < 1:
            raise ValueError(f"channel_period must be >= 1, got {channel_period}")
        if rr_ratio <= 0:
            raise ValueError(f"rr_ratio must be > 0, got {rr_ratio}")

        self._channel_period = channel_period
        self._rr_ratio = rr_ratio
        self._instrument = instrument
        self._timeframe = timeframe
        self._atr_period = atr_period

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"DonchianBreakout({self._channel_period})"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Scan the DataFrame for Donchian channel breakouts and return Signals.

        At most one signal is produced per bar.  The DataFrame is never mutated.
        The channel is computed using the prior ``channel_period`` bars only
        (shifted by 1 bar) — no look-ahead.

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
        # Need at least channel_period + 1 rows: channel_period for the rolling
        # window and at least 1 bar beyond to evaluate the breakout.
        if n < self._channel_period + 1:
            return []

        high = df["high_bid"].astype(float)
        low = df["low_bid"].astype(float)
        close = df["close_bid"].astype(float)

        # Rolling channel over exactly channel_period bars, shifted by 1 so that
        # bar i's channel is derived from bars [i-channel_period, i-1] (no look-ahead).
        # min_periods=channel_period ensures we only get a value once the window is full.
        channel_high: pd.Series[float] = (
            high.rolling(self._channel_period, min_periods=self._channel_period)
            .max()
            .shift(1)
        )
        channel_low: pd.Series[float] = (
            low.rolling(self._channel_period, min_periods=self._channel_period)
            .min()
            .shift(1)
        )

        # ATR(14) — standard True Range average (shared helper, INV-11).
        atr_series = _atr(df, self._atr_period)

        signals: list[Signal] = []

        for i in range(1, n):
            ch_high = channel_high.iloc[i]
            ch_low = channel_low.iloc[i]

            # Skip bars where the channel is not yet fully computed (NaN from shift+rolling).
            if pd.isna(ch_high) or pd.isna(ch_low):
                continue

            curr_close = close.iloc[i]

            # Determine breakout direction (close-based, lean from spec).
            if curr_close > ch_high:
                direction = Direction.LONG
            elif curr_close < ch_low:
                direction = Direction.SHORT
            else:
                # Inside the channel — no signal.
                continue

            # stop_distance = ATR(14) at signal bar (INV-11) — must be > 0.
            atr_value = float(atr_series.iloc[i])
            if atr_value <= 0 or pd.isna(atr_value):
                continue

            stop_distance = atr_value
            target_distance = stop_distance * self._rr_ratio

            # quality_score: normalised breakout strength in [0, 1].
            # Measure how far close has moved beyond the channel edge relative to
            # the channel width.  Channel width = ch_high - ch_low.
            #   LONG:  excess = close - ch_high  (how far above the upper rail)
            #   SHORT: excess = ch_low  - close  (how far below the lower rail)
            channel_width = ch_high - ch_low
            if channel_width > 0:
                if direction == Direction.LONG:
                    excess = curr_close - ch_high
                else:
                    excess = ch_low - curr_close
                quality_score = float(min(excess / channel_width, 1.0))
            else:
                quality_score = 0.0

            # generated_at: bar's close timestamp (INV-03 — never datetime.now()).
            bar_time = df["time"].iloc[i]
            if hasattr(bar_time, "to_pydatetime"):
                generated_at: datetime = bar_time.to_pydatetime()
            else:
                generated_at = bar_time

            # Ensure UTC-aware (INV-03).
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)

            entry_ref = float(curr_close)

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
