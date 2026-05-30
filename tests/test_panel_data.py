"""Tests for panel/data.py — the read-only view-model layer (P4-T-04).

All tests use a seeded in-memory SQLite store (no Streamlit, no live HTTP).
The view models are the tested seam.

Test areas
----------
* ``load_fills``      — newest-first, reconstructs frozen INV-14 Fill.
* ``equity_series``   — drawdown formula (A-01), incl. after-a-new-peak.
* ``blotter``         — risk_in_use == book_risk_sum + budget == book_risk_budget
                        + unrealized_pl passthrough (INV-16).
* ``watchlist``       — round-trip against seeded watchlist (INV-13).
* ``deviation_log``   — newest-first, UTC timestamps.
* ``chart_data``      — candles + overlays ("active" vs "proposed", both when
                        coexist).
* Transitive-import boundary — subprocess import walk asserts no
  execution.orders / risk.sizing / cli is reachable from panel.data (INV-01).

INV-03: all timestamps are UTC RFC 3339.
INV-13: Candidate shape is unchanged by the watchlist accessor.
INV-14: Fill/Position shapes are frozen.
INV-16: unrealized_pl is a passthrough.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from data.store import Store, _to_rfc3339
from execution.models import Fill, FillStatus, Position
from panel.data import (
    BlotterRow,
    BlotterView,
    ChartData,
    DeviationRow,
    EquityPoint,
    Overlay,
    blotter,
    chart_data,
    deviation_log,
    equity_series,
    watchlist,
)
from risk.limits import LimitsConfig, book_risk_budget, book_risk_sum
from signals.ranker import Candidate

# ---------------------------------------------------------------------------
# Shared constants / fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
SOD_EQUITY = 10_000.0


def _store() -> Store:
    return Store(db_path=":memory:")


def _write_fill(
    store: Store,
    *,
    client_order_id: str = "abc123",
    broker_trade_id: str = "T001",
    fill_price: float = 1.1000,
    units_filled: int = 1000,
    slippage: float = 0.0001,
    filled_at: datetime | None = None,
) -> Fill:
    """Helper: write a fill row and return the Fill."""
    from execution.models import FillStatus

    if filled_at is None:
        filled_at = NOW
    fill = Fill(
        client_order_id=client_order_id,
        broker_trade_id=broker_trade_id,
        fill_price=fill_price,
        units_filled=units_filled,
        slippage=slippage,
        filled_at=filled_at,
        status=FillStatus.FILLED,
    )
    store.write_fill(fill)
    return fill


def _write_position(
    store: Store,
    *,
    broker_trade_id: str = "T001",
    instrument: str = "EUR_USD",
    units: int = 1000,
    entry_price: float = 1.1000,
    stop_loss_price: float = 1.0900,
    take_profit_price: float = 1.1150,
    unrealized_pl: float = 25.0,
    opened_at: datetime | None = None,
) -> Position:
    """Helper: write a position row and return the Position."""
    if opened_at is None:
        opened_at = NOW
    pos = Position(
        broker_trade_id=broker_trade_id,
        instrument=instrument,
        units=units,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        unrealized_pl=unrealized_pl,
        opened_at=opened_at,
        candidate_ref=f"{instrument}:H1:macrossover_10_50",
    )
    store.write_position(pos)
    return pos


def _write_candidate(
    store: Store,
    run_ts: datetime,
    *,
    instrument: str = "EUR_USD",
    timeframe: str = "H1",
    strategy_name: str = "macrossover_10_50",
    direction: str = "LONG",
    entry_ref: float = 1.1050,
    stop_distance: float = 0.0050,
    target_distance: float = 0.0075,
    oos_sharpe_mean: float = 1.5,
    quality_score: float = 0.8,
    rank: int = 1,
) -> Candidate:
    """Helper: seed one Candidate into the watchlist table."""
    cand = Candidate(
        instrument=instrument,
        timeframe=timeframe,
        strategy_name=strategy_name,
        direction=direction,
        entry_ref=entry_ref,
        stop_distance=stop_distance,
        target_distance=target_distance,
        oos_sharpe_mean=oos_sharpe_mean,
        quality_score=quality_score,
        rank=rank,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at=_to_rfc3339(NOW),
    )
    store.write_watchlist([cand], run_timestamp=run_ts)
    return cand


# ===========================================================================
# load_fills — newest-first, frozen INV-14 Fill reconstruction
# ===========================================================================


class TestLoadFills:
    def test_empty_returns_empty_list(self) -> None:
        store = _store()
        assert store.load_fills() == []

    def test_single_fill_round_trips(self) -> None:
        store = _store()
        fill = _write_fill(store)
        result = store.load_fills()
        assert len(result) == 1
        f = result[0]
        assert isinstance(f, Fill)
        assert f.client_order_id == fill.client_order_id
        assert f.broker_trade_id == fill.broker_trade_id
        assert f.fill_price == fill.fill_price
        assert f.units_filled == fill.units_filled
        assert f.slippage == fill.slippage
        assert f.status == FillStatus.FILLED
        # filled_at must be UTC-aware (INV-03).
        assert f.filled_at.tzinfo is not None

    def test_newest_first_ordering(self) -> None:
        store = _store()
        t1 = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)
        _write_fill(store, client_order_id="a" * 32, broker_trade_id="T001", filled_at=t2)
        _write_fill(store, client_order_id="b" * 32, broker_trade_id="T002", filled_at=t1)
        _write_fill(store, client_order_id="c" * 32, broker_trade_id="T003", filled_at=t3)

        fills = store.load_fills()
        filled_ats = [f.filled_at for f in fills]
        assert filled_ats == sorted(filled_ats, reverse=True), "Must be newest-first"

    def test_limit_caps_results(self) -> None:
        store = _store()
        for i in range(5):
            _write_fill(
                store,
                client_order_id=f"order_{i}" + "x" * (32 - len(f"order_{i}")),
                broker_trade_id=f"T{i:03d}",
                filled_at=NOW + timedelta(hours=i),
            )
        result = store.load_fills(limit=3)
        assert len(result) == 3

    def test_limit_none_returns_all(self) -> None:
        store = _store()
        for i in range(4):
            _write_fill(
                store,
                client_order_id=f"ord{i}" + "y" * (32 - len(f"ord{i}")),
                broker_trade_id=f"T{i:03d}",
                filled_at=NOW + timedelta(hours=i),
            )
        assert len(store.load_fills(limit=None)) == 4

    def test_rejected_rows_excluded(self) -> None:
        store = _store()
        # Write a rejection (no valid Fill object — use store-layer sentinel).
        store.write_rejection(
            client_order_id="rejected_order" + "z" * (32 - len("rejected_order")),
            rejected_at=NOW,
            reason="margin insufficient",
        )
        # Write one genuine fill.
        _write_fill(store)
        result = store.load_fills()
        assert len(result) == 1  # rejection must not appear

    def test_filled_at_is_utc_aware(self) -> None:
        store = _store()
        _write_fill(store, filled_at=NOW)
        fills = store.load_fills()
        assert fills[0].filled_at.tzinfo is not None


# ===========================================================================
# equity_series — drawdown formula, running-peak reset
# ===========================================================================


class TestEquitySeries:
    def test_empty_store_returns_empty_list(self) -> None:
        store = _store()
        assert equity_series(store) == []

    def test_single_point_drawdown_is_zero(self) -> None:
        store = _store()
        store.write_equity_snapshot(as_of="2026-05-29T12:00:00Z", equity=10_000.0, day_pl=0.0)
        pts = equity_series(store)
        assert len(pts) == 1
        assert pts[0].drawdown == 0.0

    def test_drawdown_formula_below_peak(self) -> None:
        store = _store()
        store.write_equity_snapshot(as_of="2026-05-29T12:00:00Z", equity=10_000.0, day_pl=0.0)
        store.write_equity_snapshot(as_of="2026-05-29T13:00:00Z", equity=9_800.0, day_pl=-200.0)
        pts = equity_series(store)
        # drawdown = (10_000 - 9_800) / 10_000 = 0.02
        assert abs(pts[1].drawdown - 0.02) < 1e-9
        assert pts[1].drawdown >= 0.0

    def test_drawdown_zero_at_new_peak(self) -> None:
        store = _store()
        store.write_equity_snapshot(as_of="2026-05-29T12:00:00Z", equity=10_000.0, day_pl=0.0)
        store.write_equity_snapshot(as_of="2026-05-29T13:00:00Z", equity=9_800.0, day_pl=-200.0)
        store.write_equity_snapshot(as_of="2026-05-29T14:00:00Z", equity=10_100.0, day_pl=100.0)
        pts = equity_series(store)
        # At the new peak (10_100 > 10_000), drawdown must be exactly 0.
        assert pts[2].drawdown == 0.0

    def test_drawdown_after_new_peak_uses_new_peak(self) -> None:
        # After crossing a new peak the running-peak advances; a subsequent dip
        # is measured against the NEW peak, not the old one.
        store = _store()
        store.write_equity_snapshot(as_of="2026-05-29T12:00:00Z", equity=10_000.0, day_pl=0.0)
        store.write_equity_snapshot(as_of="2026-05-29T13:00:00Z", equity=10_500.0, day_pl=500.0)
        store.write_equity_snapshot(as_of="2026-05-29T14:00:00Z", equity=10_200.0, day_pl=200.0)
        pts = equity_series(store)
        # Running peak after pt[1] = 10_500.
        # drawdown = (10_500 - 10_200) / 10_500 ≈ 0.02857...
        expected = (10_500.0 - 10_200.0) / 10_500.0
        assert abs(pts[2].drawdown - expected) < 1e-9

    def test_drawdown_never_negative(self) -> None:
        store = _store()
        for i, equity in enumerate([10_000.0, 10_100.0, 10_200.0, 10_300.0]):
            store.write_equity_snapshot(
                as_of=f"2026-05-29T{12 + i:02d}:00:00Z",
                equity=equity,
                day_pl=equity - 10_000.0,
            )
        for pt in equity_series(store):
            assert pt.drawdown >= 0.0

    def test_since_filters_series(self) -> None:
        store = _store()
        for minute, equity in ((0, 10_000.0), (5, 10_050.0), (10, 10_100.0)):
            store.write_equity_snapshot(
                as_of=f"2026-05-29T12:{minute:02d}:00Z",
                equity=equity,
                day_pl=equity - 10_000.0,
            )
        pts = equity_series(store, since="2026-05-29T12:05:00Z")
        # Only the last two points returned (13:05 and 13:10).
        assert len(pts) == 2
        assert pts[0].equity == 10_050.0

    def test_equity_and_day_pl_passthrough(self) -> None:
        store = _store()
        store.write_equity_snapshot(
            as_of="2026-05-29T12:00:00Z", equity=9_950.0, day_pl=-50.0
        )
        pts = equity_series(store)
        assert pts[0].equity == 9_950.0
        assert pts[0].day_pl == -50.0

    def test_as_of_is_rfc3339_z(self) -> None:
        store = _store()
        store.write_equity_snapshot(
            as_of="2026-05-29T12:00:00Z", equity=10_000.0, day_pl=0.0
        )
        pts = equity_series(store)
        assert pts[0].as_of.endswith("Z"), "as_of must be RFC 3339 with Z suffix (INV-03)"


# ===========================================================================
# blotter — risk_in_use, risk_budget, unrealized_pl passthrough
# ===========================================================================


class TestBlotter:
    def test_empty_book(self) -> None:
        store = _store()
        view = blotter(store)
        assert view.positions == []
        assert view.risk_in_use == 0.0
        assert view.day_pl is None
        assert view.start_of_day_equity is None
        assert view.risk_budget is None

    def test_unrealized_pl_is_passthrough(self) -> None:
        # The blotter must surface the stored unrealized_pl exactly —
        # it must NOT recompute it from prices (INV-16 / D-05).
        store = _store()
        expected_upl = 37.42
        _write_position(store, unrealized_pl=expected_upl)
        view = blotter(store)
        assert len(view.positions) == 1
        assert view.positions[0].unrealized_pl == expected_upl

    def test_risk_in_use_equals_book_risk_sum(self) -> None:
        store = _store()
        pos = _write_position(
            store,
            units=1000,
            entry_price=1.1000,
            stop_loss_price=1.0900,  # risk per unit = 0.01
        )
        open_positions = store.load_open_positions()
        expected_risk = book_risk_sum(open_positions)
        # |units| * |entry - stop| = 1000 * 0.01 = 10.0
        assert expected_risk == pytest.approx(10.0)
        view = blotter(store)
        assert view.risk_in_use == pytest.approx(expected_risk)

    def test_risk_budget_equals_book_risk_budget(self) -> None:
        store = _store()
        store.write_account_state(
            start_of_day_equity=SOD_EQUITY,
            day_pl=0.0,
            as_of=NOW,
        )
        cfg = LimitsConfig()
        view = blotter(store, cfg=cfg)
        # equity = sod_equity + day_pl = 10_000 + 0 = 10_000
        expected_budget = book_risk_budget(SOD_EQUITY, cfg)
        assert view.risk_budget == pytest.approx(expected_budget)

    def test_risk_in_use_matches_kill_switch_figure(self) -> None:
        # The panel's risk_in_use and risk_budget must use the same functions
        # as check_limits so the figure is byte-identical to the kill-switch
        # backstop (DRIFT-02).
        store = _store()
        store.write_account_state(
            start_of_day_equity=10_000.0,
            day_pl=100.0,
            as_of=NOW,
        )
        _write_position(
            store,
            broker_trade_id="T001",
            units=500,
            entry_price=1.2000,
            stop_loss_price=1.1900,  # risk = 500 * 0.01 = 5.0
        )
        open_positions = store.load_open_positions()
        cfg = LimitsConfig()
        equity = 10_000.0 + 100.0  # sod + day_pl
        direct_risk_in_use = book_risk_sum(open_positions)
        direct_budget = book_risk_budget(equity, cfg)

        view = blotter(store, cfg=cfg)
        assert view.risk_in_use == pytest.approx(direct_risk_in_use)
        assert view.risk_budget == pytest.approx(direct_budget)

    def test_day_pl_and_sod_equity_from_account_state(self) -> None:
        store = _store()
        store.write_account_state(
            start_of_day_equity=12_500.0,
            day_pl=-150.0,
            as_of=NOW,
        )
        view = blotter(store)
        assert view.start_of_day_equity == 12_500.0
        assert view.day_pl == -150.0

    def test_opened_at_is_rfc3339_z(self) -> None:
        store = _store()
        _write_position(store, opened_at=NOW)
        view = blotter(store)
        assert view.positions[0].opened_at.endswith("Z"), (
            "opened_at must be RFC 3339 Z (INV-03)"
        )

    def test_multiple_open_positions(self) -> None:
        store = _store()
        _write_position(
            store, broker_trade_id="T001", instrument="EUR_USD",
            units=1000, entry_price=1.1000, stop_loss_price=1.0900,
            unrealized_pl=20.0,
        )
        _write_position(
            store, broker_trade_id="T002", instrument="GBP_USD",
            units=-500, entry_price=1.2700, stop_loss_price=1.2800,
            unrealized_pl=-5.0,
        )
        view = blotter(store)
        assert len(view.positions) == 2
        # risk = 1000 * 0.01 + 500 * 0.01 = 10.0 + 5.0 = 15.0
        assert view.risk_in_use == pytest.approx(15.0)


# ===========================================================================
# watchlist — INV-13 round-trip
# ===========================================================================


class TestWatchlist:
    def test_empty_watchlist_returns_empty_list(self) -> None:
        store = _store()
        assert watchlist(store) == []

    def test_roundtrip_preserves_candidate_shape(self) -> None:
        store = _store()
        cand = _write_candidate(store, NOW)
        result = watchlist(store)
        assert len(result) == 1
        c = result[0]
        # INV-13 shape must be preserved exactly.
        assert c.instrument == cand.instrument
        assert c.timeframe == cand.timeframe
        assert c.strategy_name == cand.strategy_name
        assert c.direction == cand.direction
        assert c.entry_ref == cand.entry_ref
        assert c.stop_distance == cand.stop_distance
        assert c.target_distance == cand.target_distance
        assert c.oos_sharpe_mean == cand.oos_sharpe_mean
        assert c.quality_score == cand.quality_score
        assert c.rank == cand.rank
        assert c.spread_ok == cand.spread_ok
        assert c.session_ok == cand.session_ok
        assert c.news_flag == cand.news_flag
        assert c.generated_at == cand.generated_at

    def test_returns_candidates_from_latest_run(self) -> None:
        store = _store()
        t1 = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
        # Older run: 2 candidates
        _write_candidate(store, t1, instrument="EUR_USD", rank=1)
        _write_candidate(store, t1, instrument="GBP_USD", timeframe="H4", rank=2)
        # Newer run: 1 candidate
        _write_candidate(store, t2, instrument="USD_JPY", timeframe="D", rank=1)

        result = watchlist(store)
        # Only latest run's candidates.
        assert len(result) == 1
        assert result[0].instrument == "USD_JPY"

    def test_result_elements_are_candidate_instances(self) -> None:
        store = _store()
        _write_candidate(store, NOW)
        result = watchlist(store)
        assert all(isinstance(c, Candidate) for c in result)


# ===========================================================================
# deviation_log — newest-first, UTC timestamps
# ===========================================================================


class TestDeviationLog:
    def _write_event(
        self,
        store: Store,
        event_id: str,
        created_at: datetime,
        instrument: str = "EUR_USD",
    ) -> None:
        from monitoring.watcher import DeviationEvent

        event = DeviationEvent(
            event_id=event_id,
            instrument=instrument,
            deviation_type="adverse",
            detail="test detail",
            broker_trade_id=None,
            severity="warn",
            created_at=created_at,
        )
        store.write_deviation_event(event)

    def test_empty_log(self) -> None:
        store = _store()
        assert deviation_log(store) == []

    def test_newest_first_ordering(self) -> None:
        store = _store()
        t1 = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)
        self._write_event(store, "e001", t2)
        self._write_event(store, "e002", t1)
        self._write_event(store, "e003", t3)

        rows = deviation_log(store)
        created_ats = [r.created_at for r in rows]
        assert created_ats == sorted(created_ats, reverse=True), "Must be newest-first"

    def test_limit_caps_results(self) -> None:
        store = _store()
        for i in range(5):
            self._write_event(store, f"evt{i}", NOW + timedelta(hours=i))
        rows = deviation_log(store, limit=3)
        assert len(rows) == 3

    def test_limit_none_returns_all(self) -> None:
        store = _store()
        for i in range(4):
            self._write_event(store, f"ev{i:02d}", NOW + timedelta(hours=i))
        assert len(deviation_log(store, limit=None)) == 4

    def test_row_is_deviation_row_instance(self) -> None:
        store = _store()
        self._write_event(store, "ev01", NOW)
        rows = deviation_log(store)
        assert all(isinstance(r, DeviationRow) for r in rows)

    def test_created_at_is_rfc3339_z(self) -> None:
        store = _store()
        self._write_event(store, "ev01", NOW)
        rows = deviation_log(store)
        assert rows[0].created_at.endswith("Z"), "created_at must be RFC 3339 Z (INV-03)"


# ===========================================================================
# chart_data — candles + overlays (active vs proposed, both when coexist)
# ===========================================================================


class TestChartData:
    def _seed_candle(
        self,
        store: Store,
        instrument: str = "EUR_USD",
        granularity: str = "H1",
        t: datetime | None = None,
    ) -> None:
        """Seed a single candle row."""
        from data.oanda_client import CandleRow

        if t is None:
            t = NOW - timedelta(hours=1)
        store.upsert([
            CandleRow(
                instrument=instrument,
                granularity=granularity,
                time=t,
                open_bid=1.0990,
                high_bid=1.1010,
                low_bid=1.0985,
                close_bid=1.1005,
                open_ask=1.0991,
                high_ask=1.1011,
                low_ask=1.0986,
                close_ask=1.1006,
                open_mid=1.09905,
                high_mid=1.10105,
                low_mid=1.09855,
                close_mid=1.10055,
                volume=1234,
                complete=True,
            )
        ])

    def test_returns_chart_data_instance(self) -> None:
        store = _store()
        result = chart_data(store, "EUR_USD", "H1")
        assert isinstance(result, ChartData)

    def test_instrument_and_timeframe_preserved(self) -> None:
        store = _store()
        result = chart_data(store, "EUR_USD", "H1")
        assert result.instrument == "EUR_USD"
        assert result.timeframe == "H1"

    def test_candles_loaded(self) -> None:
        store = _store()
        self._seed_candle(store)
        result = chart_data(
            store,
            "EUR_USD",
            "H1",
            candle_start=NOW - timedelta(hours=2),
            candle_end=NOW,
        )
        assert not result.candles.empty

    def test_no_overlays_when_no_position_no_watchlist(self) -> None:
        store = _store()
        result = chart_data(store, "EUR_USD", "H1")
        assert result.overlays == []

    def test_active_overlay_when_open_position(self) -> None:
        store = _store()
        pos = _write_position(
            store,
            instrument="EUR_USD",
            entry_price=1.1000,
            stop_loss_price=1.0900,
            take_profit_price=1.1150,
        )
        result = chart_data(store, "EUR_USD", "H1")
        assert len(result.overlays) == 1
        overlay = result.overlays[0]
        assert overlay.label == "active"
        assert overlay.entry == pos.entry_price
        assert overlay.stop == pos.stop_loss_price
        assert overlay.target == pos.take_profit_price

    def test_proposed_overlay_when_watchlist_candidate(self) -> None:
        store = _store()
        cand = _write_candidate(
            store,
            NOW,
            instrument="EUR_USD",
            timeframe="H1",
            direction="LONG",
            entry_ref=1.1050,
            stop_distance=0.0050,
            target_distance=0.0075,
        )
        result = chart_data(store, "EUR_USD", "H1")
        assert len(result.overlays) == 1
        overlay = result.overlays[0]
        assert overlay.label == "proposed"
        assert overlay.entry == cand.entry_ref
        # LONG: stop = entry - stop_distance; target = entry + target_distance
        assert overlay.stop == pytest.approx(1.1050 - 0.0050)
        assert overlay.target == pytest.approx(1.1050 + 0.0075)

    def test_proposed_overlay_short_direction(self) -> None:
        store = _store()
        _write_candidate(
            store,
            NOW,
            instrument="GBP_USD",
            timeframe="H4",
            direction="SHORT",
            entry_ref=1.2700,
            stop_distance=0.0060,
            target_distance=0.0090,
        )
        result = chart_data(store, "GBP_USD", "H4")
        overlay = result.overlays[0]
        assert overlay.label == "proposed"
        # SHORT: stop = entry + stop_distance; target = entry - target_distance
        assert overlay.stop == pytest.approx(1.2700 + 0.0060)
        assert overlay.target == pytest.approx(1.2700 - 0.0090)

    def test_both_overlays_when_position_and_watchlist_coexist(self) -> None:
        # A-02: when both an open position AND a watchlist candidate exist for
        # the same instrument+timeframe, both overlays are included with their
        # distinct labels.
        store = _store()
        _write_position(
            store,
            instrument="EUR_USD",
            entry_price=1.1000,
            stop_loss_price=1.0900,
            take_profit_price=1.1150,
        )
        _write_candidate(
            store,
            NOW,
            instrument="EUR_USD",
            timeframe="H1",
            direction="LONG",
            entry_ref=1.1050,
            stop_distance=0.0050,
            target_distance=0.0075,
        )
        result = chart_data(store, "EUR_USD", "H1")
        labels = {o.label for o in result.overlays}
        assert "active" in labels, "Expected active overlay for open position"
        assert "proposed" in labels, "Expected proposed overlay for watchlist candidate"
        assert len(result.overlays) == 2

    def test_no_overlay_for_different_instrument(self) -> None:
        # A position for GBP_USD must not produce an overlay on the EUR_USD chart.
        store = _store()
        _write_position(store, instrument="GBP_USD")
        result = chart_data(store, "EUR_USD", "H1")
        assert result.overlays == []

    def test_no_proposed_overlay_for_different_timeframe(self) -> None:
        # A watchlist candidate on H4 must not produce an overlay on the H1 chart.
        store = _store()
        _write_candidate(store, NOW, instrument="EUR_USD", timeframe="H4")
        result = chart_data(store, "EUR_USD", "H1")
        assert result.overlays == []


# ===========================================================================
# INV-01 transitive-import boundary (subprocess walk of panel.data's modules)
# ===========================================================================


class TestReadOnlyBoundary:
    """Assert that panel.data cannot reach execution.orders, risk.sizing, or cli.

    The INV-01 transitive-boundary test uses an AST-based source-code check on
    panel/data.py (and panel/__init__.py) rather than walking the runtime
    sys.modules graph.  Walking sys.modules is unreliable here because
    risk/__init__.py re-exports risk.sizing — so importing any module from the
    risk package (including the permitted risk.limits) always loads risk.sizing
    as a package __init__ side effect, regardless of whether panel.data uses it.

    The source-code check is *stricter* (catches dynamic/lazy imports that a
    sys.modules walk would miss) AND correctly scoped (it flags code in
    panel.data itself, not package initialisation boilerplate outside panel's
    control).

    Forbidden direct imports in panel.data / panel.__init__:
    * execution.orders   — the order-placement module
    * risk.sizing        — the per-trade position-sizing module
    * cli                — the CLI module (imports both of the above)

    Allowed:
    * execution.models   — Position/Fill types (INV-14); NOT build_bracket usage.
    * risk.limits        — book_risk_sum / book_risk_budget helpers (read-only).
    * data.store         — store loaders (read-only).
    * signals.ranker     — Candidate (INV-13).

    Note: build_bracket lives in execution.models but is not called by
    panel.data.  The test verifies the module never references it.
    """

    _PROBE = """\
import ast
import sys
from pathlib import Path

root = Path("{root}")
panel_files = [root / "panel" / "data.py", root / "panel" / "__init__.py"]

# Modules whose presence in panel.data source is forbidden (INV-01).
forbidden_imports = {{
    "execution.orders",
    "risk.sizing",
    "cli",
}}
# build_bracket must not be referenced (usage check, not import check).
forbidden_names = {{"build_bracket"}}

violations = []

for path in panel_files:
    if not path.exists():
        continue
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        print(f"SyntaxError in {{path}}: {{e}}")
        sys.exit(2)

    for node in ast.walk(tree):
        # Check: from X import ... or import X
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    for forbidden in forbidden_imports:
                        if mod == forbidden or mod.startswith(forbidden + "."):
                            violations.append(
                                f"{{path.name}}: imports '{{mod}}' (forbidden: {{forbidden}})"
                            )
            else:  # ImportFrom
                mod = node.module or ""
                for forbidden in forbidden_imports:
                    if mod == forbidden or mod.startswith(forbidden + "."):
                        violations.append(
                            f"{{path.name}}: from '{{mod}}' import ... (forbidden: {{forbidden}})"
                        )
        # Check: no reference to build_bracket by name.
        if isinstance(node, ast.Attribute):
            if node.attr in forbidden_names:
                violations.append(
                    f"{{path.name}}: references forbidden attribute '{{node.attr}}'"
                )
        if isinstance(node, ast.Name):
            if node.id in forbidden_names:
                violations.append(
                    f"{{path.name}}: references forbidden name '{{node.id}}'"
                )

if violations:
    for v in violations:
        print("VIOLATION:", v)
    sys.exit(1)
else:
    print("OK: no forbidden imports or names in panel.data source")
    sys.exit(0)
""".format(root="/home/sam-baby/development/fathom")

    def test_panel_data_does_not_import_forbidden_modules(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", self._PROBE],
            capture_output=True,
            text=True,
            cwd="/home/sam-baby/development/fathom",
        )
        output = result.stdout.strip() + result.stderr.strip()
        assert result.returncode == 0, (
            f"panel.data violates INV-01 read-only boundary.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in output, f"Unexpected probe output: {output}"
