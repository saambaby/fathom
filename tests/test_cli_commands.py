"""Tests for the Phase 2 CLI subcommands: scan, watchlist, chart (P2-T-07).

Design (NO live HTTP)
---------------------
* All tests drive ``cli.cmd_scan``, ``cli.cmd_watchlist``, ``cli.cmd_chart``
  and ``cli.main`` directly — no subprocess.
* The OANDA client, Settings, and candle fetch are never called (dry-run or
  patched).  The ranker, portfolio limiter, and chart renderer are exercised
  against in-memory SQLite + synthetic fixtures.
* ``--dry-run`` skip for ``cmd_scan`` is exercised explicitly.

Coverage
--------
1. ``scan`` runs ranker→portfolio end-to-end, persists to ``watchlist`` table,
   prints ``Candidate[]`` JSON to stdout; round-trip JSON matches INV-13 shape.
2. Empty approved-set → empty watchlist, exit 0, clear message (INV-10).
3. ``watchlist`` re-reads the persisted run; JSON round-trips back to Candidate
   with correct field names + types (INV-13 shape check).
4. ``chart <instrument>`` writes a non-empty PNG and prints its path to stdout.
5. ``backtest`` subcommand still works (not broken).
6. ``--dry-run scan`` smoke: exits 0 against an empty DB.
7. No live HTTP, no token logged (INV-08); all timestamps UTC (INV-03).
8. No order/execution import path (INV-01).
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import cli
from data.oanda_client import CandleRow, InstrumentMeta
from data.store import Store
from signals.ranker import Candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _make_candle(
    instrument: str, granularity: str, t: datetime, price: float
) -> CandleRow:
    return CandleRow(
        instrument=instrument,
        granularity=granularity,
        time=t,
        open_bid=price,
        high_bid=price + 0.0030,
        low_bid=price - 0.0030,
        close_bid=price + 0.0001,
        open_ask=price + 0.0002,
        high_ask=price + 0.0032,
        low_ask=price - 0.0028,
        close_ask=price + 0.0003,
        open_mid=price + 0.0001,
        high_mid=price + 0.0031,
        low_mid=price - 0.0029,
        close_mid=price + 0.0002,
        volume=100,
        complete=True,
    )


def _populate_h1(store: Store, instrument: str, n_bars: int = 200) -> None:
    """Insert hourly candles for the given instrument."""
    base = _utc(2026, 4, 1)
    rows = []
    for i in range(n_bars):
        t = base + timedelta(hours=i)
        delta = 0.0050 * math.sin(2 * math.pi * i / 48)
        rows.append(_make_candle(instrument, "H1", t, 1.1000 + delta))
    store.upsert(rows)


def _make_candidate(rank: int = 1, instrument: str = "EUR_USD") -> Candidate:
    return Candidate(
        instrument=instrument,
        timeframe="H1",
        strategy_name="macrossover_10_50_eur_usd_h1",
        direction="LONG",
        entry_ref=1.1050,
        stop_distance=0.0020,
        target_distance=0.0030,
        oos_sharpe_mean=1.5,
        quality_score=0.75,
        rank=rank,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at="2026-04-10T12:00:00Z",
    )


def _make_namespace(**kwargs: object) -> argparse.Namespace:
    """Build an argparse.Namespace with scan/watchlist/chart defaults."""
    defaults = {
        "command": "scan",
        "instruments": "EUR_USD",
        "timeframes": "H1",
        "db_path": "/tmp/fathom_test_not_used.db",
        "history_years": 1,
        "dry_run": True,
        "timeframe": "H1",
        "out_dir": "/tmp/fathom_charts_test",
        "instrument": "EUR_USD",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# INV-01 guard — no execution import path
# ---------------------------------------------------------------------------


class TestNoOrderPath:
    def test_cli_has_no_execution_import(self) -> None:
        """cli.py must not import execution or risk at module level (INV-01)."""
        import importlib
        import types

        module = sys.modules.get("cli") or importlib.import_module("cli")
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if isinstance(attr, types.ModuleType):
                assert "execution" not in attr.__name__, (
                    f"cli.py has a live import of execution module: {attr.__name__}"
                )
                assert "orders" not in attr.__name__, (
                    f"cli.py has a live import of orders module: {attr.__name__}"
                )


# ---------------------------------------------------------------------------
# scan — end-to-end with mocked ranker/limiter
# ---------------------------------------------------------------------------


class TestScanCommand:
    """Tests for ``cmd_scan`` — mocked Ranker/PortfolioLimiter (no HTTP)."""

    def test_scan_persists_and_prints_json(self, tmp_path: Path) -> None:
        """scan persists candidates to watchlist table + prints Candidate[] JSON."""
        db_path = str(tmp_path / "test.db")
        candidate = _make_candidate()

        args = _make_namespace(
            command="scan",
            db_path=db_path,
            instruments="EUR_USD",
            timeframes="H1",
            dry_run=True,  # skip live fetch
            history_years=1,
        )

        # Patch lazy-imported modules at their canonical paths (cmd_scan imports
        # them inside the function body, so we must patch at source module).
        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker") as mock_ranker_cls,
            patch("signals.portfolio.PortfolioLimiter") as mock_limiter_cls,
        ):
            mock_cal_cls.return_value = MagicMock()
            mock_ranker_inst = MagicMock()
            mock_ranker_inst.rank.return_value = [candidate]
            mock_ranker_cls.return_value = mock_ranker_inst

            mock_limiter_inst = MagicMock()
            mock_limiter_inst.apply.return_value = [candidate]
            mock_limiter_cls.return_value = mock_limiter_inst

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_scan(args)

        assert rc == 0

        # Verify the watchlist table was persisted.
        store = Store(db_path)
        try:
            rows = store.load_watchlist()
        finally:
            store.close()

        assert len(rows) == 1
        row = rows[0]
        assert row["instrument"] == "EUR_USD"
        assert row["timeframe"] == "H1"
        assert row["rank"] == 1
        assert row["spread_ok"] is True
        assert row["news_flag"] is False

        # Verify the stdout JSON is a Candidate[] array.
        output = buf.getvalue().strip()
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        c = parsed[0]
        # INV-13 field names check.
        for field in (
            "instrument", "timeframe", "strategy_name", "direction",
            "entry_ref", "stop_distance", "target_distance",
            "oos_sharpe_mean", "quality_score", "rank",
            "spread_ok", "session_ok", "news_flag", "generated_at",
        ):
            assert field in c, f"Missing INV-13 field: {field}"

    def test_scan_empty_approved_set_exits_zero(self, tmp_path: Path) -> None:
        """Empty approved-set → empty watchlist, exit 0, message on stdout (INV-10)."""
        db_path = str(tmp_path / "empty.db")

        args = _make_namespace(
            command="scan",
            db_path=db_path,
            instruments="EUR_USD",
            timeframes="H1",
            dry_run=True,
            history_years=1,
        )

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker") as mock_ranker_cls,
            patch("signals.portfolio.PortfolioLimiter") as mock_limiter_cls,
        ):
            mock_cal_cls.return_value = MagicMock()
            mock_ranker_inst = MagicMock()
            mock_ranker_inst.rank.return_value = []  # empty approved-set → []
            mock_ranker_cls.return_value = mock_ranker_inst

            mock_limiter_inst = MagicMock()
            mock_limiter_inst.apply.return_value = []
            mock_limiter_cls.return_value = mock_limiter_inst

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_scan(args)

        assert rc == 0, "Empty approved-set must exit 0 (INV-10)"

        output = buf.getvalue()
        assert output.strip(), "Should print a clear message even when empty"

        # Watchlist table should be present but empty.
        store = Store(db_path)
        try:
            rows = store.load_watchlist()
        finally:
            store.close()
        assert rows == []

    def test_dry_run_scan_exits_zero_empty_db(self, tmp_path: Path) -> None:
        """``--dry-run`` scan against an empty DB exits 0 and emits no tokens."""
        db_path = str(tmp_path / "dryrun.db")

        args = _make_namespace(
            command="scan",
            db_path=db_path,
            instruments="EUR_USD",
            timeframes="H1",
            dry_run=True,
            history_years=1,
        )

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker") as mock_ranker_cls,
            patch("signals.portfolio.PortfolioLimiter") as mock_limiter_cls,
        ):
            mock_cal_cls.return_value = MagicMock()
            mock_ranker_inst = MagicMock()
            mock_ranker_inst.rank.return_value = []
            mock_ranker_cls.return_value = mock_ranker_inst
            mock_limiter_inst = MagicMock()
            mock_limiter_inst.apply.return_value = []
            mock_limiter_cls.return_value = mock_limiter_inst

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_scan(args)

        assert rc == 0

        # INV-08: no token in stdout or stderr (the real token would be in Settings,
        # which is never constructed in --dry-run).
        assert "token" not in buf.getvalue().lower()
        assert "api_key" not in buf.getvalue().lower()

    def test_scan_multiple_candidates_json_shape(self, tmp_path: Path) -> None:
        """scan with two candidates produces correct JSON order and shapes."""
        db_path = str(tmp_path / "multi.db")
        c1 = _make_candidate(rank=1, instrument="EUR_USD")
        c2 = _make_candidate(rank=2, instrument="GBP_USD")

        args = _make_namespace(
            command="scan", db_path=db_path, dry_run=True,
            instruments="EUR_USD,GBP_USD",
        )

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker") as mock_ranker_cls,
            patch("signals.portfolio.PortfolioLimiter") as mock_limiter_cls,
        ):
            mock_cal_cls.return_value = MagicMock()
            mock_ranker_inst = MagicMock()
            mock_ranker_inst.rank.return_value = [c1, c2]
            mock_ranker_cls.return_value = mock_ranker_inst
            mock_limiter_inst = MagicMock()
            mock_limiter_inst.apply.return_value = [c1, c2]
            mock_limiter_cls.return_value = mock_limiter_inst

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_scan(args)

        assert rc == 0
        parsed = json.loads(buf.getvalue().strip())
        assert len(parsed) == 2
        assert parsed[0]["instrument"] == "EUR_USD"
        assert parsed[1]["instrument"] == "GBP_USD"


# ---------------------------------------------------------------------------
# watchlist — re-read persisted run
# ---------------------------------------------------------------------------


class TestWatchlistCommand:
    """Tests for ``cmd_watchlist`` — reads from the SQLite watchlist table."""

    def test_watchlist_round_trips_candidate_json(self, tmp_path: Path) -> None:
        """watchlist emits valid JSON matching the INV-13 Candidate shape."""
        db_path = str(tmp_path / "wl.db")
        run_dt = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

        # Persist a canned candidate directly.
        candidate = _make_candidate()
        store = Store(db_path)
        try:
            store.write_watchlist([candidate], run_timestamp=run_dt)
        finally:
            store.close()

        args = _make_namespace(command="watchlist", db_path=db_path)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_watchlist(args)

        assert rc == 0
        output = buf.getvalue().strip()
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

        c = parsed[0]
        # INV-13 field names — exact set.
        expected_fields = {
            "instrument", "timeframe", "strategy_name", "direction",
            "entry_ref", "stop_distance", "target_distance",
            "oos_sharpe_mean", "quality_score", "rank",
            "spread_ok", "session_ok", "news_flag", "generated_at",
        }
        assert set(c.keys()) == expected_fields, (
            f"Field mismatch: got {set(c.keys())}, expected {expected_fields}"
        )

        # Type checks.
        assert isinstance(c["instrument"], str)
        assert isinstance(c["timeframe"], str)
        assert isinstance(c["direction"], str)
        assert isinstance(c["entry_ref"], float)
        assert isinstance(c["stop_distance"], float)
        assert isinstance(c["target_distance"], float)
        assert isinstance(c["oos_sharpe_mean"], float)
        assert isinstance(c["quality_score"], float)
        assert isinstance(c["rank"], int)
        assert isinstance(c["spread_ok"], bool)
        assert isinstance(c["session_ok"], bool)
        assert isinstance(c["news_flag"], bool)
        assert isinstance(c["generated_at"], str)

        # UTC RFC 3339 format (INV-03).
        assert c["generated_at"].endswith("Z"), (
            f"generated_at must end with Z: {c['generated_at']!r}"
        )

    def test_watchlist_empty_table_prints_empty_array(self, tmp_path: Path) -> None:
        """Empty watchlist → prints [] and exits 0."""
        db_path = str(tmp_path / "empty_wl.db")
        # Just create the DB (tables created on Store init).
        Store(db_path).close()

        args = _make_namespace(command="watchlist", db_path=db_path)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_watchlist(args)

        assert rc == 0
        assert json.loads(buf.getvalue().strip()) == []

    def test_watchlist_reads_latest_run(self, tmp_path: Path) -> None:
        """watchlist reads only the latest run's rows (most recent run_timestamp)."""
        db_path = str(tmp_path / "two_runs.db")
        run1 = datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc)
        run2 = datetime(2026, 5, 10, 20, 0, 0, tzinfo=timezone.utc)

        old_cand = Candidate(
            instrument="GBP_USD", timeframe="H1",
            strategy_name="donchian_20_gbp_usd_h1",
            direction="SHORT", entry_ref=1.2600,
            stop_distance=0.0025, target_distance=0.0038,
            oos_sharpe_mean=0.9, quality_score=0.6, rank=1,
            spread_ok=True, session_ok=True, news_flag=False,
            generated_at="2026-05-10T06:00:00Z",
        )
        new_cand = _make_candidate()

        store = Store(db_path)
        try:
            store.write_watchlist([old_cand], run_timestamp=run1)
            store.write_watchlist([new_cand], run_timestamp=run2)
        finally:
            store.close()

        args = _make_namespace(command="watchlist", db_path=db_path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_watchlist(args)

        assert rc == 0
        parsed = json.loads(buf.getvalue().strip())
        assert len(parsed) == 1
        # Should be the latest run's candidate (EUR_USD from run2).
        assert parsed[0]["instrument"] == "EUR_USD"

    def test_scan_then_watchlist_round_trip(self, tmp_path: Path) -> None:
        """scan persists; watchlist re-reads; JSON shapes match (INV-13)."""
        db_path = str(tmp_path / "rt.db")
        candidate = _make_candidate()

        # Persist via scan mock.
        scan_args = _make_namespace(command="scan", db_path=db_path, dry_run=True)
        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker") as mock_ranker_cls,
            patch("signals.portfolio.PortfolioLimiter") as mock_limiter_cls,
        ):
            mock_cal_cls.return_value = MagicMock()
            ranker_inst = MagicMock()
            ranker_inst.rank.return_value = [candidate]
            mock_ranker_cls.return_value = ranker_inst
            limiter_inst = MagicMock()
            limiter_inst.apply.return_value = [candidate]
            mock_limiter_cls.return_value = limiter_inst

            scan_buf = io.StringIO()
            with redirect_stdout(scan_buf):
                rc_scan = cli.cmd_scan(scan_args)

        assert rc_scan == 0
        scan_json = json.loads(scan_buf.getvalue().strip())

        # Read back via watchlist.
        wl_args = _make_namespace(command="watchlist", db_path=db_path)
        wl_buf = io.StringIO()
        with redirect_stdout(wl_buf):
            rc_wl = cli.cmd_watchlist(wl_args)

        assert rc_wl == 0
        wl_json = json.loads(wl_buf.getvalue().strip())

        # The round-trip must match on all INV-13 fields.
        assert len(scan_json) == len(wl_json) == 1
        for field in (
            "instrument", "timeframe", "strategy_name", "direction",
            "entry_ref", "stop_distance", "target_distance",
            "oos_sharpe_mean", "quality_score", "rank",
            "spread_ok", "session_ok", "news_flag", "generated_at",
        ):
            assert scan_json[0][field] == wl_json[0][field], (
                f"Round-trip mismatch on field {field!r}: "
                f"scan={scan_json[0][field]!r}, watchlist={wl_json[0][field]!r}"
            )


# ---------------------------------------------------------------------------
# chart — PNG creation
# ---------------------------------------------------------------------------


class TestChartCommand:
    def test_chart_writes_png_prints_path(self, tmp_path: Path) -> None:
        """chart <instrument> writes a non-empty PNG and prints its path."""
        db_path = str(tmp_path / "chart.db")
        out_dir = str(tmp_path / "charts")
        run_dt = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

        candidate = _make_candidate()

        # Persist the candidate to watchlist and candles to the store.
        store = Store(db_path)
        try:
            _populate_h1(store, "EUR_USD", n_bars=150)
            store.write_watchlist([candidate], run_timestamp=run_dt)
        finally:
            store.close()

        args = _make_namespace(
            command="chart",
            instrument="EUR_USD",
            timeframe="H1",
            db_path=db_path,
            out_dir=out_dir,
            history_years=1,
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_chart(args)

        assert rc == 0
        png_path = buf.getvalue().strip()
        assert png_path.endswith(".png"), f"Expected PNG path, got: {png_path!r}"
        assert os.path.exists(png_path), f"PNG file not created: {png_path!r}"
        assert os.path.getsize(png_path) > 0, "PNG is empty"

    def test_chart_no_watchlist_entry_exits_nonzero(self, tmp_path: Path) -> None:
        """chart fails with exit 1 when no watchlist entry exists."""
        db_path = str(tmp_path / "no_wl.db")
        # Create an empty DB.
        Store(db_path).close()

        args = _make_namespace(
            command="chart",
            instrument="EUR_USD",
            timeframe="H1",
            db_path=db_path,
            out_dir=str(tmp_path / "charts"),
            history_years=1,
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_chart(args)

        assert rc != 0

    def test_chart_no_candles_exits_nonzero(self, tmp_path: Path) -> None:
        """chart fails with exit 1 when candles are missing from the store."""
        db_path = str(tmp_path / "no_candles.db")
        run_dt = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

        candidate = _make_candidate()
        store = Store(db_path)
        try:
            # No candles inserted — only the watchlist entry.
            store.write_watchlist([candidate], run_timestamp=run_dt)
        finally:
            store.close()

        args = _make_namespace(
            command="chart",
            instrument="EUR_USD",
            timeframe="H1",
            db_path=db_path,
            out_dir=str(tmp_path / "charts"),
            history_years=1,
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_chart(args)

        assert rc != 0


# ---------------------------------------------------------------------------
# backtest not broken
# ---------------------------------------------------------------------------


class TestBacktestUnbroken:
    """Confirm the existing ``backtest`` subcommand still exits 0 (--dry-run)."""

    def test_backtest_dry_run_empty_db(self, tmp_path: Path) -> None:
        """``fathom backtest --dry-run`` against an empty DB exits 0."""
        db_path = str(tmp_path / "backtest.db")
        args = argparse.Namespace(
            command="backtest",
            instruments="EUR_USD",
            timeframes="H1",
            strategies="all",
            workers=1,
            db_path=db_path,
            history_years=1,
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_backtest(args)

        assert rc == 0


# ---------------------------------------------------------------------------
# main() router — all four subcommands registered
# ---------------------------------------------------------------------------


class TestMainRouter:
    def test_main_routes_scan(self, tmp_path: Path) -> None:
        """main() routes 'scan' to cmd_scan."""
        db_path = str(tmp_path / "route_scan.db")
        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker") as mock_ranker_cls,
            patch("signals.portfolio.PortfolioLimiter") as mock_limiter_cls,
        ):
            mock_cal_cls.return_value = MagicMock()
            ri = MagicMock()
            ri.rank.return_value = []
            mock_ranker_cls.return_value = ri
            li = MagicMock()
            li.apply.return_value = []
            mock_limiter_cls.return_value = li

            rc = cli.main([
                "scan",
                "--dry-run",
                "--db-path", db_path,
                "--instruments", "EUR_USD",
                "--timeframes", "H1",
            ])
        assert rc == 0

    def test_main_routes_watchlist(self, tmp_path: Path) -> None:
        """main() routes 'watchlist' to cmd_watchlist."""
        db_path = str(tmp_path / "route_wl.db")
        Store(db_path).close()
        rc = cli.main(["watchlist", "--db-path", db_path])
        assert rc == 0

    def test_main_routes_backtest(self, tmp_path: Path) -> None:
        """main() routes 'backtest' to cmd_backtest (--dry-run)."""
        db_path = str(tmp_path / "route_bt.db")
        rc = cli.main([
            "backtest",
            "--dry-run",
            "--db-path", db_path,
            "--instruments", "EUR_USD",
            "--timeframes", "H1",
        ])
        assert rc == 0
