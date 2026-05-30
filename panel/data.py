"""Read-only view models + accessors for the Fathom admin panel (P4-T-04).

This module is the *tested seam* between the SQLite store and the Streamlit
view layer.  All data logic lives here; the Streamlit app (``panel/app.py``,
next task) is a thin view over these tested view models.

Invariants enforced here
------------------------
* **INV-01** — read-only; no writes, no order/execution/sizing path.  This
  module imports ONLY ``data.store``, ``risk.limits`` (the two read helpers
  ``book_risk_sum`` / ``book_risk_budget`` — never ``check_limits`` or
  ``risk.sizing``), ``signals.ranker.Candidate``, and ``execution.models``
  (``Position`` / ``Fill`` types).  A transitive-import test in
  ``tests/test_panel_data.py`` asserts no forbidden module is reachable.
* **INV-03** — all timestamps in view models are UTC RFC 3339 strings (sourced
  directly from the store; never recomputed or re-formatted with a local
  clock).
* **INV-13** — ``watchlist()`` returns ``Candidate`` objects unchanged; no
  field is renamed or retyped.
* **INV-14/16** — ``unrealized_pl`` on the blotter is the reconciled
  passthrough from the ``Position`` model; it is **not** recomputed here and
  no live price call is made.

Accessor functions
------------------
* :func:`equity_series` — ``list[EquityPoint]`` from ``load_equity_snapshots``.
* :func:`blotter` — ``BlotterView`` with open positions + risk figures.
* :func:`watchlist` — ``list[Candidate]`` from the latest scan run.
* :func:`deviation_log` — ``list[DeviationRow]`` newest-first.
* :func:`chart_data` — ``ChartData`` with candles + position/watchlist overlays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pandas as pd

from data.store import Store, _to_rfc3339
from execution.models import Fill, Position
from risk.limits import LimitsConfig, book_risk_budget, book_risk_sum
from signals.ranker import Candidate

if TYPE_CHECKING:
    pass  # no further type-only imports needed


# ---------------------------------------------------------------------------
# Timeframe → granularity mapping (INV-13: keep "timeframe" dimension
# end-to-end; map only at the store call).
# ---------------------------------------------------------------------------

#: Maps the INV-13 ``Candidate.timeframe`` strings (e.g. ``"H1"``) to the
#: ``granularity`` argument that ``Store.load_candles`` accepts.  Currently
#: a 1-to-1 pass-through because OANDA uses the same strings — the mapping
#: layer exists so a future rename is a one-line change here, not scattered.
_TIMEFRAME_TO_GRANULARITY: dict[str, str] = {
    "S5": "S5",
    "S10": "S10",
    "S15": "S15",
    "S30": "S30",
    "M1": "M1",
    "M2": "M2",
    "M4": "M4",
    "M5": "M5",
    "M10": "M10",
    "M15": "M15",
    "M30": "M30",
    "H1": "H1",
    "H2": "H2",
    "H3": "H3",
    "H4": "H4",
    "H6": "H6",
    "H8": "H8",
    "H12": "H12",
    "D": "D",
    "W": "W",
    "M": "M",
}


def _timeframe_to_granularity(timeframe: str) -> str:
    """Map a ``timeframe`` dimension value to its ``granularity`` store argument.

    Passes through known values unchanged; unknown values are also passed through
    unchanged (OANDA strings match) so new granularities do not require a
    table update here — they work implicitly.

    Args:
        timeframe: The timeframe string (e.g. ``"H1"``).

    Returns:
        The granularity string for ``Store.load_candles``.
    """
    return _TIMEFRAME_TO_GRANULARITY.get(timeframe, timeframe)


# ---------------------------------------------------------------------------
# View models (frozen dataclasses — simple, inspectable, serialisable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityPoint:
    """One point on the equity curve.

    Attributes:
        as_of: UTC RFC 3339 timestamp of the snapshot (INV-03).
        equity: Broker NAV at ``as_of`` (broker-truth; INV-16).
        day_pl: Today's P&L vs start-of-day equity (negative on a loss).
        drawdown: ``(running_peak − equity) / running_peak``, a fraction ≥ 0.
            Exactly 0 at a new peak.  Spec resolution A-01: drawdown is
            never negative — at a new equity high it resets to 0.
    """

    as_of: str
    equity: float
    day_pl: float
    drawdown: float


@dataclass(frozen=True)
class BlotterRow:
    """One open position row for the blotter panel.

    Attributes:
        broker_trade_id: OANDA trade identifier.
        instrument: e.g. ``"EUR_USD"``.
        units: Signed position size (long > 0, short < 0).
        entry_price: The fill/entry price.
        stop_loss_price: Active bracket stop price.
        take_profit_price: Active bracket target price.
        unrealized_pl: Reconciled mark-to-market PnL (passthrough from the
            ``Position`` model — **not** recomputed here; INV-16 / D-05).
        opened_at: UTC RFC 3339 string (INV-03).
        candidate_ref: Provenance ``"instrument:timeframe:strategy_name"``.
    """

    broker_trade_id: str
    instrument: str
    units: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    unrealized_pl: float
    opened_at: str
    candidate_ref: str


@dataclass(frozen=True)
class BlotterView:
    """Aggregated view for the blotter panel.

    Attributes:
        positions: Open positions as ``BlotterRow`` instances, one per open
            trade (sorted by ``broker_trade_id``).
        day_pl: Today's total P&L vs start-of-day equity (from
            ``account_state``; negative on a loss).  ``None`` when the store
            has no ``account_state`` row (reconciliation has never run).
        start_of_day_equity: Start-of-day equity snapshot from
            ``account_state``.  ``None`` when reconciliation has never run.
        risk_in_use: ``book_risk_sum(open_positions)`` — aggregate
            stop-distance risk of the current book in account-price terms.
            Reuses the extracted ``risk/limits.py`` helper so this figure is
            byte-identical to the figure the kill switch evaluates (INV-05;
            DRIFT-02).
        risk_budget: ``book_risk_budget(equity, cfg)`` — the maximum allowed
            aggregate book risk.  ``None`` when ``equity`` is not available.
    """

    positions: list[BlotterRow]
    day_pl: float | None
    start_of_day_equity: float | None
    risk_in_use: float
    risk_budget: float | None


@dataclass(frozen=True)
class DeviationRow:
    """One deviation-log row for the deviation-log panel.

    Attributes:
        event_id: Stable PK of the deviation event.
        instrument: OANDA instrument identifier.
        deviation_type: Type string (e.g. ``"fill_discrepancy"``).
        detail: Human-readable description.
        broker_trade_id: OANDA trade id, or ``None`` when not position-linked.
        severity: ``"WARNING"`` | ``"CRITICAL"`` etc.
        created_at: UTC RFC 3339 string (INV-03).
        delivered: Whether the event has been posted to Discord.
    """

    event_id: str
    instrument: str
    deviation_type: str
    detail: str
    broker_trade_id: str | None
    severity: str
    created_at: str
    delivered: bool


@dataclass(frozen=True)
class Overlay:
    """One chart overlay for a given instrument.

    Attributes:
        label: ``"active"`` (open position) | ``"proposed"`` (watchlist
            candidate).  When both exist both overlays are included with their
            distinct labels (A-02 overlay-precedence resolution).
        entry: Reference entry price.
        stop: Stop-loss price.
        target: Take-profit price.
    """

    label: str
    entry: float
    stop: float
    target: float


@dataclass(frozen=True)
class ChartData:
    """Candle data + overlays for one instrument/timeframe combination.

    Attributes:
        instrument: OANDA instrument identifier.
        timeframe: The ``timeframe`` dimension (INV-13 — kept end-to-end;
            only mapped to ``granularity`` at the store call inside
            :func:`chart_data`).
        candles: DataFrame from ``Store.load_candles`` — columns
            ``time (datetime64[ns, UTC])``, ``open_bid``, ``high_bid``,
            ``low_bid``, ``close_bid``, ``open_ask``, ``high_ask``,
            ``low_ask``, ``close_ask``, ``volume``.
        overlays: Zero or more overlay lines.  An open ``Position`` for the
            instrument → an ``"active"`` overlay; a watchlist ``Candidate`` →
            a ``"proposed"`` overlay.  When both exist, both are included with
            distinct labels.
    """

    instrument: str
    timeframe: str
    candles: pd.DataFrame
    overlays: list[Overlay] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Accessor functions
# ---------------------------------------------------------------------------


def equity_series(
    store: Store,
    since: str | None = None,
) -> list[EquityPoint]:
    """Build the equity curve with running-peak drawdown.

    Loads all equity snapshots (optionally bounded by ``since``), computes a
    running peak, and returns a list of :class:`EquityPoint` objects ordered
    oldest-first (the store returns them ``as_of`` ascending).

    Drawdown formula (A-01 resolution)::

        drawdown = (running_peak - equity) / running_peak

    A fraction ≥ 0: exactly 0 at a new equity high; positive when equity is
    below the running peak.  The first point always has ``drawdown = 0``
    (it is the initial peak).

    Args:
        store: A ``Store`` instance.
        since: Optional RFC 3339 lower-bound for ``as_of`` (inclusive) — same
            semantics as ``Store.load_equity_snapshots(since=...)``.

    Returns:
        A list of :class:`EquityPoint`, oldest-first.  Empty when the store
        has no equity snapshots.
    """
    raw = store.load_equity_snapshots(since=since)
    if not raw:
        return []

    points: list[EquityPoint] = []
    running_peak: float = 0.0

    for row in raw:
        as_of = str(row["as_of"])
        equity = float(row["equity"])  # type: ignore[arg-type]
        day_pl = float(row["day_pl"])  # type: ignore[arg-type]

        # Update running peak.
        if equity >= running_peak:
            running_peak = equity
            drawdown = 0.0
        else:
            # running_peak > 0 guaranteed after the first iteration.
            drawdown = (running_peak - equity) / running_peak

        points.append(
            EquityPoint(
                as_of=as_of,
                equity=equity,
                day_pl=day_pl,
                drawdown=drawdown,
            )
        )

    return points


def blotter(
    store: Store,
    cfg: LimitsConfig | None = None,
) -> BlotterView:
    """Build the blotter view: open positions + risk figures.

    Loads open positions from the store, surfaces each position's reconciled
    ``unrealized_pl`` as a passthrough (no recompute, no live price — INV-16 /
    D-05), and computes risk-in-use + risk-budget from the extracted
    ``risk/limits.py`` helpers (DRIFT-02: the panel figure matches the kill
    switch exactly).

    Args:
        store: A ``Store`` instance.
        cfg: ``LimitsConfig`` for the book-risk budget calculation.  When
            ``None`` (default), uses ``LimitsConfig()`` (the approved
            Phase 3 defaults).

    Returns:
        A :class:`BlotterView`.
    """
    if cfg is None:
        cfg = LimitsConfig()

    open_positions: list[Position] = store.load_open_positions()
    account_state = store.load_account_state()

    day_pl: float | None = None
    start_of_day_equity: float | None = None
    risk_budget: float | None = None

    if account_state is not None:
        day_pl = float(account_state["day_pl"])  # type: ignore[arg-type]
        start_of_day_equity = float(account_state["start_of_day_equity"])  # type: ignore[arg-type]
        equity = start_of_day_equity + day_pl  # current equity estimate
        risk_budget = book_risk_budget(equity, cfg)

    risk_in_use = book_risk_sum(open_positions)

    rows: list[BlotterRow] = [
        BlotterRow(
            broker_trade_id=p.broker_trade_id,
            instrument=p.instrument,
            units=p.units,
            entry_price=p.entry_price,
            stop_loss_price=p.stop_loss_price,
            take_profit_price=p.take_profit_price,
            unrealized_pl=p.unrealized_pl,  # passthrough — no recompute (INV-16)
            opened_at=_to_rfc3339(p.opened_at),  # normalise to RFC 3339 Z (INV-03)
            candidate_ref=p.candidate_ref,
        )
        for p in open_positions
    ]

    return BlotterView(
        positions=rows,
        day_pl=day_pl,
        start_of_day_equity=start_of_day_equity,
        risk_in_use=risk_in_use,
        risk_budget=risk_budget,
    )


def watchlist(store: Store) -> list[Candidate]:
    """Return the latest scan run's candidates (INV-13 shape, unchanged).

    Loads the latest persisted watchlist from the store (the run with the
    highest ``run_timestamp``) and reconstructs ``Candidate`` objects from
    the stored rows.  The INV-13 shape is honoured exactly — no field is
    renamed, retyped, or reordered.

    Args:
        store: A ``Store`` instance.

    Returns:
        A list of :class:`Candidate` objects ordered by ``rank`` ascending.
        Empty when no watchlist run has been persisted yet.
    """
    rows = store.load_watchlist()
    return [Candidate(**{k: v for k, v in row.items()}) for row in rows]


def deviation_log(
    store: Store,
    limit: int | None = None,
) -> list[DeviationRow]:
    """Return deviation-log rows, newest-first.

    Args:
        store: A ``Store`` instance.
        limit: Maximum number of rows.  ``None`` returns all rows (the store's
            default).

    Returns:
        A list of :class:`DeviationRow`, ordered by ``created_at`` descending
        (newest first — the store's ``ORDER BY created_at DESC`` guarantees
        this).  Empty when the log has no entries.
    """
    raw = store.load_deviation_log(limit=limit)
    return [
        DeviationRow(
            event_id=str(row["event_id"]),
            instrument=str(row["instrument"]),
            deviation_type=str(row["deviation_type"]),
            detail=str(row["detail"]),
            broker_trade_id=(
                str(row["broker_trade_id"])
                if row["broker_trade_id"] is not None
                else None
            ),
            severity=str(row["severity"]),
            created_at=str(row["created_at"]),
            delivered=bool(row["delivered"]),
        )
        for row in raw
    ]


def chart_data(
    store: Store,
    instrument: str,
    timeframe: str,
    *,
    candle_start: datetime | None = None,
    candle_end: datetime | None = None,
) -> ChartData:
    """Build chart data (candles + overlays) for one instrument/timeframe pair.

    Loads candles from the store (mapping ``timeframe`` → ``granularity`` only
    here at the store call — INV-13 / D-05) and builds overlays from open
    positions and the watchlist.

    Overlay precedence (A-02 resolution):

    * If an open ``Position`` exists for ``instrument`` → include an
      ``"active"`` overlay (entry/stop/target from the position).
    * If a watchlist ``Candidate`` exists for ``instrument`` + ``timeframe`` →
      include a ``"proposed"`` overlay (entry_ref/stop_distance/target_distance
      converted to absolute prices using the candidate direction).
    * When both exist, **both** are included with their distinct labels.

    Args:
        store: A ``Store`` instance.
        instrument: OANDA instrument identifier, e.g. ``"EUR_USD"``.
        timeframe: The timeframe dimension (e.g. ``"H1"``); kept as-is in the
            returned ``ChartData`` (INV-13) and mapped to ``granularity`` only
            for the ``load_candles`` call.
        candle_start: Inclusive start of the candle range (UTC-aware).
            When ``None``, a 30-day lookback from ``datetime.now(timezone.utc)``
            is used as a sensible default for panel rendering.
        candle_end: Inclusive end of the candle range (UTC-aware).
            When ``None``, ``datetime.now(timezone.utc)`` is used.

    Returns:
        A :class:`ChartData` containing the candle DataFrame and any overlays.
    """
    now = datetime.now(timezone.utc)
    if candle_end is None:
        candle_end = now
    if candle_start is None:
        from datetime import timedelta
        candle_start = now - timedelta(days=30)

    granularity = _timeframe_to_granularity(timeframe)
    candles_df = store.load_candles(
        instrument=instrument,
        granularity=granularity,
        start=candle_start,
        end=candle_end,
    )

    overlays: list[Overlay] = []

    # --- Active overlay: open Position for this instrument -------------------
    open_positions = store.load_open_positions()
    for pos in open_positions:
        if pos.instrument == instrument:
            overlays.append(
                Overlay(
                    label="active",
                    entry=pos.entry_price,
                    stop=pos.stop_loss_price,
                    target=pos.take_profit_price,
                )
            )
            break  # Only one open position per instrument expected

    # --- Proposed overlay: watchlist Candidate for this instrument+timeframe -
    candidates = watchlist(store)
    for cand in candidates:
        if cand.instrument == instrument and cand.timeframe == timeframe:
            # Convert distance-based stop/target to absolute prices (for overlay
            # visual anchoring) using the candidate direction.
            entry = cand.entry_ref
            if cand.direction == "LONG":
                stop = entry - cand.stop_distance
                target = entry + cand.target_distance
            else:  # SHORT
                stop = entry + cand.stop_distance
                target = entry - cand.target_distance
            overlays.append(
                Overlay(
                    label="proposed",
                    entry=entry,
                    stop=stop,
                    target=target,
                )
            )
            break  # Only one candidate per instrument+timeframe expected

    return ChartData(
        instrument=instrument,
        timeframe=timeframe,
        candles=candles_df,
        overlays=overlays,
    )
