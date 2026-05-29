"""Unit tests for execution.orders.submit_order + the execution store tables.

Mocks OANDA v20 with the ``responses`` library — NO live HTTP. Covers the
order-placement acceptance criteria:

* atomic bracket: SL + TP in ONE v20 request (INV-04);
* idempotency: a duplicate ``client_order_id`` does not create a second broker
  order (INV-15);
* a transient error then success yields exactly ONE fill (retry de-dupes);
* slippage = signed (positive = adverse) vs ``Candidate.entry_ref``;
* a rejection records ``status="rejected"`` with no position; a partial fill
  records ``units_filled < units`` and ``status="partial"``;
* UTC RFC-3339 timestamps on persisted rows (INV-03);
* no secret appears in any persisted row (INV-08).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
import responses as resp_lib
from pydantic import SecretStr
from responses import matchers

from config.settings import Settings
from data.oanda_client import OandaAPIError, OandaClient
from data.store import Store
from execution.models import EntryType, Fill, FillStatus, Order
from execution.orders import OrderRejected, submit_order
from strategies.base import Direction

PRACTICE_BASE = "https://api-fxpractice.oanda.com"
ACCOUNT_ID = "001-001-1234567-001"
ORDERS_URL = f"{PRACTICE_BASE}/v3/accounts/{ACCOUNT_ID}/orders"
SUMMARY_URL = f"{PRACTICE_BASE}/v3/accounts/{ACCOUNT_ID}/summary"

NOW = datetime(2026, 5, 29, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _settings(env: str = "demo") -> Settings:
    return Settings(
        env=env,
        oanda_api_token=SecretStr("super-secret-token-DO-NOT-LEAK"),
        oanda_account_id=ACCOUNT_ID,
    )


def _store() -> Store:
    return Store(db_path=":memory:")


def _long_order(client_order_id: str = "a" * 32) -> Order:
    return Order(
        client_order_id=client_order_id,
        instrument="EUR_USD",
        direction=Direction.LONG,
        units=10_000,
        entry_type=EntryType.MARKET,
        stop_loss_price=1.08000,
        take_profit_price=1.09500,
        candidate_ref="EUR_USD:H1:macrossover",
        created_at=NOW,
    )


def _short_order(client_order_id: str = "b" * 32) -> Order:
    return Order(
        client_order_id=client_order_id,
        instrument="EUR_USD",
        direction=Direction.SHORT,
        units=-10_000,
        entry_type=EntryType.MARKET,
        stop_loss_price=1.09500,
        take_profit_price=1.08000,
        candidate_ref="EUR_USD:H1:macrossover",
        created_at=NOW,
    )


def _fill_response(
    *,
    price: str = "1.08550",
    units: str = "10000",
    trade_id: str = "T-555",
    fill_time: str = "2026-05-29T13:00:01.000000000Z",
) -> dict[str, Any]:
    """A v20 OrderCreate success response with an orderFillTransaction."""
    return {
        "orderCreateTransaction": {"id": "1001", "time": fill_time},
        "orderFillTransaction": {
            "id": "1002",
            "time": fill_time,
            "price": price,
            "units": units,
            "tradeOpened": {"tradeID": trade_id, "units": units},
        },
        "lastTransactionID": "1002",
    }


def _reject_response(reason: str = "INSUFFICIENT_MARGIN") -> dict[str, Any]:
    return {
        "orderRejectTransaction": {
            "id": "2001",
            "time": "2026-05-29T13:00:01.000000000Z",
            "type": "MARKET_ORDER_REJECT",
            "reason": reason,
        },
        "lastTransactionID": "2001",
    }


def _client() -> OandaClient:
    return OandaClient(_settings())


def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Atomic bracket (INV-04)
# ---------------------------------------------------------------------------


class TestAtomicBracket:
    @resp_lib.activate
    def test_sl_and_tp_in_one_request(self) -> None:
        captured: list[dict[str, Any]] = []

        def _capture(request: Any) -> tuple[int, dict[str, str], str]:
            captured.append(json.loads(request.body))
            return (201, {}, json.dumps(_fill_response()))

        resp_lib.add_callback(resp_lib.POST, ORDERS_URL, callback=_capture)

        store = _store()
        order = _long_order()
        submit_order(
            order,
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )

        assert len(captured) == 1, "exactly one v20 request (atomic, INV-04)"
        body = captured[0]["order"]
        assert "stopLossOnFill" in body
        assert "takeProfitOnFill" in body
        assert body["stopLossOnFill"]["price"] == "1.08000"
        assert body["takeProfitOnFill"]["price"] == "1.09500"
        # The idempotency key is attached as the v20 client extension (INV-15).
        assert body["clientExtensions"]["id"] == order.client_order_id

    @resp_lib.activate
    def test_units_signed_for_short(self) -> None:
        captured: list[dict[str, Any]] = []

        def _capture(request: Any) -> tuple[int, dict[str, str], str]:
            captured.append(json.loads(request.body))
            return (201, {}, json.dumps(_fill_response(units="-10000")))

        resp_lib.add_callback(resp_lib.POST, ORDERS_URL, callback=_capture)
        store = _store()
        submit_order(
            _short_order(),
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        assert captured[0]["order"]["units"] == "-10000"


# ---------------------------------------------------------------------------
# Idempotency (INV-15)
# ---------------------------------------------------------------------------


class TestIdempotency:
    @resp_lib.activate
    def test_duplicate_submit_no_second_order(self) -> None:
        """A second submit of the same client_order_id makes no broker call."""
        # Only ONE response is registered. If submit_order hit the broker
        # twice, responses would raise ConnectionError on the second call.
        resp_lib.add(
            resp_lib.POST, ORDERS_URL, json=_fill_response(), status=201
        )
        store = _store()
        order = _long_order()
        kwargs = dict(
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        first = submit_order(order, **kwargs)  # type: ignore[arg-type]
        second = submit_order(order, **kwargs)  # type: ignore[arg-type]

        assert isinstance(first, Fill) and isinstance(second, Fill)
        assert first.broker_trade_id == second.broker_trade_id
        # Exactly one HTTP call was made.
        assert len(resp_lib.calls) == 1
        # Exactly one position row exists.
        cur = store._conn.execute("SELECT COUNT(*) FROM positions")
        assert cur.fetchone()[0] == 1

    @resp_lib.activate
    def test_transient_then_success_exactly_one_fill(self) -> None:
        """A 503 then a 201 yields exactly one filled position (retry reuses id)."""
        resp_lib.add(resp_lib.POST, ORDERS_URL, status=503, body="service down")
        resp_lib.add(
            resp_lib.POST, ORDERS_URL, json=_fill_response(), status=201
        )
        store = _store()
        order = _long_order()
        fill = submit_order(
            order,
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        assert fill.status is FillStatus.FILLED
        assert len(resp_lib.calls) == 2  # one failed, one succeeded
        cur = store._conn.execute("SELECT COUNT(*) FROM positions")
        assert cur.fetchone()[0] == 1
        cur = store._conn.execute(
            "SELECT COUNT(*) FROM fills WHERE status='filled'"
        )
        assert cur.fetchone()[0] == 1

    @resp_lib.activate
    def test_terminal_4xx_not_retried(self) -> None:
        """A 400 is terminal: surfaced immediately, not retried into 5xx loop."""
        resp_lib.add(resp_lib.POST, ORDERS_URL, status=400, body="bad order")
        store = _store()
        with pytest.raises(OandaAPIError) as exc:
            submit_order(
                _long_order(),
                client=_client(),
                store=store,
                entry_ref=1.08500,
                precision=5,
                now=NOW,
                sleep=_no_sleep,
            )
        assert exc.value.status_code == 400
        assert len(resp_lib.calls) == 1  # no retry on a 4xx


# ---------------------------------------------------------------------------
# Slippage capture
# ---------------------------------------------------------------------------


class TestSlippage:
    @resp_lib.activate
    def test_long_adverse_slippage_positive(self) -> None:
        # Long, filled above entry_ref => adverse => positive slippage.
        resp_lib.add(
            resp_lib.POST,
            ORDERS_URL,
            json=_fill_response(price="1.08600"),
            status=201,
        )
        store = _store()
        fill = submit_order(
            _long_order(),
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        assert fill.slippage == pytest.approx(1.08600 - 1.08500)
        assert fill.slippage > 0

    @resp_lib.activate
    def test_short_adverse_slippage_positive(self) -> None:
        # Short, filled below entry_ref => adverse => positive slippage.
        resp_lib.add(
            resp_lib.POST,
            ORDERS_URL,
            json=_fill_response(price="1.08400", units="-10000"),
            status=201,
        )
        store = _store()
        fill = submit_order(
            _short_order(),
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        assert fill.slippage == pytest.approx(1.08500 - 1.08400)
        assert fill.slippage > 0


# ---------------------------------------------------------------------------
# Rejection / partial
# ---------------------------------------------------------------------------


class TestRejectionAndPartial:
    @resp_lib.activate
    def test_rejection_records_status_no_position(self) -> None:
        resp_lib.add(
            resp_lib.POST, ORDERS_URL, json=_reject_response(), status=201
        )
        store = _store()
        order = _long_order()
        with pytest.raises(OrderRejected):
            submit_order(
                order,
                client=_client(),
                store=store,
                entry_ref=1.08500,
                precision=5,
                now=NOW,
                sleep=_no_sleep,
            )
        cur = store._conn.execute("SELECT COUNT(*) FROM positions")
        assert cur.fetchone()[0] == 0
        cur = store._conn.execute(
            "SELECT status FROM fills WHERE client_order_id=?",
            (order.client_order_id,),
        )
        assert cur.fetchone()[0] == "rejected"
        # A rejected row is not resurrected as a Fill by the idempotency read.
        assert store.get_fill_by_client_order_id(order.client_order_id) is None

    @resp_lib.activate
    def test_partial_fill_recorded(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            ORDERS_URL,
            json=_fill_response(units="4000"),
            status=201,
        )
        store = _store()
        fill = submit_order(
            _long_order(),  # ordered 10_000
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        assert fill.status is FillStatus.PARTIAL
        assert abs(fill.units_filled) < 10_000
        assert fill.units_filled == 4000


# ---------------------------------------------------------------------------
# INV-03 (UTC) and INV-08 (no secret leaked)
# ---------------------------------------------------------------------------


class TestTimestampsAndSecrets:
    @resp_lib.activate
    def test_persisted_timestamps_are_utc_rfc3339(self) -> None:
        resp_lib.add(
            resp_lib.POST, ORDERS_URL, json=_fill_response(), status=201
        )
        store = _store()
        order = _long_order()
        submit_order(
            order,
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        cur = store._conn.execute(
            "SELECT filled_at FROM fills WHERE client_order_id=?",
            (order.client_order_id,),
        )
        filled_at = cur.fetchone()[0]
        assert filled_at.endswith("Z")
        # Parses as UTC and round-trips through the Fill model.
        reread = store.get_fill_by_client_order_id(order.client_order_id)
        assert reread is not None
        assert reread.filled_at.tzinfo is not None
        assert reread.filled_at.utcoffset() == timezone.utc.utcoffset(None)

    @resp_lib.activate
    def test_no_secret_in_persisted_rows(self) -> None:
        resp_lib.add(
            resp_lib.POST, ORDERS_URL, json=_fill_response(), status=201
        )
        store = _store()
        order = _long_order()
        submit_order(
            order,
            client=_client(),
            store=store,
            entry_ref=1.08500,
            precision=5,
            now=NOW,
            sleep=_no_sleep,
        )
        secret = "super-secret-token-DO-NOT-LEAK"
        for table in ("orders", "fills", "positions"):
            cur = store._conn.execute(f"SELECT * FROM {table}")
            for row in cur.fetchall():
                for value in row:
                    assert secret not in str(value)


# ---------------------------------------------------------------------------
# Account summary endpoint (INV-09 single env reader)
# ---------------------------------------------------------------------------


class TestAccountSummary:
    @resp_lib.activate
    def test_account_summary_hits_practice_endpoint(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            SUMMARY_URL,
            json={"account": {"balance": "100000.0", "NAV": "100000.0"}},
            status=200,
        )
        summary = _client().account_summary()
        assert summary["account"]["balance"] == "100000.0"
        request_url = resp_lib.calls[0].request.url
        assert request_url is not None
        assert request_url.startswith(PRACTICE_BASE)
