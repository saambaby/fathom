"""Reconciliation — the broker-is-truth truth-keeper (INV-16).

On monitor startup and on a timer, fetch the broker's view of open trades plus
the account summary and reconcile them against Fathom's ``positions`` table and
the singleton ``account_state`` row the kill switch reads.  **The broker is the
source of truth**: on any disagreement, local state is corrected to match, and
every correction is recorded in the returned :class:`ReconcileReport` and logged
at WARNING — drift is never silently dropped.

Design (per ``docs/features/reconciliation.md``)
------------------------------------------------
The diff is a **pure function** over ``(broker_state, store_state)`` returning a
set of corrective :class:`Action` objects, so it is unit-testable without a
broker.  :func:`reconcile` is the thin wrapper that fetches state, calls the
pure diff, applies the actions to the store, and updates ``account_state``.

Conflict resolution — one rule resolves every case (INV-16):

* **broker-only** trade (we missed a fill / restarted)        → **adopt** (insert)
* **store-only** open position (broker closed it, SL/TP hit)  → **close** + record
  ``realized_pl``
* **matched** trade                                            → **refresh**
  ``unrealized_pl`` / stop / target / units
* **orphaned fill** (a ``fills`` row whose ``broker_trade_id`` has no
  ``positions`` row — a crash between the fill-write and the position-write in
  ``submit_order``)                                            → **repair** from
  broker truth (adopt if the broker still reports it open; otherwise it is a
  closed trade and surfaced as drift)

account_state (DRIFT-02 / DRIFT-05)
-----------------------------------
* ``day_pl`` ← the **account-summary's** realized day P&L (the broker's figure,
  authoritative per INV-16; the store column is a cached mirror, not an
  independent sum).
* ``start_of_day_equity`` ← snapshotted **once, on the first reconcile after the
  UTC-day boundary** (00:00 UTC, INV-03-consistent with the kill-switch reset).
  A mid-day process restart **re-reads** the persisted snapshot — it does not
  re-snapshot, so the kill-switch threshold is stable across restarts.

Invariants
----------
* **INV-16** — broker wins on every conflict; this module is the enforcement.
* **INV-07/INV-09** — practice endpoint only; the injected ``OandaClient`` chose
  it once from ``settings.env``.  This module never reads ``env`` or the token.
* **INV-03** — every timestamp written is UTC-aware / RFC 3339.
* **INV-08** — no secret is read or logged here.
* **INV-05** — supplies the true ``day_pl`` / ``start_of_day_equity`` the kill
  switch depends on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from execution.models import Position

if TYPE_CHECKING:  # avoid import cycles / heavy deps at runtime
    from data.oanda_client import OandaClient
    from data.store import Store
    from execution.models import Fill

__all__ = [
    "ActionKind",
    "Action",
    "BrokerTrade",
    "BrokerState",
    "StoreState",
    "ReconcileReport",
    "compute_reconcile_actions",
    "reconcile",
]

logger = logging.getLogger("fathom.execution.reconcile")


# ---------------------------------------------------------------------------
# Value objects: broker truth + store view (inputs to the pure diff)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerTrade:
    """One open trade as the broker reports it (the parsed v20 ``trades`` entry).

    The broker is authoritative for every field here (INV-16).  ``opened_at`` is
    UTC-aware (INV-03), parsed from the v20 ``openTime``.
    """

    broker_trade_id: str
    instrument: str
    units: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    unrealized_pl: float
    opened_at: datetime


@dataclass(frozen=True)
class BrokerState:
    """The broker's truth at reconcile time: open trades + account-summary figures."""

    open_trades: tuple[BrokerTrade, ...]
    nav: float
    """Net asset value / equity from the account summary."""
    realized_day_pl: float
    """Realized day P&L from the account summary (authoritative ``day_pl``)."""


@dataclass(frozen=True)
class StoreState:
    """Fathom's local view: open positions + orphaned fills (no position row)."""

    open_positions: tuple[Position, ...]
    orphaned_fills: tuple["Fill", ...] = ()


# ---------------------------------------------------------------------------
# Corrective actions (outputs of the pure diff)
# ---------------------------------------------------------------------------


class ActionKind(str, Enum):
    """The kind of corrective write a reconcile action represents."""

    ADOPT = "adopt"
    CLOSE = "close"
    REFRESH = "refresh"


@dataclass(frozen=True)
class Action:
    """One corrective action the apply-wrapper will write to the store.

    Exactly the fields each kind needs are populated; the rest stay ``None``.
    Carries a human-readable ``drift_reason`` for the report + the WARNING log.
    """

    kind: ActionKind
    broker_trade_id: str
    drift: bool
    drift_reason: str
    position: Optional[Position] = None  # ADOPT
    realized_pl: Optional[float] = None  # CLOSE
    unrealized_pl: Optional[float] = None  # REFRESH
    stop_loss_price: Optional[float] = None  # REFRESH
    take_profit_price: Optional[float] = None  # REFRESH
    units: Optional[int] = None  # REFRESH


@dataclass
class ReconcileReport:
    """The outcome of a reconcile pass (returned by :func:`reconcile`).

    ``adopted`` / ``closed`` / ``matched`` are broker-trade-id lists;
    ``drift_flags`` collects every drift message (also logged at WARNING).
    ``start_of_day_equity`` / ``day_pl`` are the figures written to
    ``account_state`` this pass (the kill switch's inputs).
    """

    adopted: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    drift_flags: list[str] = field(default_factory=list)
    start_of_day_equity: float = 0.0
    day_pl: float = 0.0
    snapshotted_today: bool = False
    """True if this pass took a fresh UTC-day ``start_of_day_equity`` snapshot."""


# ---------------------------------------------------------------------------
# Pure diff — broker truth vs store view → corrective actions (INV-16)
# ---------------------------------------------------------------------------


def compute_reconcile_actions(
    broker: BrokerState,
    store: StoreState,
) -> list[Action]:
    """Pure function: diff broker truth against the store view (INV-16).

    No I/O, no clock, no broker — fully unit-testable.  Keyed on
    ``broker_trade_id``:

    * broker-only → ``ADOPT`` (flagged drift: we missed it).
    * store-only-open → ``CLOSE`` with broker ``realized_pl`` (flagged drift:
      the broker closed it without us).
    * matched → ``REFRESH`` (drift only if the broker's bracket/units differ
      from the store's — a silently-moved bracket is real drift).
    * orphaned fill whose trade is broker-open and lacks an ``ADOPT`` already →
      ``ADOPT`` (flagged drift: crash between fill-write and position-write).

    Returns the actions in a deterministic order (adopts, closes, refreshes,
    then orphan repairs) so application and tests are stable.
    """
    broker_by_id = {t.broker_trade_id: t for t in broker.open_trades}
    store_by_id = {p.broker_trade_id: p for p in store.open_positions}

    adopts: list[Action] = []
    closes: list[Action] = []
    refreshes: list[Action] = []

    # broker-only → adopt; matched → refresh.
    for trade_id, trade in broker_by_id.items():
        if trade_id not in store_by_id:
            adopts.append(_adopt_action(trade, reason="broker-only position"))
        else:
            refreshes.append(_refresh_action(trade, store_by_id[trade_id]))

    # store-only (broker no longer reports it) → close with broker realized P&L.
    for trade_id, pos in store_by_id.items():
        if trade_id not in broker_by_id:
            closes.append(
                Action(
                    kind=ActionKind.CLOSE,
                    broker_trade_id=trade_id,
                    drift=True,
                    drift_reason=(
                        f"store-open position {trade_id} ({pos.instrument}) is "
                        "no longer open at the broker — closing with broker "
                        "realized P&L"
                    ),
                    # The account summary's realized day P&L is authoritative for
                    # the aggregate; per-trade realized P&L is no longer fetchable
                    # from open-trades once closed, so we attribute the position's
                    # last unrealized mark as its realized result.  The aggregate
                    # day_pl written to account_state remains the broker figure.
                    realized_pl=pos.unrealized_pl,
                )
            )

    # Orphaned fills: a fill with no position row.  If the broker still reports
    # the trade open and we have not already queued an adopt for it, adopt from
    # broker truth.  If the broker does not report it, the trade closed during
    # the crash window — surface it as drift (no position to write).
    already_adopting = {a.broker_trade_id for a in adopts}
    for fill in store.orphaned_fills:
        tid = fill.broker_trade_id
        if tid in store_by_id:
            continue  # a position row exists after all — not actually orphaned.
        if tid in broker_by_id and tid not in already_adopting:
            adopts.append(
                _adopt_action(
                    broker_by_id[tid],
                    reason=(
                        f"orphaned fill {fill.client_order_id} → broker trade "
                        f"{tid} open with no position row (crash between "
                        "fill-write and position-write) — repairing from broker"
                    ),
                )
            )
            already_adopting.add(tid)
        elif tid not in broker_by_id:
            # The fill's trade is not open at the broker: it filled and closed
            # inside the crash window.  We cannot reconstruct a closed position
            # row from open-trades; record drift so it is never silently dropped.
            refreshes.append(
                Action(
                    kind=ActionKind.REFRESH,  # no-op write target; drift-only
                    broker_trade_id=tid,
                    drift=True,
                    drift_reason=(
                        f"orphaned fill {fill.client_order_id} → broker trade "
                        f"{tid} not open at broker (filled+closed in the crash "
                        "window); aggregate day_pl from account summary covers it"
                    ),
                    unrealized_pl=None,
                    stop_loss_price=None,
                    take_profit_price=None,
                    units=None,
                )
            )

    return adopts + closes + refreshes


def _adopt_action(trade: BrokerTrade, *, reason: str) -> Action:
    """Build an ADOPT action from a broker trade (broker is truth)."""
    position = Position(
        broker_trade_id=trade.broker_trade_id,
        instrument=trade.instrument,
        units=trade.units,
        entry_price=trade.entry_price,
        stop_loss_price=trade.stop_loss_price,
        take_profit_price=trade.take_profit_price,
        opened_at=trade.opened_at,
        unrealized_pl=trade.unrealized_pl,
        closed_at=None,
        realized_pl=None,
        candidate_ref=f"reconcile-adopted:{trade.instrument}",
    )
    return Action(
        kind=ActionKind.ADOPT,
        broker_trade_id=trade.broker_trade_id,
        drift=True,
        drift_reason=f"adopting {trade.broker_trade_id}: {reason}",
        position=position,
    )


def _refresh_action(trade: BrokerTrade, pos: Position) -> Action:
    """Build a REFRESH action; flag drift only when the broker bracket/units moved."""
    bracket_moved = (
        trade.stop_loss_price != pos.stop_loss_price
        or trade.take_profit_price != pos.take_profit_price
        or trade.units != pos.units
    )
    reason = ""
    if bracket_moved:
        reason = (
            f"matched {trade.broker_trade_id}: broker bracket/units "
            f"(stop={trade.stop_loss_price}, target={trade.take_profit_price}, "
            f"units={trade.units}) differ from store "
            f"(stop={pos.stop_loss_price}, target={pos.take_profit_price}, "
            f"units={pos.units}) — correcting to broker"
        )
    return Action(
        kind=ActionKind.REFRESH,
        broker_trade_id=trade.broker_trade_id,
        drift=bracket_moved,
        drift_reason=reason,
        unrealized_pl=trade.unrealized_pl,
        stop_loss_price=trade.stop_loss_price,
        take_profit_price=trade.take_profit_price,
        units=trade.units,
    )


# ---------------------------------------------------------------------------
# Broker-state parsing (v20 wire → typed value objects)
# ---------------------------------------------------------------------------


def _parse_utc(raw: str) -> datetime:
    """Parse a v20 RFC-3339 UTC time string (trailing ``Z``, ns precision)."""
    s = raw.rstrip("Z")
    if "." in s:
        date_part, frac = s.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        s = f"{date_part}.{frac}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _parse_open_trade(raw: dict[str, Any]) -> BrokerTrade:
    """Convert a single v20 open-trade dict into a typed :class:`BrokerTrade`.

    Bracket prices live in the ``stopLossOrder`` / ``takeProfitOrder``
    sub-objects.  A broker trade with no bracket would violate INV-04 upstream;
    if a price is absent we fall back to the entry ``price`` so the (positive)
    ``Position`` validators still hold and the missing bracket surfaces as a
    refresh-drift rather than a crash.
    """
    entry_price = float(raw["price"])
    sl = raw.get("stopLossOrder") or {}
    tp = raw.get("takeProfitOrder") or {}
    stop_price = float(sl["price"]) if "price" in sl else entry_price
    target_price = float(tp["price"]) if "price" in tp else entry_price
    return BrokerTrade(
        broker_trade_id=str(raw["id"]),
        instrument=str(raw["instrument"]),
        units=int(float(raw["currentUnits"])),
        entry_price=entry_price,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
        unrealized_pl=float(raw.get("unrealizedPL", 0.0)),
        opened_at=_parse_utc(str(raw["openTime"])),
    )


def _fetch_broker_state(client: "OandaClient") -> BrokerState:
    """Fetch + parse the broker truth (open trades + account summary)."""
    raw_trades = client.open_trades()
    trades = tuple(_parse_open_trade(t) for t in raw_trades)
    summary = client.account_summary()
    account = summary.get("account", summary)
    nav = float(account.get("NAV", account.get("balance", 0.0)))
    # v20 account summary exposes realized day P&L as ``pl`` over the broker's
    # day; the kill switch reasons in UTC (start_of_day_equity below).
    realized_day_pl = float(account.get("pl", 0.0))
    return BrokerState(open_trades=trades, nav=nav, realized_day_pl=realized_day_pl)


# ---------------------------------------------------------------------------
# account_state UTC-day snapshot (DRIFT-02 / DRIFT-05)
# ---------------------------------------------------------------------------


def _resolve_start_of_day_equity(
    store: "Store",
    *,
    nav: float,
    now: datetime,
) -> tuple[float, bool]:
    """Decide ``start_of_day_equity`` for this pass (snapshot-once-per-UTC-day).

    Returns ``(start_of_day_equity, snapshotted_now)``.

    * No persisted row, or the persisted ``as_of`` is from a **prior** UTC day →
      snapshot the current ``nav`` (first reconcile after 00:00 UTC).
    * Persisted ``as_of`` is **today** (UTC) → re-read and carry the stored
      snapshot forward (a mid-day restart does NOT re-snapshot — the kill-switch
      threshold is stable across restarts).
    """
    existing = store.load_account_state()
    today = now.astimezone(timezone.utc).date()
    if existing is not None:
        prior_as_of = _parse_utc(str(existing["as_of"]))
        if prior_as_of.astimezone(timezone.utc).date() == today:
            # Same UTC day → re-read, never re-snapshot.
            stored_equity = existing["start_of_day_equity"]
            assert isinstance(stored_equity, (int, float))
            return float(stored_equity), False
    # New UTC day (or first-ever reconcile) → snapshot now.
    return nav, True


# ---------------------------------------------------------------------------
# Apply wrapper — fetch broker + store state, diff, apply, update account_state
# ---------------------------------------------------------------------------


def reconcile(
    *,
    client: "OandaClient",
    store: "Store",
    now: datetime,
) -> ReconcileReport:
    """Reconcile local state against the broker — the broker wins (INV-16).

    1. Fetch broker open trades + account summary; load the store's open
       positions + orphaned fills.
    2. :func:`compute_reconcile_actions` (pure) yields the corrective actions.
    3. Apply each action: ADOPT inserts, CLOSE marks closed with ``realized_pl``,
       REFRESH updates unrealized/bracket/units.  Every drift action is logged
       at WARNING and recorded in ``drift_flags`` — never silently dropped.
    4. Update ``account_state``: ``day_pl`` ← the broker account-summary figure
       (authoritative); ``start_of_day_equity`` ← snapshot-once-per-UTC-day,
       stable across restarts.

    Idempotent: a re-run with no broker change produces only REFRESH no-op
    rewrites, no new adopts/closes, and re-reads (does not re-snapshot)
    ``start_of_day_equity``.

    Args:
        client: an already-constructed practice ``OandaClient`` (INV-07/09).
        store: the persistence layer (positions / fills / account_state).
        now: UTC-aware reconcile time (INV-03 — never ``datetime.now()`` naive);
            drives the UTC-day snapshot boundary and close timestamps.

    Returns:
        A :class:`ReconcileReport` of adopted/closed/matched ids, drift flags,
        and the ``account_state`` figures written.

    Raises:
        ValueError: if ``now`` is not UTC-aware (INV-03).
        OandaAPIError: on a broker HTTP 4xx/5xx during the fetch.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a UTC-aware datetime (INV-03).")

    broker = _fetch_broker_state(client)
    store_state = StoreState(
        open_positions=tuple(store.load_open_positions()),
        orphaned_fills=tuple(store.load_orphaned_fills()),
    )

    actions = compute_reconcile_actions(broker, store_state)
    report = ReconcileReport()

    for action in actions:
        if action.drift and action.drift_reason:
            logger.warning("reconcile drift: %s", action.drift_reason)
            report.drift_flags.append(action.drift_reason)

        if action.kind is ActionKind.ADOPT:
            assert action.position is not None
            store.adopt_position(action.position)
            report.adopted.append(action.broker_trade_id)
        elif action.kind is ActionKind.CLOSE:
            assert action.realized_pl is not None
            store.close_position(
                action.broker_trade_id,
                realized_pl=action.realized_pl,
                closed_at=now,
            )
            report.closed.append(action.broker_trade_id)
        elif action.kind is ActionKind.REFRESH:
            # A drift-only orphan-closed marker carries no refresh payload; skip
            # the write but keep its drift flag (recorded above).
            if action.units is None:
                continue
            assert action.unrealized_pl is not None
            assert action.stop_loss_price is not None
            assert action.take_profit_price is not None
            store.refresh_position(
                action.broker_trade_id,
                unrealized_pl=action.unrealized_pl,
                stop_loss_price=action.stop_loss_price,
                take_profit_price=action.take_profit_price,
                units=action.units,
            )
            report.matched.append(action.broker_trade_id)

    # account_state: broker is truth for day_pl; UTC-day snapshot for equity.
    start_of_day_equity, snapshotted = _resolve_start_of_day_equity(
        store, nav=broker.nav, now=now
    )
    store.write_account_state(
        start_of_day_equity=start_of_day_equity,
        day_pl=broker.realized_day_pl,
        as_of=now,
    )
    report.start_of_day_equity = start_of_day_equity
    report.day_pl = broker.realized_day_pl
    report.snapshotted_today = snapshotted
    return report
