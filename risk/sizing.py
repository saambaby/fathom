"""Position sizing — derive a signed unit count from stop distance + equity.

This module owns **INV-05**: no single trade risks more than 0.25% of current
account equity, and position size is *derived* from the stop distance and that
risk budget — never a fixed lot.  It is the first gate that can reject a trade:
an uncomputable or oversized size yields ``units=0`` with a reason, never a
naked or oversized order (INV-04 / INV-11 boundary).

Design (DRIFT-07 / AMBIGUOUS-02, resolved at the 2026-05-29 cross-spec audit)
----------------------------------------------------------------------------
* Per-unit risk in **account currency** is::

      per_unit_risk = stop_distance × quote_to_account_rate

  ``stop_distance`` is a *price distance* (in the instrument's quote currency,
  e.g. USD for EUR_USD, JPY for USD_JPY).  ``rate`` (``quote_to_account_rate``)
  converts one unit of the **quote** currency into the **account** currency.
  There is deliberately **no** ``InstrumentMeta.pip_value`` field — per-unit
  risk derives from ``stop_distance`` and ``rate`` only.

  - quote == account (EUR_USD, USD account)  → ``rate = 1.0``.
  - quote != account (USD_JPY, USD account)  → ``rate = 1 / USD_JPY_mid``
    (the value of 1 JPY in USD).  The caller (execution CLI/orchestrator)
    computes ``rate`` from the latest cached candle mid; this function takes it
    as an input and is therefore pure.

* ``risk_budget = equity × risk_fraction`` (``risk_fraction`` defaults to
  ``0.0025`` = 0.25%, the INV-05 cap).

* ``units = floor(risk_budget / per_unit_risk)``, signed by direction.  Floor is
  strictly non-increasing, so ``|units| × per_unit_risk ≤ risk_budget`` always
  holds — the cap cannot be silently exceeded (the INV-05 property test pins
  this across random inputs).

* **Reject** (``units=0`` + reason) when ``stop_distance ≤ 0`` (INV-04/11 — never
  sized naked), when ``rate``/``equity`` are non-finite or non-positive, or when
  the largest cap-respecting size is below ``InstrumentMeta.min_trade_size`` —
  we never round *up* to the minimum (that would breach the cap).  No
  max-trade-size clamp (OANDA exposes no per-instrument max; the book-level cap
  lives in risk-limits-kill-switch).

Account currency is assumed **USD** for the Phase 3 demo account (config-driven
at the call site; this pure function only needs the already-resolved ``rate``).

Purity: no network, no clock, no store access.  ``equity`` and ``rate`` are
inputs (INV-03 has no bearing here — there are no timestamps).
"""

from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel, field_validator

from data.oanda_client import InstrumentMeta
from signals.ranker import Candidate
from strategies.base import Direction

__all__ = ["SizingResult", "DEFAULT_RISK_FRACTION", "ACCOUNT_CURRENCY", "size_position"]

#: The INV-05 per-trade risk cap as a fraction of equity (0.25%).  This is the
#: single most safety-critical constant in the system.  It is the *default*
#: ``risk_fraction``; a caller may pass a smaller value but the cap it enforces
#: can never be silently exceeded (floor-only sizing guarantees this).
DEFAULT_RISK_FRACTION: float = 0.0025

#: Account currency for the Phase 3 demo account.  Documented here for the
#: ``rate`` contract: ``rate`` converts the instrument's *quote* currency into
#: this currency.  Config-driven at the call site (the CLI resolves the rate);
#: this constant exists only to document the assumption.
ACCOUNT_CURRENCY: str = "USD"


class SizingResult(BaseModel):
    """The outcome of a sizing decision.

    Fields
    ------
    units       : signed integer unit count (long > 0, short < 0).  ``0`` means
                  **rejected** — never place an order on a zero result.
    risk_amount : the actual money at risk in account currency if the stop is
                  hit (``|units| × per_unit_risk``).  ``0.0`` on rejection.  By
                  construction ``risk_amount ≤ equity × risk_fraction`` (INV-05).
    reason      : human-readable rejection reason, set iff ``units == 0``;
                  ``None`` on a successful size.  Surfaced by the execution CLI.
    """

    units: int
    risk_amount: float
    reason: Optional[str] = None

    @field_validator("risk_amount")
    @classmethod
    def _risk_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"risk_amount must be >= 0, got {v}")
        return v


def _reject(reason: str) -> SizingResult:
    """Build a rejection result (``units=0``, no money at risk)."""
    return SizingResult(units=0, risk_amount=0.0, reason=reason)


def size_position(
    candidate: Candidate,
    equity: float,
    *,
    instrument_meta: InstrumentMeta,
    rate: float = 1.0,
    risk_fraction: float = DEFAULT_RISK_FRACTION,
) -> SizingResult:
    """Derive a signed unit count for ``candidate`` under the INV-05 cap.

    Args:
        candidate: the approved, frozen ``Candidate`` (INV-13); read-only.  Its
            ``stop_distance`` (a quote-currency price distance) and ``direction``
            drive the size.
        equity: current account equity in account currency (an input — fetched
            once by the orchestrator; not read here).  Must be finite and > 0.
        instrument_meta: the instrument's OANDA metadata; only
            ``min_trade_size`` is consulted (the reject floor).  There is no
            ``pip_value`` field and no max-size field.
        rate: quote→account conversion rate (``quote_to_account_rate``).
            ``1.0`` when the quote currency *is* the account currency (e.g.
            EUR_USD with a USD account).  For a non-account-quote pair (e.g.
            USD_JPY with a USD account) this is the value of 1 quote-currency
            unit in account currency (``1 / USD_JPY_mid``).  Must be finite > 0.
        risk_fraction: fraction of equity at risk per trade.  Defaults to
            ``0.0025`` (the INV-05 0.25% cap).  Must be finite and > 0.

    Returns:
        A ``SizingResult``.  On success ``units`` is signed by direction and
        ``risk_amount = |units| × stop_distance × rate ≤ equity × risk_fraction``
        (INV-05).  On any rejection ``units == 0`` and ``reason`` is set.

    Notes:
        Pure and deterministic — no network, no clock, no store.  Never raises
        on bad sizing inputs; it rejects with a reason instead, so the execution
        path always gets a definite, safe answer (never a naked or oversized
        order).
    """
    # --- 1. Validate the cap parameters (defensive; reject, never raise). ---
    if not math.isfinite(risk_fraction) or risk_fraction <= 0:
        return _reject(
            f"risk_fraction must be finite and > 0, got {risk_fraction!r}."
        )
    if not math.isfinite(equity) or equity <= 0:
        return _reject(f"equity must be finite and > 0, got {equity!r}.")
    if not math.isfinite(rate) or rate <= 0:
        return _reject(
            f"quote_to_account_rate must be finite and > 0, got {rate!r}."
        )

    # --- 2. Reject a naked / uncomputable stop (INV-04 / INV-11 boundary). ---
    stop_distance = candidate.stop_distance
    if not math.isfinite(stop_distance) or stop_distance <= 0:
        return _reject(
            "stop_distance must be finite and > 0 — a candidate with no valid "
            f"stop is rejected, never sized naked (INV-04); got {stop_distance!r}."
        )

    # --- 3. Resolve direction sign (long > 0, short < 0; FLAT/unknown reject). -
    try:
        direction = Direction(candidate.direction)
    except ValueError:
        return _reject(
            f"untradeable direction {candidate.direction!r} (expected LONG/SHORT)."
        )
    if direction is Direction.LONG:
        sign = 1
    elif direction is Direction.SHORT:
        sign = -1
    else:  # Direction.FLAT
        return _reject(
            f"untradeable direction {candidate.direction!r} (expected LONG/SHORT)."
        )

    # --- 4. Per-unit risk in account currency (DRIFT-07). -------------------
    # stop_distance is a quote-currency price distance; rate converts it to the
    # account currency.  Both factors are > 0 here, so per_unit_risk > 0.
    per_unit_risk = stop_distance * rate
    if not math.isfinite(per_unit_risk) or per_unit_risk <= 0:
        # Defensive: only reachable via float overflow/underflow of the product.
        return _reject(
            "per-unit risk is not finite/positive after conversion "
            f"(stop_distance={stop_distance!r}, rate={rate!r})."
        )

    # --- 5. Risk budget and floor-only sizing (INV-05 cap). -----------------
    risk_budget = equity * risk_fraction
    raw_units = risk_budget / per_unit_risk
    # floor() only ever rounds DOWN, so |units| * per_unit_risk <= risk_budget.
    # This is the line that makes the 0.25% cap unbreachable: there is no path
    # that rounds up or clamps to a minimum.
    magnitude = math.floor(raw_units)

    # --- 6. Reject when the cap cannot fund the minimum trade size. ---------
    # We never round UP to min_trade_size — that would breach the cap.
    min_trade_size = instrument_meta.min_trade_size
    if magnitude < min_trade_size or magnitude < 1:
        return _reject(
            f"risk budget {risk_budget:.6g} (= equity {equity:.6g} × "
            f"{risk_fraction:.6g}) funds only {magnitude} units at a per-unit "
            f"risk of {per_unit_risk:.6g}, below the instrument minimum "
            f"{min_trade_size:.6g}. Rejected (never rounded up to the minimum)."
        )

    units = sign * magnitude
    risk_amount = magnitude * per_unit_risk

    return SizingResult(units=units, risk_amount=risk_amount, reason=None)
