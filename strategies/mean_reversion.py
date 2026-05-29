"""Mean-reversion strategies.

BollingerReversion
------------------
Generates signals when the close price's z-score relative to a rolling SMA
exceeds a configurable threshold (num_std standard deviations):
- LONG  when z-score ≤ −num_std (lower band breach, oversold stretch)
- SHORT when z-score ≥ +num_std (upper band breach, overbought stretch)
- No signal while price is within the bands.

Rolling std uses sample std (ddof=1) — classic Bollinger convention.
Band centre uses SMA (simple moving average) — chosen over EMA per spec lean.

RSIReversion
------------
Generates signals on RSI cross-outs of extreme zones:
- LONG  on RSI crossing ABOVE oversold threshold (exit oversold zone)
- SHORT on RSI crossing BELOW overbought threshold (exit overbought zone)
- No signal while RSI is mid-range.

RSI uses Wilder's smoothing: ewm(com=period-1, adjust=False) on gains/losses —
the same formulation as the shared ATR helper (strategies._indicators).

library_defaults:
  pandas.rolling(period, min_periods=period).std(ddof=1) — sample std (Bollinger).
  pandas.ewm(com=period-1, adjust=False) — Wilder's recursive smoothing (RSI + ATR).

INV-03: Signal.generated_at is set to the bar's close timestamp (UTC-aware),
        never datetime.now().
INV-11: stop_distance = ATR(14) via strategies._indicators.atr(); no per-file ATR copy.
        target_distance = stop_distance × rr_ratio (default 1.5). Fixed RR — no
        band-midline target (both strategies).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from strategies._indicators import atr as _atr
from strategies.base import Direction, Signal, Strategy


# ---------------------------------------------------------------------------
# BollingerReversion
# ---------------------------------------------------------------------------


class BollingerReversion(Strategy):
    """Bollinger-band / z-score mean-reversion strategy.

    Signals fire when the close price's z-score breaches the configurable
    band threshold in either direction.  No signal is emitted while price
    remains within the bands.

    Parameters
    ----------
    period:
        Rolling window for SMA (band centre) and sample std (band width).
    num_std:
        Band width in standard deviations (also the z-score threshold).
        Default 2.0.
    rr_ratio:
        Reward-to-risk ratio: target_distance = stop_distance × rr_ratio.
        Default 1.5 (fixed per INV-11 — no band-midline target).
    instrument:
        OANDA instrument identifier (e.g. ``"EUR_USD"``).
    timeframe:
        Granularity string (e.g. ``"H1"`` or ``"D"``).
    """

    def __init__(
        self,
        period: int,
        num_std: float = 2.0,
        *,
        rr_ratio: float = 1.5,
        instrument: str = "",
        timeframe: str = "",
    ) -> None:
        if period < 2:
            raise ValueError(f"period must be >= 2 (need ≥2 for sample std), got {period}")
        if num_std <= 0:
            raise ValueError(f"num_std must be > 0, got {num_std}")
        if rr_ratio <= 0:
            raise ValueError(f"rr_ratio must be > 0, got {rr_ratio}")

        self._period = period
        self._num_std = num_std
        self._rr_ratio = rr_ratio
        self._instrument = instrument
        self._timeframe = timeframe

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"BollingerReversion({self._period},{self._num_std})"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Scan for Bollinger z-score breaches and return Signals.

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
        # Need at least period rows to compute rolling SMA/std.
        if n < self._period:
            return []

        close = df["close_bid"].astype(float)

        # SMA band centre (classic Bollinger — explicit over EMA per spec)
        sma: pd.Series = close.rolling(self._period, min_periods=self._period).mean()
        # Sample std (ddof=1) — Bollinger convention
        rolling_std: pd.Series = close.rolling(self._period, min_periods=self._period).std(ddof=1)

        # ATR(14) — shared helper (INV-11)
        atr_series = _atr(df, 14)

        signals: list[Signal] = []

        for i in range(n):
            mean_val = sma.iloc[i]
            std_val = rolling_std.iloc[i]

            # Skip bars where rolling window is not yet full or std is zero/NaN
            if pd.isna(mean_val) or pd.isna(std_val) or std_val == 0.0:
                continue

            close_val = close.iloc[i]
            z_score = (close_val - mean_val) / std_val

            # Determine direction: breach threshold → signal; within bands → skip
            if z_score <= -self._num_std:
                direction = Direction.LONG
            elif z_score >= self._num_std:
                direction = Direction.SHORT
            else:
                # Within bands — no signal
                continue

            # stop_distance = ATR(14) at signal bar (INV-11, must be > 0)
            atr_value = float(atr_series.iloc[i])
            if atr_value <= 0 or pd.isna(atr_value):
                continue

            stop_distance = atr_value
            target_distance = stop_distance * self._rr_ratio  # fixed RR (INV-11)

            # quality_score: how far the z-score penetrated beyond the threshold, clamped [0,1].
            # excess = |z| − num_std; normalise by num_std so one extra std → 1.0.
            excess = abs(z_score) - self._num_std
            quality_score = float(min(excess / self._num_std, 1.0)) if self._num_std != 0 else 0.0

            # generated_at: bar's close timestamp (INV-03 — never datetime.now())
            bar_time = df["time"].iloc[i]
            if hasattr(bar_time, "to_pydatetime"):
                generated_at: datetime = bar_time.to_pydatetime()
            else:
                generated_at = bar_time

            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)

            entry_ref = float(close_val)

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


# ---------------------------------------------------------------------------
# RSIReversion
# ---------------------------------------------------------------------------


class RSIReversion(Strategy):
    """RSI-extremes mean-reversion strategy.

    Signals fire on RSI cross-outs of the oversold / overbought zones:
    - LONG  when RSI crosses back above ``oversold`` (exits the oversold zone).
    - SHORT when RSI crosses back below ``overbought`` (exits the overbought zone).

    Cross-out trigger (not mere level) avoids repeated signals while RSI is
    pinned deep in the extreme zone.

    RSI uses Wilder's smoothing: ``ewm(com=period-1, adjust=False)`` on
    average gains / average losses — the same formulation as the shared ATR
    and the PoC ``_compute_atr``.

    Parameters
    ----------
    period:
        RSI look-back.  Default 14 (standard Wilder RSI).
    oversold:
        Lower RSI threshold.  Default 30.
    overbought:
        Upper RSI threshold.  Default 70.
    rr_ratio:
        Reward-to-risk ratio: target_distance = stop_distance × rr_ratio.
        Default 1.5 (fixed per INV-11 — no band-midline target).
    instrument:
        OANDA instrument identifier (e.g. ``"EUR_USD"``).
    timeframe:
        Granularity string (e.g. ``"H1"`` or ``"D"``).
    """

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        *,
        rr_ratio: float = 1.5,
        instrument: str = "",
        timeframe: str = "",
    ) -> None:
        if period < 1:
            raise ValueError(f"period must be >= 1, got {period}")
        if not (0 <= oversold < overbought <= 100):
            raise ValueError(
                f"oversold ({oversold}) must be < overbought ({overbought}), "
                f"both in [0, 100]"
            )
        if rr_ratio <= 0:
            raise ValueError(f"rr_ratio must be > 0, got {rr_ratio}")

        self._period = period
        self._oversold = oversold
        self._overbought = overbought
        self._rr_ratio = rr_ratio
        self._instrument = instrument
        self._timeframe = timeframe

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"RSIReversion({self._period},{self._oversold},{self._overbought})"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Scan for RSI cross-outs of extreme zones and return Signals.

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
        # Need at least period + 1 rows: period for the first RSI value, +1 for a crossover.
        if n < self._period + 1:
            return []

        close = df["close_bid"].astype(float)
        delta = close.diff()

        # Separate gains and losses (no NaN on first row after diff; it IS NaN — that's fine,
        # ewm with adjust=False initialises from the first non-NaN value)
        gains = delta.clip(lower=0.0)
        losses = (-delta).clip(lower=0.0)

        # Wilder's smoothing: ewm(com=period-1, adjust=False)  — same family as ATR
        avg_gain = gains.ewm(com=self._period - 1, adjust=False).mean()
        avg_loss = losses.ewm(com=self._period - 1, adjust=False).mean()

        # RSI: avoid division by zero when avg_loss == 0
        rsi: pd.Series = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss.replace(0, float("inf"))))
        # Row 0 of delta is NaN (diff() has no predecessor); gains/losses[0] propagate NaN
        # through ewm, making avg_gain[0]/avg_loss[0] NaN → rsi[0] NaN. Force that to stay
        # NaN so the cross-out check (float(NaN) >= overbought) evaluates False — no spurious
        # signal on bar 1.  Only when avg_gain is genuinely zero (not NaN) is RSI 100.
        rsi = rsi.where(avg_gain.notna(), other=float("nan"))

        # ATR(14) — shared helper (INV-11)
        atr_series = _atr(df, 14)

        signals: list[Signal] = []

        for i in range(1, n):
            prev_rsi = float(rsi.iloc[i - 1])
            curr_rsi = float(rsi.iloc[i])

            # Cross-out of oversold zone: was ≤ oversold, now > oversold → LONG
            if prev_rsi <= self._oversold < curr_rsi:
                direction = Direction.LONG
            # Cross-out of overbought zone: was ≥ overbought, now < overbought → SHORT
            elif prev_rsi >= self._overbought > curr_rsi:
                direction = Direction.SHORT
            else:
                # No cross-out — skip
                continue

            # stop_distance = ATR(14) at signal bar (INV-11, must be > 0)
            atr_value = float(atr_series.iloc[i])
            if atr_value <= 0 or pd.isna(atr_value):
                continue

            stop_distance = atr_value
            target_distance = stop_distance * self._rr_ratio  # fixed RR (INV-11)

            # quality_score: depth of the extreme before reverting, normalised to [0,1].
            # Use how far prev_rsi penetrated into the zone relative to the zone width.
            # For LONG: zone width = oversold (from 0); depth = oversold - prev_rsi.
            # For SHORT: zone width = 100 - overbought; depth = prev_rsi - overbought.
            if direction == Direction.LONG:
                zone_width = self._oversold  # range [0, oversold]
                depth = max(self._oversold - prev_rsi, 0.0)
            else:
                zone_width = 100.0 - self._overbought  # range [overbought, 100]
                depth = max(prev_rsi - self._overbought, 0.0)

            quality_score = float(min(depth / zone_width, 1.0)) if zone_width > 0 else 0.0

            # generated_at: bar's close timestamp (INV-03 — never datetime.now())
            bar_time = df["time"].iloc[i]
            if hasattr(bar_time, "to_pydatetime"):
                generated_at: datetime = bar_time.to_pydatetime()
            else:
                generated_at = bar_time

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
