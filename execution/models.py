"""The frozen in-process execution contract (INV-14) + the bracket-maths function.

This module is the execution-side analogue of ``signals/ranker.py::Candidate``
(INV-13): the ``Order``/``Fill``/``Position`` pydantic v2 models are the stable
wire shapes that ``position-sizing``, ``order-placement``, ``reconciliation``,
the deviation monitor, and the alerter all build against.  Field names
(snake_case), types, and flat shape are **frozen** — a change is a breaking
amendment to INV-14.

The module holds models and pure maths only.  No OANDA submission, no network,
no clock beyond the timestamps passed in (no ``datetime.now()`` anywhere).

Invariants enforced here
------------------------
* **INV-03** — every datetime field is UTC-aware; naive datetimes are rejected
  by validators (the ``Signal``/``Candidate`` style).
* **INV-04** — :func:`build_bracket` produces a stop **and** a take-profit for
  every ``Order``; there is no code path to a naked order.  A non-positive
  ``stop_distance`` raises (rejected, never sized naked).
* **INV-14** — these three models are the frozen execution contract.
* **INV-15** — :func:`build_bracket` computes the deterministic
  ``client_order_id``.

Sign convention (AMBIGUOUS-05, pinned by the AC)
------------------------------------------------
* ``units`` / ``units_filled`` are signed to match OANDA v20: **long > 0**,
  **short < 0**.  Zero is invalid.
* ``slippage`` is signed so **positive = adverse** (a fill worse than the
  candidate's ``entry_ref``) regardless of direction.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator

from signals.ranker import Candidate
from strategies.base import Direction

__all__ = ["EntryType", "FillStatus", "Order", "Fill", "Position", "build_bracket"]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class EntryType(str, Enum):
    """How an order enters the market.

    Phase 3 is market-only (the watchlist ``entry_ref`` is a reference, the
    order fills at market — see the spec's resolved open question).  The enum
    exists so a later limit/stop-entry amendment is additive, not a breaking
    shape change.
    """

    MARKET = "market"


class FillStatus(str, Enum):
    """Terminal/partial state of a broker fill."""

    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Shared validators (mirroring the Signal/Candidate validator style)
# ---------------------------------------------------------------------------


def _require_utc_aware(v: datetime, field: str) -> datetime:
    """Reject a naive datetime (INV-03).  Returns ``v`` unchanged when aware."""
    if v.tzinfo is None:
        raise ValueError(
            f"{field} must be UTC-aware (INV-03). "
            "Use a UTC-aware bar/clock timestamp, never datetime.now()."
        )
    return v


def _require_positive(v: float, field: str) -> float:
    """Reject a non-positive price."""
    if v <= 0:
        raise ValueError(f"{field} must be > 0, got {v}")
    return v


def _require_signed_nonzero_units(v: int, field: str) -> int:
    """Reject zero units; sign encodes direction (long > 0, short < 0)."""
    if v == 0:
        raise ValueError(
            f"{field} must be a signed non-zero integer "
            "(long > 0, short < 0); got 0."
        )
    return v


# ---------------------------------------------------------------------------
# Order — intent to open a bracketed position (INV-04, INV-14, INV-15)
# ---------------------------------------------------------------------------


class Order(BaseModel):
    """An intent to open a bracketed position.

    Frozen execution contract (INV-14).  Every ``Order`` carries both a
    ``stop_loss_price`` and a ``take_profit_price`` (INV-04) and a non-empty
    deterministic ``client_order_id`` (INV-15).

    Fields
    ------
    client_order_id   : deterministic idempotency key (sha256[:32]) — INV-15.
    instrument        : OANDA instrument identifier, e.g. ``"EUR_USD"``.
    direction         : ``LONG`` | ``SHORT``.
    units             : signed size (long > 0, short < 0); zero invalid.
    entry_type        : market-only for Phase 3.
    stop_loss_price   : absolute bracket stop price (> 0).
    take_profit_price : absolute bracket target price (> 0).
    candidate_ref     : provenance ``f"{instrument}:{timeframe}:{strategy_name}"``.
    created_at        : UTC-aware order-creation time (INV-03).
    """

    client_order_id: str
    instrument: str
    direction: Direction
    units: int
    entry_type: EntryType
    stop_loss_price: float
    take_profit_price: float
    candidate_ref: str
    created_at: datetime

    @field_validator("client_order_id", "instrument", "candidate_ref")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("units")
    @classmethod
    def _units_signed_nonzero(cls, v: int) -> int:
        return _require_signed_nonzero_units(v, "units")

    @field_validator("stop_loss_price")
    @classmethod
    def _stop_positive(cls, v: float) -> float:
        return _require_positive(v, "stop_loss_price")

    @field_validator("take_profit_price")
    @classmethod
    def _target_positive(cls, v: float) -> float:
        return _require_positive(v, "take_profit_price")

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, v: datetime) -> datetime:
        return _require_utc_aware(v, "created_at")


# ---------------------------------------------------------------------------
# Fill — the broker's confirmation (INV-14)
# ---------------------------------------------------------------------------


class Fill(BaseModel):
    """The broker's confirmation of an order.

    Fields
    ------
    client_order_id : the originating order's idempotency key (INV-15 link).
    broker_trade_id : OANDA's trade identifier for the resulting position.
    fill_price      : the executed price (> 0).
    units_filled    : signed filled size (long > 0, short < 0); zero invalid.
    slippage        : signed; **positive = adverse** vs ``entry_ref`` (any dir).
    filled_at       : UTC-aware fill time (INV-03).
    status          : ``filled`` | ``partial`` | ``rejected``.
    """

    client_order_id: str
    broker_trade_id: str
    fill_price: float
    units_filled: int
    slippage: float
    filled_at: datetime
    status: FillStatus

    @field_validator("client_order_id", "broker_trade_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("fill_price")
    @classmethod
    def _fill_price_positive(cls, v: float) -> float:
        return _require_positive(v, "fill_price")

    @field_validator("units_filled")
    @classmethod
    def _units_filled_signed_nonzero(cls, v: int) -> int:
        return _require_signed_nonzero_units(v, "units_filled")

    @field_validator("filled_at")
    @classmethod
    def _filled_at_utc(cls, v: datetime) -> datetime:
        return _require_utc_aware(v, "filled_at")


# ---------------------------------------------------------------------------
# Position — current/closed open state (INV-14)
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """Current (or closed) open state of a bracketed position.

    Fields
    ------
    broker_trade_id   : OANDA trade identifier.
    instrument        : OANDA instrument identifier.
    units             : signed size (long > 0, short < 0); zero invalid.
    entry_price       : the fill/entry price (> 0).
    stop_loss_price   : active bracket stop (> 0).
    take_profit_price : active bracket target (> 0).
    opened_at         : UTC-aware open time (INV-03).
    unrealized_pl     : mark-to-market PnL while open.
    closed_at         : UTC-aware close time, or ``None`` while open.
    realized_pl       : realised PnL, written on close (``None`` until then).
    candidate_ref     : provenance ``f"{instrument}:{timeframe}:{strategy_name}"``.
    """

    broker_trade_id: str
    instrument: str
    units: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    opened_at: datetime
    unrealized_pl: float
    closed_at: Optional[datetime] = None
    realized_pl: Optional[float] = None
    candidate_ref: str

    @field_validator("broker_trade_id", "instrument", "candidate_ref")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("units")
    @classmethod
    def _units_signed_nonzero(cls, v: int) -> int:
        return _require_signed_nonzero_units(v, "units")

    @field_validator("entry_price")
    @classmethod
    def _entry_positive(cls, v: float) -> float:
        return _require_positive(v, "entry_price")

    @field_validator("stop_loss_price")
    @classmethod
    def _stop_positive(cls, v: float) -> float:
        return _require_positive(v, "stop_loss_price")

    @field_validator("take_profit_price")
    @classmethod
    def _target_positive(cls, v: float) -> float:
        return _require_positive(v, "take_profit_price")

    @field_validator("opened_at")
    @classmethod
    def _opened_at_utc(cls, v: datetime) -> datetime:
        return _require_utc_aware(v, "opened_at")

    @field_validator("closed_at")
    @classmethod
    def _closed_at_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        return _require_utc_aware(v, "closed_at")


# ---------------------------------------------------------------------------
# build_bracket — pure Candidate → Order maths (INV-04, INV-15)
# ---------------------------------------------------------------------------


def build_bracket(
    candidate: Candidate,
    units: int,
    *,
    execution_date: datetime,
    precision: int,
) -> Order:
    """Convert an approved ``Candidate`` + a signed unit count into an ``Order``.

    Turns the candidate's **price-distance** stop/target into **absolute**
    bracket prices for the order's direction, rounds them to ``precision``
    decimal places (bound to ``InstrumentMeta.display_precision``, DRIFT-07),
    and computes the deterministic ``client_order_id`` (INV-15).

    Bracket maths (INV-04 — always a stop **and** a target)::

        LONG  : stop   = entry_ref − stop_distance
                target = entry_ref + target_distance
        SHORT : stop   = entry_ref + stop_distance
                target = entry_ref − target_distance

    Args:
        candidate: the frozen, approved ``Candidate`` (INV-13); read-only.
        units: signed size — long > 0, short < 0 (from ``position-sizing``).
        execution_date: UTC-aware order-creation time; becomes ``created_at``
            and folds into the ``client_order_id`` (INV-15).  No internal clock.
        precision: decimal places to round bracket prices to
            (``InstrumentMeta.display_precision``).

    Returns:
        A fully-bracketed ``Order`` (stop + target both present, INV-04) with a
        non-empty deterministic ``client_order_id`` (INV-15).

    Raises:
        ValueError: if ``candidate.stop_distance`` or ``target_distance`` is
            non-positive (rejected upstream — never sized naked); if
            ``execution_date`` is naive (INV-03); if ``units`` is zero or its
            sign disagrees with the candidate direction; if ``candidate.direction``
            is not ``LONG``/``SHORT``.
    """
    _require_utc_aware(execution_date, "execution_date")

    if candidate.stop_distance <= 0:
        raise ValueError(
            "build_bracket: stop_distance must be > 0 (INV-04 — a candidate "
            f"with a non-positive stop is rejected, never sized naked); got "
            f"{candidate.stop_distance}."
        )
    if candidate.target_distance <= 0:
        raise ValueError(
            "build_bracket: target_distance must be > 0 (INV-04); got "
            f"{candidate.target_distance}."
        )

    direction = Direction(candidate.direction)
    if direction is Direction.LONG:
        if units <= 0:
            raise ValueError(
                f"build_bracket: LONG candidate requires units > 0, got {units}."
            )
        stop_price = candidate.entry_ref - candidate.stop_distance
        target_price = candidate.entry_ref + candidate.target_distance
    elif direction is Direction.SHORT:
        if units >= 0:
            raise ValueError(
                f"build_bracket: SHORT candidate requires units < 0, got {units}."
            )
        stop_price = candidate.entry_ref + candidate.stop_distance
        target_price = candidate.entry_ref - candidate.target_distance
    else:
        raise ValueError(
            f"build_bracket: untradeable direction {candidate.direction!r} "
            "(expected LONG or SHORT)."
        )

    stop_price = round(stop_price, precision)
    target_price = round(target_price, precision)

    # Defence in depth: a stop distance smaller than the rounding granularity
    # could round a bracket onto the wrong side of entry, silently producing a
    # non-protective order.  Reject rather than emit a malformed bracket (INV-04).
    entry_rounded = round(candidate.entry_ref, precision)
    if direction is Direction.LONG:
        if not (stop_price < entry_rounded < target_price):
            raise ValueError(
                "build_bracket: rounded LONG bracket does not straddle entry "
                f"(stop={stop_price}, entry={entry_rounded}, target={target_price}) "
                f"at precision {precision} — stop/target distances too small to "
                "represent. Rejected (INV-04)."
            )
    else:  # SHORT
        if not (target_price < entry_rounded < stop_price):
            raise ValueError(
                "build_bracket: rounded SHORT bracket does not straddle entry "
                f"(target={target_price}, entry={entry_rounded}, stop={stop_price}) "
                f"at precision {precision} — stop/target distances too small to "
                "represent. Rejected (INV-04)."
            )

    client_order_id = _client_order_id(candidate, execution_date)
    candidate_ref = (
        f"{candidate.instrument}:{candidate.timeframe}:{candidate.strategy_name}"
    )

    return Order(
        client_order_id=client_order_id,
        instrument=candidate.instrument,
        direction=direction,
        units=units,
        entry_type=EntryType.MARKET,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
        candidate_ref=candidate_ref,
        created_at=execution_date,
    )


def _client_order_id(candidate: Candidate, execution_date: datetime) -> str:
    """Deterministic idempotency key (INV-15 / DRIFT-03).

    ``sha256(f"{instrument}:{strategy_name}:{timeframe}:{generated_at}:"
    f"{execution_date}").hexdigest()[:32]`` over the candidate fields plus the
    injected ``execution_date``.  ``generated_at`` is already the candidate's
    RFC-3339 string; ``execution_date`` is interpolated via the f-string as the
    spec states.  Pure: identical inputs always yield the identical id.
    """
    payload = (
        f"{candidate.instrument}:{candidate.strategy_name}:{candidate.timeframe}:"
        f"{candidate.generated_at}:{execution_date}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
