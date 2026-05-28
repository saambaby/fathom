"""Cost model for the backtest engine (POC-T-05).

This module is the enforcement point for **INV-06**: a backtest result is only
valid if it models real trading costs.  For the PoC we model two of the four
cost categories:

  1. **Spread** — half the spread is paid on each leg (entry + exit).  A long
     buys at the ask (entry worsened upward) and sells at the bid (exit
     worsened downward); a short is the mirror image.
  2. **Slippage** — a pip offset applied adversely on stop/target fills (market
     fills, not limit entries).

The remaining two categories are deliberately **not** modelled in the PoC:

  - **Commission** — out of scope for the PoC instruments (spread-only account).
  - **Swap / overnight financing** — DEFERRED per decision **D-03**.  Every
    output carries ``swap_modelled=False`` and ``apply_costs`` only accepts
    ``swap_pips=0.0``.  A non-zero ``swap_pips`` is rejected so that no caller
    can silently believe swap is being modelled when it is not.

Why a zero-cost trade is impossible (INV-06)
--------------------------------------------
``total_cost_pips`` is computed as ``spread_pips + slippage_pips`` — a value
that depends **only** on the cost parameters, never on the price path or the
trade direction.  For any ``spread_pips > 0`` *or* ``slippage_pips > 0`` the sum
is strictly > 0.  The half-spread / slippage offsets are always applied on the
*adverse* side of each leg, so the net entry/exit prices can only ever move PnL
*against* the trade — never in its favour.  Consequently ``net PnL <= gross
PnL`` is structurally guaranteed, and a cost-free round trip cannot occur unless
the caller explicitly passes zero spread *and* zero slippage (which the
backtest engine never does — see ``CostParams`` validation in ``engine.py``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from strategies.base import Direction


class CostResult(BaseModel):
    """Result of applying costs to a single round-trip trade.

    Attributes
    ----------
    net_entry:
        Entry price after the adverse half-spread (and, conceptually, any entry
        slippage — see below) has been applied.  This is the price the trade is
        *actually* considered to have filled at.
    net_exit:
        Exit price after the adverse half-spread and exit slippage.
    total_cost_pips:
        Total cost of the round trip, expressed in pips.  Strictly > 0 whenever
        either ``spread_pips`` or ``slippage_pips`` is non-zero (INV-06).
    swap_modelled:
        Always ``False`` for the PoC (D-03).  Present so downstream metrics and
        approved-set entries can carry the honest label.
    """

    net_entry: float
    net_exit: float
    total_cost_pips: float = Field(..., ge=0.0)
    swap_modelled: bool = False


class CostParams(BaseModel):
    """Per-run cost configuration for the engine.

    Attributes
    ----------
    spread_pips:
        Full bid/ask spread in pips.  Half is paid on entry, half on exit.
        Must be > 0 (a zero-spread backtest is fiction — INV-06).
    slippage_pips:
        Pip offset applied adversely on stop and target (market) fills.
        Must be >= 0; combined with a positive spread the total cost is always
        > 0.  Slippage alone (with zero spread) would also be > 0, but the
        engine additionally requires a positive spread.
    pip_value:
        Price increment of one pip for the instrument, e.g. ``0.0001`` for most
        FX majors and ``0.01`` for JPY pairs.  Used to convert between price
        units and pips.  Must be > 0.
    swap_pips:
        DEFERRED (D-03).  Must remain 0.0.
    """

    spread_pips: float = Field(..., gt=0.0)
    slippage_pips: float = Field(0.0, ge=0.0)
    pip_value: float = Field(..., gt=0.0)
    swap_pips: float = Field(0.0)

    @model_validator(mode="after")
    def _swap_must_be_zero(self) -> "CostParams":
        # D-03: swap is not implemented. Reject any attempt to pass a non-zero
        # value rather than silently ignoring it (which would let a caller
        # believe swap was modelled when it was not).
        if self.swap_pips != 0.0:
            raise ValueError(
                "swap_pips must be 0.0 — swap/financing is deferred (D-03). "
                "Every output is labelled swap_modelled=False."
            )
        return self


def apply_costs(
    entry_price: float,
    exit_price: float,
    direction: Direction,
    spread_pips: float,
    slippage_pips: float,
    pip_value: float,
    swap_pips: float = 0.0,
) -> CostResult:
    """Apply spread + slippage costs to a single round-trip trade.

    Parameters
    ----------
    entry_price:
        Gross (mid) entry price — typically the next bar's open at signal time.
    exit_price:
        Gross (mid) exit price — the stop or target *level* the trade exited at.
    direction:
        ``Direction.LONG`` or ``Direction.SHORT``.  ``FLAT`` is rejected.
    spread_pips:
        Full spread in pips.  Half is added to a long entry / subtracted from a
        long exit (opposite for a short).
    slippage_pips:
        Pip offset applied adversely on the stop/target exit fill.  For the PoC
        slippage is charged on the exit leg (stop/target are the market fills;
        entry is treated as a controlled next-open fill).
    pip_value:
        Price increment of one pip (e.g. ``0.0001`` or ``0.01`` for JPY).
    swap_pips:
        DEFERRED (D-03) — must be 0.0.

    Returns
    -------
    CostResult
        ``net_entry``, ``net_exit``, ``total_cost_pips`` (strictly > 0 for any
        non-zero spread or slippage), and ``swap_modelled=False``.

    Raises
    ------
    ValueError
        If ``direction`` is ``FLAT``, if ``pip_value <= 0``, if either
        ``spread_pips`` or ``slippage_pips`` is negative, or if ``swap_pips`` is
        non-zero (D-03).
    """
    if direction not in (Direction.LONG, Direction.SHORT):
        raise ValueError(
            f"apply_costs requires LONG or SHORT direction, got {direction!r}."
        )
    if pip_value <= 0.0:
        raise ValueError(f"pip_value must be > 0, got {pip_value}.")
    if spread_pips < 0.0:
        raise ValueError(f"spread_pips must be >= 0, got {spread_pips}.")
    if slippage_pips < 0.0:
        raise ValueError(f"slippage_pips must be >= 0, got {slippage_pips}.")
    if swap_pips != 0.0:
        # D-03: swap is deferred. Reject loudly rather than silently dropping it.
        raise ValueError(
            "swap_pips must be 0.0 — swap/financing is deferred (D-03)."
        )

    half_spread_price = (spread_pips / 2.0) * pip_value
    slippage_price = slippage_pips * pip_value

    if direction is Direction.LONG:
        # Long: buy at the ask (entry up by half-spread), sell at the bid
        # (exit down by half-spread). Slippage on a long exit (stop/target is a
        # sell) fills *lower* — adverse.
        net_entry = entry_price + half_spread_price
        net_exit = exit_price - half_spread_price - slippage_price
    else:  # Direction.SHORT
        # Short: sell at the bid (entry down by half-spread), buy back at the
        # ask (exit up by half-spread). Slippage on a short exit (a buy) fills
        # *higher* — adverse.
        net_entry = entry_price - half_spread_price
        net_exit = exit_price + half_spread_price + slippage_price

    # Total cost is path-independent: full spread (half per leg) + slippage on
    # the exit fill. Strictly > 0 for any non-zero spread or slippage (INV-06).
    total_cost_pips = spread_pips + slippage_pips

    return CostResult(
        net_entry=net_entry,
        net_exit=net_exit,
        total_cost_pips=total_cost_pips,
        swap_modelled=False,
    )
