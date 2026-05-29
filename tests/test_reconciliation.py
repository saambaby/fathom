"""Tests for execution.reconcile — the broker-is-truth reconciler (INV-16).

Two layers:

* **Pure diff** (``compute_reconcile_actions``) — no broker, no I/O: adopt
  broker-only, close store-only, refresh matched, repair orphaned fills.
* **Apply wrapper** (``reconcile``) — v20 mocked with the ``responses`` library
  (no live HTTP): adoption inserts, close writes ``realized_pl``, matched
  refresh, ``account_state`` UTC-day snapshot once + stable across restart,
  drift logged at WARNING, idempotent re-run, orphaned-fill repair.

INV-07 (practice endpoint), INV-03 (UTC), INV-08 (no secret logged) are checked
implicitly: the client is built from ``env="demo"`` so all URLs are
``api-fxpractice``; every persisted timestamp is RFC 3339 ``Z``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest
import responses as resp_lib
from pydantic import SecretStr

from config.settings import Settings
from data.oanda_client import OandaClient
from data.store import Store
from execution.models import Fill, FillStatus, Position
from execution.reconcile import (
    Action,
    ActionKind,
    BrokerState,
    BrokerTrade,
    StoreState,
    compute_reconcile_actions,
    reconcile,
)

PRACTICE_BASE = "https://api-fxpractice.oanda.com"
ACCOUNT_ID = "001-001-1234567-001"
OPEN_TRADES_URL = f"{PRACTICE_BASE}/v3/accounts/{ACCOUNT_ID}/openTrades"
SUMMARY_URL = f"{PRACTICE_BASE}/v3/accounts/{ACCOUNT_ID}/summary"

NOW = datetime(2026, 5, 29, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        env="demo",
        oanda_api_token=SecretStr("super-secret-token-DO-NOT-LEAK"),
        oanda_account_id=ACCOUNT_ID,
    )


def _client() -> OandaClient:
    return OandaClient(_settings())


def _store() -> Store:
    return Store(db_path=":memory:")


def _broker_trade(
    *,
    trade_id: str = "T1",
    instrument: str = "EUR_USD",
    units: int = 10_000,
    entry: float = 1.08500,
    stop: float = 1.08000,
    target: float = 1.09500,
    upl: float = 3.21,
    opened: datetime = NOW,
) -> BrokerTrade:
    return BrokerTrade(
        broker_trade_id=trade_id,
        instrument=instrument,
        units=units,
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=target,
        unrealized_pl=upl,
        opened_at=opened,
    )


def _position(
    *,
    trade_id: str = "T1",
    instrument: str = "EUR_USD",
    units: int = 10_000,
    entry: float = 1.08500,
    stop: float = 1.08000,
    target: float = 1.09500,
    upl: float = 0.0,
) -> Position:
    return Position(
        broker_trade_id=trade_id,
        instrument=instrument,
        units=units,
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=target,
        opened_at=NOW,
        unrealized_pl=upl,
        closed_at=None,
        realized_pl=None,
        candidate_ref="EUR_USD:H1:macrossover",
    )


def _open_trade_json(
    *,
    trade_id: str = "T1",
    instrument: str = "EUR_USD",
    units: str = "10000",
    price: str = "1.08500",
    stop: str = "1.08000",
    target: str = "1.09500",
    upl: str = "3.21",
    open_time: str = "2026-05-29T12:00:00.000000000Z",
) -> dict[str, Any]:
    return {
        "id": trade_id,
        "instrument": instrument,
        "currentUnits": units,
        "price": price,
        "unrealizedPL": upl,
        "openTime": open_time,
        "stopLossOrder": {"price": stop},
        "takeProfitOrder": {"price": target},
    }


def _open_trades_response(*trades: dict[str, Any]) -> dict[str, Any]:
    return {"trades": list(trades), "lastTransactionID": "100"}


def _summary_response(*, nav: str = "10050.00", pl: str = "50.00") -> dict[str, Any]:
    return {
        "account": {
            "id": ACCOUNT_ID,
            "NAV": nav,
            "balance": "10000.00",
            "pl": pl,
            "unrealizedPL": "0.00",
        },
        "lastTransactionID": "100",
    }


def _register(open_trades: dict[str, Any], summary: dict[str, Any]) -> None:
    resp_lib.add(resp_lib.GET, OPEN_TRADES_URL, json=open_trades, status=200)
    resp_lib.add(resp_lib.GET, SUMMARY_URL, json=summary, status=200)


# ===========================================================================
# Pure diff (compute_reconcile_actions) — no broker, no I/O
# ===========================================================================


class TestPureDiff:
    def test_broker_only_is_adopted(self) -> None:
        broker = BrokerState(
            open_trades=(_broker_trade(trade_id="T1"),), nav=10_000.0, realized_day_pl=0.0
        )
        store = StoreState(open_positions=())
        actions = compute_reconcile_actions(broker, store)
        assert len(actions) == 1
        a = actions[0]
        assert a.kind is ActionKind.ADOPT and a.broker_trade_id == "T1"
        assert a.drift is True
        assert a.position is not None and a.position.broker_trade_id == "T1"

    def test_store_only_is_closed_with_realized_pl(self) -> None:
        # Store thinks T1 open; broker reports nothing → close it.
        broker = BrokerState(open_trades=(), nav=9_900.0, realized_day_pl=-100.0)
        store = StoreState(open_positions=(_position(trade_id="T1", upl=-42.0),))
        actions = compute_reconcile_actions(broker, store)
        assert len(actions) == 1
        a = actions[0]
        assert a.kind is ActionKind.CLOSE and a.broker_trade_id == "T1"
        assert a.drift is True
        assert a.realized_pl == -42.0

    def test_matched_is_refreshed_no_drift_when_identical(self) -> None:
        broker = BrokerState(
            open_trades=(_broker_trade(trade_id="T1", upl=9.0),),
            nav=10_000.0,
            realized_day_pl=0.0,
        )
        store = StoreState(open_positions=(_position(trade_id="T1", upl=0.0),))
        actions = compute_reconcile_actions(broker, store)
        assert len(actions) == 1
        a = actions[0]
        assert a.kind is ActionKind.REFRESH and a.broker_trade_id == "T1"
        assert a.drift is False  # same bracket/units → no drift
        assert a.unrealized_pl == 9.0

    def test_matched_moved_bracket_flags_drift(self) -> None:
        broker = BrokerState(
            open_trades=(_broker_trade(trade_id="T1", stop=1.07000),),
            nav=10_000.0,
            realized_day_pl=0.0,
        )
        store = StoreState(open_positions=(_position(trade_id="T1", stop=1.08000),))
        actions = compute_reconcile_actions(broker, store)
        assert actions[0].drift is True
        assert actions[0].stop_loss_price == 1.07000

    def test_orphaned_fill_open_at_broker_is_adopted_once(self) -> None:
        # Broker reports T1 open; store has the fill but no position row.
        fill = Fill(
            client_order_id="c" * 32,
            broker_trade_id="T1",
            fill_price=1.08500,
            units_filled=10_000,
            slippage=0.0,
            filled_at=NOW,
            status=FillStatus.FILLED,
        )
        broker = BrokerState(
            open_trades=(_broker_trade(trade_id="T1"),), nav=10_000.0, realized_day_pl=0.0
        )
        store = StoreState(open_positions=(), orphaned_fills=(fill,))
        actions = compute_reconcile_actions(broker, store)
        # Exactly ONE adopt — the broker-only loop and the orphan loop must not
        # both queue T1.
        adopts = [a for a in actions if a.kind is ActionKind.ADOPT]
        assert len(adopts) == 1 and adopts[0].broker_trade_id == "T1"

    def test_orphaned_fill_closed_at_broker_is_drift_only(self) -> None:
        fill = Fill(
            client_order_id="d" * 32,
            broker_trade_id="T9",
            fill_price=1.08500,
            units_filled=10_000,
            slippage=0.0,
            filled_at=NOW,
            status=FillStatus.FILLED,
        )
        broker = BrokerState(open_trades=(), nav=10_000.0, realized_day_pl=-10.0)
        store = StoreState(open_positions=(), orphaned_fills=(fill,))
        actions = compute_reconcile_actions(broker, store)
        assert len(actions) == 1
        assert actions[0].drift is True and actions[0].units is None


# ===========================================================================
# Apply wrapper (reconcile) — v20 mocked with responses
# ===========================================================================


class TestAdoption:
    @resp_lib.activate
    def test_broker_only_position_adopted(self) -> None:
        _register(_open_trades_response(_open_trade_json(trade_id="T1")), _summary_response())
        store = _store()
        report = reconcile(client=_client(), store=store, now=NOW)
        assert report.adopted == ["T1"]
        rows = store.load_open_positions()
        assert len(rows) == 1 and rows[0].broker_trade_id == "T1"
        assert rows[0].instrument == "EUR_USD"
        # Practice endpoint only (INV-07): the URL hit was api-fxpractice.
        assert all("fxpractice" in (c.request.url or "") for c in resp_lib.calls)


class TestClose:
    @resp_lib.activate
    def test_store_only_position_closed_with_realized_pl(self) -> None:
        # Broker reports NO open trades; store has T1 open → close it.
        _register(_open_trades_response(), _summary_response(nav="9900.00", pl="-100.00"))
        store = _store()
        store.write_position(_position(trade_id="T1", upl=-42.0))
        report = reconcile(client=_client(), store=store, now=NOW)
        assert report.closed == ["T1"]
        # No longer open.
        assert store.load_open_positions() == []
        # realized_pl + closed_at written.
        cur = store._conn.execute(
            "SELECT realized_pl, closed_at FROM positions WHERE broker_trade_id = 'T1'"
        )
        realized_pl, closed_at = cur.fetchone()
        assert realized_pl == -42.0
        assert closed_at is not None and closed_at.endswith("Z")  # RFC 3339 (INV-03)
        # account_state.day_pl mirrors the broker account-summary figure.
        state = store.load_account_state()
        assert state is not None and state["day_pl"] == -100.0


class TestMatchedRefresh:
    @resp_lib.activate
    def test_matched_position_refreshed_from_broker(self) -> None:
        _register(
            _open_trades_response(
                _open_trade_json(trade_id="T1", upl="12.34", stop="1.07500")
            ),
            _summary_response(),
        )
        store = _store()
        store.write_position(_position(trade_id="T1", upl=0.0, stop=1.08000))
        report = reconcile(client=_client(), store=store, now=NOW)
        assert report.matched == ["T1"]
        pos = store.load_open_positions()[0]
        assert pos.unrealized_pl == 12.34
        assert pos.stop_loss_price == 1.07500  # corrected to broker truth
        # Moved bracket → drift recorded.
        assert any("T1" in f for f in report.drift_flags)


class TestAccountStateSnapshot:
    @resp_lib.activate
    def test_snapshot_once_per_utc_day_and_stable_across_restart(self) -> None:
        store = _store()

        # First reconcile of the UTC day → snapshot start_of_day_equity = NAV.
        _register(_open_trades_response(), _summary_response(nav="10050.00", pl="0.00"))
        r1 = reconcile(client=_client(), store=store, now=NOW)
        assert r1.snapshotted_today is True
        assert r1.start_of_day_equity == 10050.0
        resp_lib.reset()

        # Mid-day "restart": NAV has moved, but same UTC day → re-read, NOT
        # re-snapshot. start_of_day_equity stays the morning figure; day_pl
        # tracks the broker.
        _register(_open_trades_response(), _summary_response(nav="9800.00", pl="-250.00"))
        later = NOW.replace(hour=20)
        r2 = reconcile(client=_client(), store=store, now=later)
        assert r2.snapshotted_today is False
        assert r2.start_of_day_equity == 10050.0  # stable across restart
        assert r2.day_pl == -250.0

    @resp_lib.activate
    def test_new_utc_day_resnapshots(self) -> None:
        store = _store()
        _register(_open_trades_response(), _summary_response(nav="10050.00", pl="0.00"))
        reconcile(client=_client(), store=store, now=NOW)
        resp_lib.reset()

        # Next UTC day, fresh NAV → new snapshot.
        _register(_open_trades_response(), _summary_response(nav="9800.00", pl="0.00"))
        next_day = datetime(2026, 5, 30, 0, 5, 0, tzinfo=timezone.utc)
        r = reconcile(client=_client(), store=store, now=next_day)
        assert r.snapshotted_today is True
        assert r.start_of_day_equity == 9800.0


class TestDriftLogging:
    @resp_lib.activate
    def test_drift_logged_at_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        _register(_open_trades_response(_open_trade_json(trade_id="T1")), _summary_response())
        store = _store()
        with caplog.at_level(logging.WARNING, logger="fathom.execution.reconcile"):
            report = reconcile(client=_client(), store=store, now=NOW)
        assert report.drift_flags  # adoption is drift
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert any("adopting T1" in r.getMessage() for r in caplog.records)


class TestIdempotency:
    @resp_lib.activate
    def test_rerun_no_broker_change_is_noop(self) -> None:
        store = _store()
        # Two identical reconcile passes, same open trade.
        for _ in range(2):
            _register(
                _open_trades_response(_open_trade_json(trade_id="T1")),
                _summary_response(),
            )
            reconcile(client=_client(), store=store, now=NOW)
            resp_lib.reset()
        # Exactly one position row (no duplicate adoption); still open.
        cur = store._conn.execute("SELECT COUNT(*) FROM positions")
        assert cur.fetchone()[0] == 1
        assert len(store.load_open_positions()) == 1
        # Second pass adopted nothing.
        _register(_open_trades_response(_open_trade_json(trade_id="T1")), _summary_response())
        report = reconcile(client=_client(), store=store, now=NOW)
        assert report.adopted == []
        assert report.matched == ["T1"]  # now a refresh, not an adopt


class TestOrphanedFillRepair:
    @resp_lib.activate
    def test_orphaned_fill_repaired_from_broker(self) -> None:
        # Simulate the crash window: a fill row exists, but no position row,
        # and the broker still reports the trade open.
        store = _store()
        store.write_fill(
            Fill(
                client_order_id="e" * 32,
                broker_trade_id="T1",
                fill_price=1.08500,
                units_filled=10_000,
                slippage=0.0,
                filled_at=NOW,
                status=FillStatus.FILLED,
            )
        )
        # Sanity: store detects the orphan before reconcile.
        assert len(store.load_orphaned_fills()) == 1
        assert store.load_open_positions() == []

        _register(_open_trades_response(_open_trade_json(trade_id="T1")), _summary_response())
        report = reconcile(client=_client(), store=store, now=NOW)

        assert report.adopted == ["T1"]
        # Position now exists → no longer orphaned.
        assert len(store.load_open_positions()) == 1
        assert store.load_orphaned_fills() == []

    def test_rejection_row_is_not_an_orphan(self) -> None:
        # A rejected order legitimately has no position; it must NOT be picked
        # up as an orphaned fill (would otherwise falsely flag drift / adopt).
        store = _store()
        store.write_rejection("g" * 32, rejected_at=NOW, reason="MARKET_HALTED")
        assert store.load_orphaned_fills() == []

    @resp_lib.activate
    def test_orphaned_fill_closed_at_broker_is_drift_only(self) -> None:
        store = _store()
        store.write_fill(
            Fill(
                client_order_id="f" * 32,
                broker_trade_id="T9",
                fill_price=1.08500,
                units_filled=10_000,
                slippage=0.0,
                filled_at=NOW,
                status=FillStatus.FILLED,
            )
        )
        _register(_open_trades_response(), _summary_response(pl="-10.00"))
        report = reconcile(client=_client(), store=store, now=NOW)
        # No position adopted (broker doesn't report it), but drift recorded.
        assert report.adopted == []
        assert any("T9" in f for f in report.drift_flags)
