"""Strategy interface definitions.

Defines the core abstractions used by all strategy implementations:
- Direction: LONG / SHORT / FLAT
- Signal: pydantic model representing a single trading signal
- Strategy: abstract base class for all strategies

INV-03: Signal.generated_at must be UTC-aware (the bar's close timestamp, not datetime.now()).
"""

from __future__ import annotations

import abc
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd
from pydantic import BaseModel, field_validator


class Direction(str, Enum):
    """Trade direction for a signal."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Signal(BaseModel):
    """A single trading signal produced by a strategy.

    Fields
    ------
    instrument      : OANDA instrument identifier, e.g. ``"EUR_USD"``
    direction       : LONG, SHORT, or FLAT
    entry_ref       : Reference price at the signal bar (e.g. bar close)
    stop_distance   : Distance from entry_ref to stop-loss in price units; must be > 0
    target_distance : Distance from entry_ref to take-profit in price units; must be > 0
    strategy_name   : Name of the strategy that produced this signal
    timeframe       : Granularity string, e.g. ``"H1"`` or ``"D"``
    quality_score   : Normalised signal quality in [0, 1]
    generated_at    : UTC-aware datetime — the **bar's close timestamp** (INV-03);
                      must NOT be datetime.now()
    """

    instrument: str
    direction: Direction
    entry_ref: float
    stop_distance: float
    target_distance: float
    strategy_name: str
    timeframe: str
    quality_score: float
    generated_at: datetime

    @field_validator("stop_distance")
    @classmethod
    def stop_distance_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"stop_distance must be > 0, got {v}")
        return v

    @field_validator("target_distance")
    @classmethod
    def target_distance_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"target_distance must be > 0, got {v}")
        return v

    @field_validator("quality_score")
    @classmethod
    def quality_score_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"quality_score must be in [0, 1], got {v}")
        return v

    @field_validator("generated_at")
    @classmethod
    def generated_at_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "generated_at must be UTC-aware (INV-03). "
                "Use the bar's close timestamp, not datetime.now()."
            )
        return v


class Strategy(abc.ABC):
    """Abstract base class for all Fathom trading strategies.

    Subclasses must implement:
    - ``name`` property — a unique string identifier for the strategy
    - ``generate_signals(df)`` — produce zero or more Signals from a candle DataFrame

    The DataFrame contract (D-02): columns include at minimum
    ``time, open_bid, high_bid, low_bid, close_bid`` with
    ``time`` as UTC-aware datetime64.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique strategy identifier string."""
        ...

    @abc.abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        """Generate trading signals from the provided OHLC DataFrame.

        Parameters
        ----------
        df:
            Candle data. At minimum: columns ``time`` (UTC-aware datetime64),
            ``open_bid``, ``high_bid``, ``low_bid``, ``close_bid``, ``volume``.
            DataFrames are used read-only — implementations must not mutate them.

        Returns
        -------
        list[Signal]
            Zero or more signals. At most one signal per bar.
        """
        ...

    # Make Strategy un-instantiable without full implementation (abc.ABC handles this)
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
