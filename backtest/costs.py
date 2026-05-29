"""Cost model for the backtest engine (POC-T-05; swap lifted in P1A-T-03).

This module is the enforcement point for **INV-06**: a backtest result is only
valid if it models real trading costs.  Phase 1A (P1A-T-03) lifts the PoC's
swap deferral (D-03), so all four INV-06 cost categories are now modelled:

  1. **Spread** — half the spread is paid on each leg (entry + exit).  A long
     buys at the ask (entry worsened upward) and sells at the bid (exit
     worsened downward); a short is the mirror image.
  2. **Slippage** — a pip offset applied adversely on stop/target fills (market
     fills, not limit entries).
  3. **Commission** — an optional per-round-trip charge in pips
     (``commission_pips``, default 0.0 for spread-only accounts).
  4. **Swap / overnight financing** — ``swap = daily_rate × holding_days`` on
     the direction's side (long → ``swap_long_rate``, short →
     ``swap_short_rate``).  ``holding_days`` is the calendar-day count between
     the entry and exit bar UTC dates, computed by the engine.  A position
     closed same-bar (0 holding days) accrues zero swap.

The D-03 deferral is gone: the legacy ``swap_pips`` field, its pydantic
validator, and the inline ``apply_costs`` guard have all been removed (both
guard sites — not just one).  ``CostResult.swap_modelled`` is now ``True``
whenever financing is applied.

Sign convention for financing
-----------------------------
``swap_long_rate`` / ``swap_short_rate`` are *daily costs in pips*: a positive
rate is a charge that worsens net PnL, a negative rate is positive carry that
improves it.  The per-trade financing charge is
``swap_pips = rate × holding_days`` (rate selected by direction).  It is folded
into ``net_exit`` adversely-signed so that, after the engine's
``net PnL = f(net_entry, net_exit)`` computation, net PnL is reduced by exactly
``swap_pips`` for a long and likewise for a short — direction-aware and
sign-correct for both charge and carry.

Why a zero-cost *spread path* is impossible (INV-06)
----------------------------------------------------
The **spread + slippage + commission floor** is computed as
``spread_pips + slippage_pips + commission_pips`` — a value that depends
**only** on the cost parameters, never on the price path, the trade direction,
or the financing rate.  For any ``spread_pips > 0`` *or* ``slippage_pips > 0``
that floor is strictly > 0, and ``CostParams`` enforces ``spread_pips > 0`` so
the engine can never run cost-free.  The half-spread / slippage offsets are
always applied on the *adverse* side of each leg, so they move net PnL against
the trade.

Financing is **additive on top of** that floor: a positive-carry side (negative
``rate``) reduces the *net* cost but is reported in a separate, possibly
negative ``swap_pips`` term — it is never subtracted out of the
strictly-positive spread+slippage floor.  ``total_cost_pips`` is therefore
``spread_pips + slippage_pips + commission_pips + swap_pips`` where only the
swap term can be negative; the floor guarantees the spread path itself is never
free.  ``gross PnL ≥ net PnL`` holds for any non-positive-carry trade, and
positive carry can only improve net — it can never make the spread leg free.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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
        Exit price after the adverse half-spread, exit slippage, commission, and
        overnight financing — all folded in on the exit leg so the engine's
        ``net PnL = f(net_entry, net_exit)`` reflects every cost category.
    total_cost_pips:
        Total cost of the round trip, in pips:
        ``spread_pips + slippage_pips + commission_pips + swap_pips``.  The
        spread+slippage+commission portion is strictly > 0 whenever
        ``spread_pips`` or ``slippage_pips`` is non-zero (INV-06); the
        ``swap_pips`` term may be negative for a positive-carry trade.
    swap_modelled:
        ``True`` whenever financing was applied (the normal Phase-1 path);
        ``False`` only when ``apply_costs`` is run with both financing rates at
        ``0.0`` (the backward-compatible spread-only path).
    """

    net_entry: float
    net_exit: float
    total_cost_pips: float
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
        Must be >= 0; combined with a positive spread the spread+slippage floor
        is always > 0.
    pip_value:
        Price increment of one pip for the instrument, e.g. ``0.0001`` for most
        FX majors and ``0.01`` for JPY pairs.  Used to convert between price
        units and pips.  Must be > 0.
    swap_long_rate:
        Daily overnight financing for a **long** position, in pips per day
        (mapped from ``InstrumentMeta.long_rate`` at the engine boundary).
        Positive = a charge; negative = positive carry.  No sign constraint.
    swap_short_rate:
        Daily overnight financing for a **short** position, in pips per day
        (mapped from ``InstrumentMeta.short_rate``).  Same sign convention.
    commission_pips:
        Per-round-trip commission in pips.  Defaults to ``0.0`` (spread-only
        accounts).  Must be >= 0.
    """

    spread_pips: float = Field(..., gt=0.0)
    slippage_pips: float = Field(0.0, ge=0.0)
    pip_value: float = Field(..., gt=0.0)
    swap_long_rate: float = Field(0.0)
    swap_short_rate: float = Field(0.0)
    commission_pips: float = Field(0.0, ge=0.0)


def apply_costs(
    entry_price: float,
    exit_price: float,
    direction: Direction,
    spread_pips: float,
    slippage_pips: float,
    pip_value: float,
    swap_long_rate: float,
    swap_short_rate: float,
    holding_days: int,
    commission_pips: float = 0.0,
) -> CostResult:
    """Apply spread + slippage + commission + swap to a round-trip trade.

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
        Pip offset applied adversely on the stop/target exit fill (stop/target
        are the market fills; entry is treated as a controlled next-open fill).
    pip_value:
        Price increment of one pip (e.g. ``0.0001`` or ``0.01`` for JPY).
    swap_long_rate:
        Daily financing for a long, in pips/day.  Used when ``direction`` is
        ``LONG``.  Positive = charge, negative = carry.
    swap_short_rate:
        Daily financing for a short, in pips/day.  Used when ``direction`` is
        ``SHORT``.
    holding_days:
        Calendar days the position was held (entry→exit UTC dates), computed by
        the engine.  ``0`` for a same-bar / intraday close → zero swap.  Must be
        >= 0.
    commission_pips:
        Per-round-trip commission in pips (default 0.0).  Must be >= 0.

    Returns
    -------
    CostResult
        ``net_entry``, ``net_exit`` (with all costs folded onto the exit leg),
        ``total_cost_pips`` (spread + slippage + commission + swap; the
        spread+slippage+commission portion is strictly > 0 for any non-zero
        spread/slippage), and ``swap_modelled`` (``True`` when financing was
        applied, i.e. when either rate is non-zero).

    Raises
    ------
    ValueError
        If ``direction`` is ``FLAT``, if ``pip_value <= 0``, if ``spread_pips``,
        ``slippage_pips``, or ``commission_pips`` is negative, or if
        ``holding_days`` is negative.  (Non-zero financing no longer raises —
        the D-03 guard is gone.)
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
    if commission_pips < 0.0:
        raise ValueError(f"commission_pips must be >= 0, got {commission_pips}.")
    if holding_days < 0:
        raise ValueError(f"holding_days must be >= 0, got {holding_days}.")

    # Financing = daily rate × holding days, on the direction's side. A same-bar
    # close (holding_days == 0) accrues exactly zero swap. swap_modelled flips
    # True whenever financing data is supplied (either rate non-zero).
    daily_rate = (
        swap_long_rate if direction is Direction.LONG else swap_short_rate
    )
    swap_pips = daily_rate * holding_days
    swap_modelled = swap_long_rate != 0.0 or swap_short_rate != 0.0

    half_spread_price = (spread_pips / 2.0) * pip_value
    slippage_price = slippage_pips * pip_value
    # Commission + swap are flat pip charges (not tied to a price level). Fold
    # them onto the exit leg, signed so that net PnL is reduced by exactly
    # (commission_pips + swap_pips). A positive swap (charge) worsens net PnL; a
    # negative swap (carry) improves it. This keeps the engine's existing
    # net-PnL-from-net-prices computation correct and direction-aware.
    flat_charge_price = (commission_pips + swap_pips) * pip_value

    if direction is Direction.LONG:
        # Long: buy at the ask (entry up by half-spread), sell at the bid
        # (exit down by half-spread). Slippage on a long exit (stop/target is a
        # sell) fills *lower* — adverse. A charge lowers the long's net exit.
        net_entry = entry_price + half_spread_price
        net_exit = exit_price - half_spread_price - slippage_price - flat_charge_price
    else:  # Direction.SHORT
        # Short: sell at the bid (entry down by half-spread), buy back at the
        # ask (exit up by half-spread). Slippage on a short exit (a buy) fills
        # *higher* — adverse. A charge raises the short's net exit (its PnL is
        # entry - exit, so a higher exit reduces PnL by the charge).
        net_entry = entry_price - half_spread_price
        net_exit = exit_price + half_spread_price + slippage_price + flat_charge_price

    # Total cost: the strictly-positive spread+slippage+commission floor plus
    # the financing term (which may be negative for positive carry). The floor
    # alone is > 0 for any non-zero spread or slippage (INV-06).
    total_cost_pips = spread_pips + slippage_pips + commission_pips + swap_pips

    return CostResult(
        net_entry=net_entry,
        net_exit=net_exit,
        total_cost_pips=total_cost_pips,
        swap_modelled=swap_modelled,
    )
