"""Book-level risk gate + daily-loss kill switch (P3-T-04).

The deterministic gate that decides whether a freshly-*sized* order is allowed
onto the book *right now*.  Like sizing (``risk/sizing.py``), it can only
subtract: every check is a potential reject, never a green light.  It is the
book-level backstop for **INV-05** — where ``sizing`` caps a *single* trade at
0.25% of equity, this module caps the *aggregate* book and halts all new entries
once the day's loss crosses a threshold.

Four checks, evaluated most-global-first (the first breach wins; later checks are
short-circuited):

1. **Daily-loss kill switch.**  ``day_pl <= -(daily_loss_cap ×
   start_of_day_equity)`` → reject every order with ``kill_switch_active=True``
   until the next 00:00 UTC boundary.  ``day_pl`` is today's total P&L vs
   start-of-day equity (already negative on a loss — see ``data.store`` /
   DRIFT-02), read from the ``account_state`` row by the caller and injected
   here.  The reset boundary is computed from the injected ``now`` (INV-03);
   reconciliation zeroes ``day_pl`` at the new UTC day, so a fresh-day call
   naturally observes an inactive switch.
2. **Max concurrent.**  ``len(open_positions) >= max_concurrent`` → reject.
3. **Book risk.**  ``current_book_risk + order_risk > max_book_risk × equity``
   → reject.  ``current_book_risk`` is summed from each open position's
   **stop-distance risk** (``|units| × |entry_price − stop_loss_price|``), never
   notional.  ``order_risk`` is the prospective order's risk amount in account
   currency — the ``SizingResult.risk_amount`` already computed by sizing,
   injected here (this module does not re-derive it).
4. **Correlation bucket.**  Group the prospective order + open positions into
   correlation buckets (two instruments share a bucket when
   ``|pearson_corr| > correlation_threshold``, computed via the shared
   ``signals/correlation.py`` primitive over injected return Series).  If the
   order's bucket would then hold more than ``max_per_correlation_group``
   distinct instruments, reject — correlated pairs count as one bet.

Purity (AC): all state is injected — ``open_positions``, ``day_pl``, ``equity``,
``start_of_day_equity``, ``now``, ``order_risk``, and the correlation ``returns``
map.  No DB, no network, no clock beyond the injected ``now`` (INV-03).  The
function never raises on adversarial inputs; it rejects with a reason, so the
execution path always gets a definite, safe answer.

DRIFT-09: ``max_per_correlation_group`` (a *bucket-size* / shared-exposure cap)
is a distinct concept from the portfolio limiter's ``max_per_currency`` (a
per-leg-currency cap).  Both build on the same ``signals/correlation.py``
primitive but group differently.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from typing import Mapping, Optional, Sequence

import pandas as pd
from pydantic import BaseModel, Field

from execution.models import Order, Position
from signals.correlation import pearson_corr, split_currencies as split_currencies

__all__ = [
    "LimitsConfig",
    "LimitDecision",
    "KillSwitchStatus",
    "DEFAULT_DAILY_LOSS_CAP",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_BOOK_RISK",
    "DEFAULT_MAX_PER_CORRELATION_GROUP",
    "DEFAULT_CORRELATION_THRESHOLD",
    "position_risk",
    "book_risk_sum",
    "book_risk_budget",
    "check_limits",
    "kill_switch_status",
]

# ---------------------------------------------------------------------------
# Approved config defaults (D-P3-A / D-P3-B)
# ---------------------------------------------------------------------------

#: Daily cumulative loss cap as a fraction of start-of-day equity (1.0%).
#: ~4 max-loss trades at the 0.25% per-trade cap.  Crossing this halts all new
#: entries until 00:00 UTC.  Operator-overridable via :class:`LimitsConfig`.
DEFAULT_DAILY_LOSS_CAP: float = 0.01

#: Max simultaneously-open positions allowed on the book.
DEFAULT_MAX_CONCURRENT: int = 5

#: Max aggregate book risk as a fraction of equity (1.0%) — the sum of every
#: open position's stop-distance risk plus the prospective order's risk.
DEFAULT_MAX_BOOK_RISK: float = 0.01

#: Max distinct instruments allowed inside a single correlation bucket (shared
#: exposure).  Correlated pairs count as one bet; this caps how many we stack.
DEFAULT_MAX_PER_CORRELATION_GROUP: int = 2

#: Absolute Pearson |ρ| above which two instruments share a correlation bucket.
DEFAULT_CORRELATION_THRESHOLD: float = 0.7


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class LimitsConfig(BaseModel):
    """Tunable, explicit, documented book-level limits (D-P3-A / D-P3-B).

    Every field carries the approved Phase-3 default; a caller may override any
    subset.  All four are *caps* — raising one can only ever allow more, never
    weaken the INV-05 per-trade guarantee owned by ``risk/sizing.py``.

    Attributes:
        daily_loss_cap: fraction of start-of-day equity; ``day_pl`` at/below
            ``-(daily_loss_cap × start_of_day_equity)`` trips the kill switch.
        max_concurrent: max simultaneously-open positions.
        max_book_risk: fraction of equity; aggregate stop-distance risk
            (open + prospective) may not exceed ``max_book_risk × equity``.
        max_per_correlation_group: max distinct instruments per correlation
            bucket (shared exposure).
        correlation_threshold: absolute Pearson |ρ| above which two instruments
            share a bucket (0–1).
    """

    daily_loss_cap: float = Field(
        default=DEFAULT_DAILY_LOSS_CAP,
        gt=0.0,
        le=1.0,
        description="Daily-loss kill-switch threshold as a fraction of "
        "start-of-day equity (default 0.01 = 1.0%).",
    )
    max_concurrent: int = Field(
        default=DEFAULT_MAX_CONCURRENT,
        ge=1,
        description="Max simultaneously-open positions (default 5).",
    )
    max_book_risk: float = Field(
        default=DEFAULT_MAX_BOOK_RISK,
        gt=0.0,
        le=1.0,
        description="Max aggregate book risk as a fraction of equity "
        "(default 0.01 = 1.0%).",
    )
    max_per_correlation_group: int = Field(
        default=DEFAULT_MAX_PER_CORRELATION_GROUP,
        ge=1,
        description="Max distinct instruments per correlation bucket "
        "(default 2).",
    )
    correlation_threshold: float = Field(
        default=DEFAULT_CORRELATION_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="Absolute Pearson |ρ| bucket threshold (default 0.7).",
    )


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class LimitDecision(BaseModel):
    """The outcome of a book-level admission check.

    Fields:
        allowed: ``True`` iff the order passes every check.  ``False`` means
            rejected — never place the order.
        reason: human-readable rejection reason, set iff ``allowed is False``;
            ``None`` on an allowed order.
        kill_switch_active: ``True`` iff the daily-loss kill switch is currently
            tripped.  When ``True``, ``allowed`` is always ``False``.
    """

    allowed: bool
    reason: Optional[str] = None
    kill_switch_active: bool = False


class KillSwitchStatus(BaseModel):
    """Read-only snapshot of the daily-loss kill switch (no side effects).

    Fields:
        active: ``True`` iff ``day_pl`` is at/below the loss cap.
        day_pl: today's P&L vs start-of-day equity (negative on a loss), as
            supplied.
        cap_amount: the loss amount that trips the switch, a positive number
            (``daily_loss_cap × start_of_day_equity``); the switch is active
            when ``day_pl <= -cap_amount``.
        reset_at: the next 00:00 UTC boundary after ``now`` (RFC-3339 via
            pydantic's UTC-aware datetime); reconciliation zeroes ``day_pl`` at
            this boundary so a later call sees the switch reset (INV-03).
    """

    active: bool
    day_pl: float
    cap_amount: float
    reset_at: datetime


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def position_risk(position: Position) -> float:
    """Stop-distance risk of an open position in account-price terms.

    ``|units| × |entry_price − stop_loss_price|`` — the money lost if the stop
    is hit, **not** notional exposure.  This is the per-position term summed
    into ``current_book_risk``.  Defensive: a non-finite result yields ``0.0``
    rather than poisoning the sum (the position model already enforces positive
    prices and signed-non-zero units, so this is only reachable via float
    pathology).
    """
    risk = abs(position.units) * abs(position.entry_price - position.stop_loss_price)
    if not math.isfinite(risk) or risk < 0:
        return 0.0
    return risk


def book_risk_sum(open_positions: Sequence[Position]) -> float:
    """Aggregate stop-distance risk of the open book in account-price terms.

    The single source of truth for ``current_book_risk`` — the sum of every open
    position's :func:`position_risk` (stop-distance, **not** notional).
    :func:`check_limits` calls this for its book-risk check, and the read-only
    admin-panel blotter reuses it so the panel's "risk-in-use" figure is
    byte-identical to the figure the kill-switch backstop evaluates (INV-05;
    panel-data-layer DRIFT-02). An empty book is ``0.0``.
    """
    return sum(position_risk(p) for p in open_positions)


def book_risk_budget(equity: float, config: LimitsConfig) -> float:
    """The aggregate book-risk budget in account currency: ``max_book_risk × equity``.

    The single source of truth for the book-risk cap amount. :func:`check_limits`
    compares ``book_risk_sum(open) + order_risk`` against this, and the read-only
    admin-panel blotter reuses it so the panel's "limit" figure matches the
    kill-switch backstop exactly (INV-05; panel-data-layer DRIFT-02). The caller
    owns guarding ``equity`` finiteness/positivity before relying on the result
    (``check_limits`` rejects a non-finite/non-positive equity upstream).
    """
    return config.max_book_risk * equity


def _next_utc_midnight(now: datetime) -> datetime:
    """The next 00:00:00 UTC boundary strictly after ``now`` (INV-03).

    The kill switch resets at this boundary (reconciliation zeroes ``day_pl``).
    ``now`` is normalised to UTC first so a non-UTC-aware caller still gets a
    correct boundary; a naive ``now`` is treated as UTC.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    next_day = (now + timedelta(days=1)).date()
    return datetime.combine(next_day, time.min, tzinfo=timezone.utc)


def _kill_switch_tripped(
    day_pl: float, start_of_day_equity: float, daily_loss_cap: float
) -> tuple[bool, float]:
    """Return ``(active, cap_amount)`` for the daily-loss kill switch.

    ``cap_amount`` is the positive loss threshold
    ``daily_loss_cap × start_of_day_equity``; the switch is active when
    ``day_pl <= -cap_amount`` (``day_pl`` is already negative on a loss).
    Non-finite or non-positive ``start_of_day_equity`` is treated defensively as
    *tripped* (cap_amount 0.0) — we never green-light when we cannot trust the
    equity baseline.
    """
    if not math.isfinite(start_of_day_equity) or start_of_day_equity <= 0:
        return True, 0.0
    if not math.isfinite(day_pl):
        return True, 0.0
    cap_amount = daily_loss_cap * start_of_day_equity
    return (day_pl <= -cap_amount), cap_amount


def _correlation_bucket_instruments(
    target: str,
    others: Sequence[str],
    returns: Mapping[str, pd.Series],
    threshold: float,
) -> set[str]:
    """Instruments transitively correlated with ``target`` (its bucket).

    Builds the connected component containing ``target`` over the graph whose
    edges are instrument pairs with ``|pearson_corr| > threshold`` (computed via
    the shared ``signals/correlation.py`` primitive on the injected return
    Series).  ``pearson_corr`` returns ``None`` on insufficient/empty data — we
    do **not** create an edge in that case (conservative: missing data never
    *forces* a grouping, mirroring the portfolio limiter).

    The returned set always contains ``target`` itself.
    """
    universe = [target, *others]
    # Deduplicate while preserving order; a bucket is over *distinct* instruments.
    seen: dict[str, None] = {}
    for instr in universe:
        seen.setdefault(instr, None)
    distinct = list(seen.keys())

    def correlated(a: str, b: str) -> bool:
        if a == b:
            return True
        ra = returns.get(a)
        rb = returns.get(b)
        if ra is None or rb is None:
            return False
        rho = pearson_corr(ra, rb)
        if rho is None:
            return False
        return abs(rho) > threshold

    # BFS connected component from ``target``.
    bucket: set[str] = {target}
    frontier = [target]
    while frontier:
        current = frontier.pop()
        for other in distinct:
            if other in bucket:
                continue
            if correlated(current, other):
                bucket.add(other)
                frontier.append(other)
    return bucket


def _allow() -> LimitDecision:
    return LimitDecision(allowed=True, reason=None, kill_switch_active=False)


def _reject(reason: str, *, kill_switch_active: bool = False) -> LimitDecision:
    return LimitDecision(
        allowed=False, reason=reason, kill_switch_active=kill_switch_active
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_limits(
    order: Order,
    *,
    open_positions: Sequence[Position],
    day_pl: float,
    equity: float,
    start_of_day_equity: float,
    config: LimitsConfig,
    now: datetime,
    order_risk: float,
    returns: Optional[Mapping[str, pd.Series]] = None,
) -> LimitDecision:
    """Decide whether ``order`` may go onto the book right now.

    Pure and deterministic — every piece of state is injected (no DB, no
    network, no clock beyond ``now``).  The four checks are evaluated
    most-global-first; the first breach wins and short-circuits the rest.

    Args:
        order: the freshly-sized, fully-bracketed prospective order.  Read-only;
            only ``instrument`` and ``units`` are consulted (sizing/brackets are
            upstream concerns).
        open_positions: positions the store believes are currently open.  Each
            contributes ``position_risk`` (stop-distance, not notional) to the
            book-risk sum and an instrument to the correlation buckets.
        day_pl: today's P&L vs start-of-day equity (negative on a loss) from the
            ``account_state`` row (DRIFT-02).
        equity: current account equity in account currency; the book-risk cap is
            a fraction of this.  Non-finite/non-positive → reject (we never
            green-light against an untrustworthy equity).
        start_of_day_equity: the snapshotted start-of-day equity from
            ``account_state`` — the kill-switch baseline.
        config: the book-level caps (approved defaults in :class:`LimitsConfig`).
        now: UTC-aware current time; used only to compute the kill-switch reset
            boundary for reporting (INV-03).  Never an internal ``datetime.now``.
        order_risk: the prospective order's risk amount in account currency
            (the ``SizingResult.risk_amount`` already computed by sizing).  Must
            be finite and >= 0; this module does not re-derive it.
        returns: optional ``instrument → daily-return Series`` map for the
            correlation buckets (same shape ``signals.correlation.mid_returns``
            produces).  When ``None`` or sparse, instruments lacking a usable
            pair are treated as uncorrelated (conservative).

    Returns:
        A :class:`LimitDecision`.  ``allowed`` is ``True`` only when every check
        passes; otherwise ``reason`` explains the breach and, for the daily-loss
        case, ``kill_switch_active`` is ``True``.
    """
    cfg = config

    # --- 1. Daily-loss kill switch (most global; halts everything). ---------
    tripped, cap_amount = _kill_switch_tripped(
        day_pl, start_of_day_equity, cfg.daily_loss_cap
    )
    if tripped:
        reset_at = _next_utc_midnight(now)
        return _reject(
            f"Daily-loss kill switch active: day_pl={day_pl:.6g} <= "
            f"-{cap_amount:.6g} ({cfg.daily_loss_cap:.4g} × start-of-day equity "
            f"{start_of_day_equity:.6g}). No new entries until {reset_at.isoformat()}.",
            kill_switch_active=True,
        )

    # --- Guard equity before any fraction-of-equity comparison. -------------
    if not math.isfinite(equity) or equity <= 0:
        return _reject(
            f"equity must be finite and > 0 to evaluate book-risk, got {equity!r}."
        )
    if not math.isfinite(order_risk) or order_risk < 0:
        return _reject(
            f"order_risk must be finite and >= 0, got {order_risk!r}."
        )

    # --- 2. Max concurrent. -------------------------------------------------
    n_open = len(open_positions)
    if n_open >= cfg.max_concurrent:
        return _reject(
            f"Max concurrent positions reached: {n_open} open >= "
            f"max_concurrent={cfg.max_concurrent}."
        )

    # --- 3. Book risk (stop-distance risk, not notional). -------------------
    current_book_risk = book_risk_sum(open_positions)
    budget = book_risk_budget(equity, cfg)
    if current_book_risk + order_risk > budget:
        return _reject(
            f"Book-risk cap exceeded: current {current_book_risk:.6g} + order "
            f"{order_risk:.6g} = {current_book_risk + order_risk:.6g} > "
            f"{cfg.max_book_risk:.4g} × equity {equity:.6g} = "
            f"{budget:.6g}."
        )

    # --- 4. Correlation bucket (shared exposure). ---------------------------
    ret_map: Mapping[str, pd.Series] = returns if returns is not None else {}
    open_instruments = [p.instrument for p in open_positions]
    bucket = _correlation_bucket_instruments(
        order.instrument, open_instruments, ret_map, cfg.correlation_threshold
    )
    if len(bucket) > cfg.max_per_correlation_group:
        peers = sorted(bucket - {order.instrument})
        return _reject(
            f"Correlation-group cap exceeded: adding {order.instrument} makes a "
            f"bucket of {len(bucket)} correlated instruments "
            f"({', '.join(sorted(bucket))}) > max_per_correlation_group="
            f"{cfg.max_per_correlation_group} (|ρ| > {cfg.correlation_threshold:.4g} "
            f"with {', '.join(peers) if peers else 'open book'})."
        )

    # --- All checks passed: the gate permits the order onto the book. -------
    return _allow()


def kill_switch_status(
    *,
    day_pl: float,
    start_of_day_equity: float,
    config: LimitsConfig,
    now: datetime,
) -> KillSwitchStatus:
    """Read-only kill-switch snapshot for the CLI/monitor (no side effects).

    Reports whether the daily-loss kill switch is currently tripped, the figure
    that triggered it (``day_pl`` and the positive ``cap_amount``), and the next
    00:00 UTC reset boundary computed from ``now`` (INV-03).  Pure: identical
    inputs always yield an identical status; it mutates nothing and shares no
    state with :func:`check_limits`.
    """
    tripped, cap_amount = _kill_switch_tripped(
        day_pl, start_of_day_equity, config.daily_loss_cap
    )
    return KillSwitchStatus(
        active=tripped,
        day_pl=day_pl,
        cap_amount=cap_amount,
        reset_at=_next_utc_midnight(now),
    )
