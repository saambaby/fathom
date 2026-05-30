"""Tests for the equity-snapshots feature (P4-T-03).

Two layers:

* **Store** (``data.store``) — the ``equity_snapshots`` table + accessors:
  append-only (no overwrite), ``load_equity_snapshots`` ordered by ``as_of``
  ascending, ``since`` lower-bound filter, empty-store behaviour.
* **Reconcile append** (``execution.reconcile``) — v20 mocked with the
  ``responses`` library (no live HTTP): each reconcile pass appends exactly one
  snapshot with ``equity == broker.nav`` and ``day_pl == nav − sod_equity`` (the
  same figures it writes to ``account_state``); the append is *after*
  ``write_account_state``; the append is non-fatal (a snapshot-write failure
  logs WARNING but never aborts the reconcile — broker-truth wins).

INV-03 (UTC RFC 3339 ``as_of``), INV-16 (broker NAV is the equity) are checked
here.  The reconcile-path tests reuse the helpers from ``test_reconciliation``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest
import responses as resp_lib

from data.store import Store, _to_rfc3339
from execution.reconcile import reconcile

from tests.test_reconciliation import (
    NOW,
    OPEN_TRADES_URL,
    SUMMARY_URL,
    _client,
    _open_trades_response,
    _register,
    _summary_response,
)


def _store() -> Store:
    return Store(db_path=":memory:")


# ===========================================================================
# Store layer — table + accessors
# ===========================================================================


class TestEquitySnapshotStore:
    def test_empty_store_returns_no_snapshots(self) -> None:
        store = _store()
        assert store.load_equity_snapshots() == []

    def test_write_then_load_round_trips_floats_and_timestamp(self) -> None:
        store = _store()
        store.write_equity_snapshot(
            as_of="2026-05-29T13:00:00Z", equity=10_050.0, day_pl=50.0
        )
        rows = store.load_equity_snapshots()
        assert rows == [
            {"as_of": "2026-05-29T13:00:00Z", "equity": 10_050.0, "day_pl": 50.0}
        ]
        # broker NAV stored as float (INV-16); as_of is RFC 3339 Z (INV-03).
        assert isinstance(rows[0]["equity"], float)
        assert isinstance(rows[0]["day_pl"], float)
        assert str(rows[0]["as_of"]).endswith("Z")

    def test_append_only_same_as_of_yields_two_distinct_rows(self) -> None:
        # Append-only: no PK to clobber — two writes at the SAME as_of must NOT
        # overwrite each other (unlike account_state's singleton row).
        store = _store()
        store.write_equity_snapshot(
            as_of="2026-05-29T13:00:00Z", equity=10_000.0, day_pl=0.0
        )
        store.write_equity_snapshot(
            as_of="2026-05-29T13:00:00Z", equity=10_010.0, day_pl=10.0
        )
        rows = store.load_equity_snapshots()
        assert len(rows) == 2
        assert {r["equity"] for r in rows} == {10_000.0, 10_010.0}

    def test_load_is_ordered_by_as_of_ascending(self) -> None:
        store = _store()
        # Insert out of chronological order; load must return oldest-first.
        store.write_equity_snapshot(
            as_of="2026-05-29T13:10:00Z", equity=10_020.0, day_pl=20.0
        )
        store.write_equity_snapshot(
            as_of="2026-05-29T13:00:00Z", equity=10_000.0, day_pl=0.0
        )
        store.write_equity_snapshot(
            as_of="2026-05-29T13:05:00Z", equity=10_010.0, day_pl=10.0
        )
        rows = store.load_equity_snapshots()
        as_ofs = [r["as_of"] for r in rows]
        assert as_ofs == [
            "2026-05-29T13:00:00Z",
            "2026-05-29T13:05:00Z",
            "2026-05-29T13:10:00Z",
        ]

    def test_since_filters_to_at_or_after_bound_inclusive(self) -> None:
        store = _store()
        for minute, equity in ((0, 10_000.0), (5, 10_010.0), (10, 10_020.0)):
            store.write_equity_snapshot(
                as_of=f"2026-05-29T13:{minute:02d}:00Z",
                equity=equity,
                day_pl=equity - 10_000.0,
            )
        rows = store.load_equity_snapshots(since="2026-05-29T13:05:00Z")
        as_ofs = [r["as_of"] for r in rows]
        # Inclusive lower bound: 13:05 is kept, 13:00 is dropped.
        assert as_ofs == ["2026-05-29T13:05:00Z", "2026-05-29T13:10:00Z"]

    def test_since_none_returns_all(self) -> None:
        store = _store()
        store.write_equity_snapshot(
            as_of="2026-05-29T13:00:00Z", equity=10_000.0, day_pl=0.0
        )
        assert len(store.load_equity_snapshots(since=None)) == 1


# ===========================================================================
# Reconcile append — v20 mocked (no live HTTP)
# ===========================================================================


class TestReconcileAppendsSnapshot:
    @resp_lib.activate
    def test_one_snapshot_per_pass_equity_is_broker_nav(self) -> None:
        _register(
            _open_trades_response(),
            _summary_response(nav="10050.00", pl="50.00"),
        )
        store = _store()
        report = reconcile(client=_client(), store=store, now=NOW)

        rows = store.load_equity_snapshots()
        assert len(rows) == 1
        snap = rows[0]
        # equity == broker.nav (INV-16).
        assert snap["equity"] == 10_050.0
        # day_pl == nav − start_of_day_equity — identical to account_state and
        # to the ReconcileReport (this is the day-open snapshot pass, so ≈ 0).
        assert snap["day_pl"] == report.day_pl == 0.0
        # as_of is the reconcile `now` as RFC 3339 Z (INV-03) and matches the
        # account_state row written from the same `now`.
        assert snap["as_of"] == _to_rfc3339(NOW)
        state = store.load_account_state()
        assert state is not None and snap["as_of"] == state["as_of"]

    @resp_lib.activate
    def test_day_pl_matches_nav_minus_start_of_day_equity(self) -> None:
        # Second-pass drawdown: start_of_day snapshot at 10_000 from an earlier
        # reconcile, then NAV falls to 9_950 → day_pl == -50.0 in the snapshot.
        store = _store()
        store.write_account_state(
            start_of_day_equity=10_000.0,
            day_pl=0.0,
            as_of=NOW,
        )
        _register(
            _open_trades_response(),
            _summary_response(nav="9950.00", pl="-100.00"),
        )
        later = datetime(2026, 5, 29, 13, 5, 0, tzinfo=timezone.utc)
        report = reconcile(client=_client(), store=store, now=later)

        rows = store.load_equity_snapshots()
        assert len(rows) == 1
        snap = rows[0]
        assert snap["equity"] == 9_950.0  # broker NAV (INV-16)
        # nav − start_of_day_equity = 9950 − 10000 = -50 (NOT the lifetime pl).
        assert snap["day_pl"] == -50.0 == report.day_pl
        assert snap["as_of"] == _to_rfc3339(later)

    def test_two_reconciles_yield_two_ordered_points(self) -> None:
        store = _store()
        t1 = datetime(2026, 5, 29, 13, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 29, 13, 5, 0, tzinfo=timezone.utc)

        def _do_reconcile(now: datetime, *, nav: str, pl: str) -> None:
            with resp_lib.RequestsMock() as rsps:
                rsps.add(
                    resp_lib.GET,
                    OPEN_TRADES_URL,
                    json=_open_trades_response(),
                    status=200,
                )
                rsps.add(
                    resp_lib.GET,
                    SUMMARY_URL,
                    json=_summary_response(nav=nav, pl=pl),
                    status=200,
                )
                reconcile(client=_client(), store=store, now=now)

        _do_reconcile(t1, nav="10000.00", pl="0.00")
        _do_reconcile(t2, nav="10030.00", pl="30.00")

        rows = store.load_equity_snapshots()
        assert len(rows) == 2
        assert [r["as_of"] for r in rows] == [_to_rfc3339(t1), _to_rfc3339(t2)]
        assert [r["equity"] for r in rows] == [10_000.0, 10_030.0]


class TestAppendIsAdditiveAndNonFatal:
    @resp_lib.activate
    def test_append_happens_strictly_after_write_account_state(self) -> None:
        # Record call order: write_account_state MUST be committed before the
        # snapshot append, so a snapshot failure can never interpose before the
        # kill-switch's broker-truth row.
        store = _store()
        order: list[str] = []

        real_write_account_state = store.write_account_state
        real_write_snapshot = store.write_equity_snapshot

        def _spy_account_state(**kwargs: Any) -> None:
            order.append("account_state")
            real_write_account_state(**kwargs)

        def _spy_snapshot(**kwargs: Any) -> None:
            order.append("snapshot")
            real_write_snapshot(**kwargs)

        store.write_account_state = _spy_account_state  # type: ignore[method-assign]
        store.write_equity_snapshot = _spy_snapshot  # type: ignore[method-assign]

        _register(_open_trades_response(), _summary_response())
        reconcile(client=_client(), store=store, now=NOW)

        assert order == ["account_state", "snapshot"]

    @resp_lib.activate
    def test_snapshot_write_failure_does_not_abort_reconcile(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A snapshot-write failure is non-fatal: reconcile still returns a normal
        # report and the account_state (broker-truth) row is still written.
        store = _store()

        def _boom(**kwargs: Any) -> None:
            raise RuntimeError("disk full")

        store.write_equity_snapshot = _boom  # type: ignore[method-assign]

        _register(
            _open_trades_response(),
            _summary_response(nav="10050.00", pl="50.00"),
        )
        with caplog.at_level(logging.WARNING, logger="fathom.execution.reconcile"):
            report = reconcile(client=_client(), store=store, now=NOW)

        # Reconcile completed: report is intact, account_state was written.
        assert report.day_pl == 0.0
        state = store.load_account_state()
        assert state is not None and state["start_of_day_equity"] == 10_050.0
        # The failure was logged at WARNING, never raised.
        assert any(
            "equity snapshot" in rec.message.lower() and rec.levelno == logging.WARNING
            for rec in caplog.records
        )

    @resp_lib.activate
    def test_snapshot_failure_leaves_no_partial_row(self) -> None:
        store = _store()

        def _boom(**kwargs: Any) -> None:
            raise RuntimeError("disk full")

        store.write_equity_snapshot = _boom  # type: ignore[method-assign]
        _register(_open_trades_response(), _summary_response())
        reconcile(client=_client(), store=store, now=NOW)

        # Restore the real method only to read back — no snapshot was persisted.
        assert Store.load_equity_snapshots(store) == []
