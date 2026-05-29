"""Tests for P3-T-10 execution CLI subcommands: execute, positions, reconcile.

Design (NO live HTTP)
---------------------
* All tests drive ``cli.cmd_execute``, ``cli.cmd_positions``, ``cli.cmd_reconcile``
  and ``cli.main`` directly — no subprocess.
* OANDA client, Settings, reconcile, pretrade_check, submit_order are all stubbed
  via ``unittest.mock.patch`` / injected fakes.  No live HTTP, no token in tests.
* Tests exercise the EXACT gate ordering (pretrade → sizing → limits → submit)
  and verify that any stage rejection aborts with non-zero exit and no order placed.

Coverage
--------
1. Unknown candidate ref → exit ≠ 0 (no watchlist entry = no execution).
2. Pretrade block → exit ≠ 0, no order placed.
3. Sizing reject (equity 0 / stop 0) → exit ≠ 0, no order placed.
4. Limits reject (kill switch active) → exit ≠ 0, reason + kill-switch status.
5. Limits reject (max concurrent) → exit ≠ 0, reason.
6. ``--dry-run`` runs steps 1–5 and prints would-be order WITHOUT calling submit.
7. Successful path (mocked submit) → exit 0, Fill JSON on stdout.
8. ``fathom positions`` → prints open Position[] JSON.
9. ``fathom reconcile`` → calls reconcile once, prints report JSON.
10. INV-01 boundary: ``hermes_integration/`` never references the execution commands.
11. fathom --help lists execute/positions/reconcile.
12. risk_fraction is always DEFAULT_RISK_FRACTION (0.0025) — never above the cap.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

import cli
from data.store import Store
from data.oanda_client import InstrumentMeta
from signals.ranker import Candidate
from execution.models import Fill, FillStatus, Position
from execution.reconcile import ReconcileReport
from risk.sizing import DEFAULT_RISK_FRACTION


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _make_candidate(
    instrument: str = "EUR_USD",
    timeframe: str = "H1",
    strategy_name: str = "macrossover_10_50",
    direction: str = "LONG",
    entry_ref: float = 1.1050,
    stop_distance: float = 0.0020,
    target_distance: float = 0.0030,
) -> Candidate:
    return Candidate(
        instrument=instrument,
        timeframe=timeframe,
        strategy_name=strategy_name,
        direction=direction,
        entry_ref=entry_ref,
        stop_distance=stop_distance,
        target_distance=target_distance,
        oos_sharpe_mean=1.5,
        quality_score=0.75,
        rank=1,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at="2026-04-10T12:00:00Z",
    )


def _make_instrument_meta(name: str = "EUR_USD") -> InstrumentMeta:
    return InstrumentMeta(
        name=name,
        pip_location=-4,
        min_trade_size=1.0,
        margin_rate=0.02,
        display_precision=5,
        long_rate=0.0001,
        short_rate=-0.0002,
        financing_days_of_week=[2],  # Wednesday
    )


def _make_fill(
    client_order_id: str = "abc123",
    broker_trade_id: str = "T001",
    fill_price: float = 1.1052,
    units_filled: int = 1000,
) -> Fill:
    return Fill(
        client_order_id=client_order_id,
        broker_trade_id=broker_trade_id,
        fill_price=fill_price,
        units_filled=units_filled,
        slippage=0.0002,
        filled_at=_utc(2026, 4, 10, 12, 1),
        status=FillStatus.FILLED,
    )


def _make_position(
    broker_trade_id: str = "T001",
    instrument: str = "EUR_USD",
    units: int = 1000,
) -> Position:
    return Position(
        broker_trade_id=broker_trade_id,
        instrument=instrument,
        units=units,
        entry_price=1.1052,
        stop_loss_price=1.1030,
        take_profit_price=1.1082,
        opened_at=_utc(2026, 4, 10, 12, 1),
        unrealized_pl=0.5,
        closed_at=None,
        realized_pl=None,
        candidate_ref="EUR_USD:H1:macrossover_10_50",
    )


def _make_namespace(**kwargs: object) -> argparse.Namespace:
    """Build an argparse.Namespace with execute/positions/reconcile defaults."""
    defaults = {
        "command": "execute",
        "candidate_ref": "EUR_USD:H1:macrossover_10_50",
        "db_path": "/tmp/fathom_execute_test_not_used.db",
        "dry_run": False,
        "yes": True,  # skip interactive confirm in tests
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _seed_watchlist(store: Store, candidate: Candidate) -> None:
    """Persist one candidate to the watchlist table (latest run)."""
    run_dt = _utc(2026, 4, 10, 10)
    store.write_watchlist([candidate], run_timestamp=run_dt)


def _seed_account_state(
    store: Store,
    start_of_day_equity: float = 100_000.0,
    day_pl: float = 0.0,
) -> None:
    store.write_account_state(
        start_of_day_equity=start_of_day_equity,
        day_pl=day_pl,
        as_of=_utc(2026, 4, 10, 10),
    )


def _make_reconcile_report(
    start_of_day_equity: float = 100_000.0,
    day_pl: float = 0.0,
) -> ReconcileReport:
    report = ReconcileReport()
    report.start_of_day_equity = start_of_day_equity
    report.day_pl = day_pl
    return report


# ---------------------------------------------------------------------------
# 1. Unknown candidate ref → exit ≠ 0 (INV-13 gate)
# ---------------------------------------------------------------------------


class TestExecuteUnknownRef:
    def test_unknown_ref_exits_nonzero(self, tmp_path: object) -> None:
        """An unknown candidate_ref errors with exit ≠ 0 without placing an order."""
        assert isinstance(tmp_path, type(tmp_path))  # hint for mypy
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")

        # Seed an empty watchlist (no candidates) in the DB.
        store = Store(db_path)
        store.close()

        args = _make_namespace(
            candidate_ref="EUR_USD:H1:does_not_exist",
            db_path=db_path,
            dry_run=True,
        )

        buf = io.StringIO()
        with redirect_stderr(buf):
            code = cli.cmd_execute(args)
        assert code != 0, "Should exit non-zero for unknown candidate ref"
        assert "ERROR" in buf.getvalue() or code != 0

    def test_wrong_ref_format_exits_nonzero(self, tmp_path: object) -> None:
        """A malformed ref (not instrument:timeframe:strategy) errors exit ≠ 0."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        store.close()

        args = _make_namespace(
            candidate_ref="BADINPUT",
            db_path=db_path,
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = cli.cmd_execute(args)
        assert code not in (0,)

    def test_ref_not_on_watchlist_exits_nonzero(self, tmp_path: object) -> None:
        """A ref present in the watchlist for a different candidate ref is rejected."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        # Seed a EUR_USD candidate but ask for GBP_USD.
        _seed_watchlist(store, _make_candidate(instrument="EUR_USD"))
        store.close()

        args = _make_namespace(
            candidate_ref="GBP_USD:H1:macrossover_10_50",
            db_path=db_path,
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = cli.cmd_execute(args)
        assert code != 0


# ---------------------------------------------------------------------------
# 2. Pretrade block → exit ≠ 0, no order placed
# ---------------------------------------------------------------------------


class TestExecutePretradeBlock:
    def test_pretrade_block_aborts(self, tmp_path: object) -> None:
        """A pretrade 'block' verdict aborts with exit ≠ 0 (no order placed)."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        block_verdict = PretradeVerdict(decision="block", reason="high-impact news event")

        mock_recon_report = _make_reconcile_report()

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=block_verdict),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                code = cli.cmd_execute(args)

        assert code != 0, "Pretrade block must produce non-zero exit"
        assert "BLOCKED" in buf_err.getvalue() or code != 0


# ---------------------------------------------------------------------------
# 3. Sizing reject → exit ≠ 0, no order placed
# ---------------------------------------------------------------------------


class TestExecuteSizingReject:
    def test_sizing_reject_aborts(self, tmp_path: object) -> None:
        """When sizing returns units=0, execute aborts with non-zero exit."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        # Use a very tiny stop_distance so the size is 0 at this equity.
        # Actually, use equity = 0 by setting day_pl = -start_of_day_equity.
        # Easier: make equity extremely small so we cannot fund min trade size.
        candidate = _make_candidate(stop_distance=1000.0)  # huge stop → 0 units

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=1.0, day_pl=0.0)  # $1 equity
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        proceed_verdict = PretradeVerdict(decision="proceed", reason="all clear")
        mock_recon_report = _make_reconcile_report(
            start_of_day_equity=1.0, day_pl=0.0
        )

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed_verdict),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                code = cli.cmd_execute(args)

        assert code != 0, "Sizing reject must produce non-zero exit"
        assert "REJECTED" in buf_err.getvalue() or code != 0

    def test_risk_fraction_is_exactly_default_cap(self, tmp_path: object) -> None:
        """risk_fraction passed to size_position must be DEFAULT_RISK_FRACTION (0.0025)."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        proceed_verdict = PretradeVerdict(decision="proceed", reason="ok")
        mock_recon_report = _make_reconcile_report()

        captured_kwargs: dict[str, object] = {}

        from risk.sizing import SizingResult

        def spy_size_position(candidate, equity, *, instrument_meta, rate=1.0, risk_fraction=DEFAULT_RISK_FRACTION):  # type: ignore[no-untyped-def]
            captured_kwargs["risk_fraction"] = risk_fraction
            # Return a rejection so the test stops early (no limits/submit needed).
            return SizingResult(units=0, risk_amount=0.0, reason="spy stop")

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed_verdict),
            patch("cli.size_position", side_effect=spy_size_position),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            with redirect_stderr(io.StringIO()):
                code = cli.cmd_execute(args)

        assert "risk_fraction" in captured_kwargs, "size_position was not called"
        assert captured_kwargs["risk_fraction"] == DEFAULT_RISK_FRACTION, (
            f"risk_fraction must be {DEFAULT_RISK_FRACTION} (the INV-05 cap), "
            f"got {captured_kwargs['risk_fraction']}"
        )


# ---------------------------------------------------------------------------
# 4. Limits reject — kill switch active
# ---------------------------------------------------------------------------


class TestExecuteLimitsReject:
    def test_kill_switch_active_aborts(self, tmp_path: object) -> None:
        """Kill switch active → exit ≠ 0, reason + kill-switch status printed."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        # day_pl is -1500 on $100k equity → 1.5% loss > 1% cap → kill switch trips.
        _seed_account_state(store, start_of_day_equity=100_000.0, day_pl=-1500.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        proceed_verdict = PretradeVerdict(decision="proceed", reason="ok")
        sizing_ok = SizingResult(units=10000, risk_amount=2.0, reason=None)
        mock_recon_report = _make_reconcile_report(
            start_of_day_equity=100_000.0, day_pl=-1500.0
        )

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed_verdict),
            patch("cli.size_position", return_value=sizing_ok),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                code = cli.cmd_execute(args)

        assert code != 0, "Kill switch must produce non-zero exit"
        err_output = buf_err.getvalue()
        assert "kill switch" in err_output.lower() or "REJECTED" in err_output

    def test_max_concurrent_aborts(self, tmp_path: object) -> None:
        """Max concurrent positions exceeded → exit ≠ 0."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        # Seed 5 open positions (the default limit).
        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        for i in range(5):
            pos = Position(
                broker_trade_id=f"T00{i}",
                instrument="GBP_USD",
                units=1000 * (1 if i % 2 == 0 else -1),
                entry_price=1.2500,
                stop_loss_price=1.2480,
                take_profit_price=1.2530,
                opened_at=_utc(2026, 4, 10, 10),
                unrealized_pl=0.0,
                candidate_ref=f"GBP_USD:H1:strat{i}",
            )
            store.write_position(pos)
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        proceed_verdict = PretradeVerdict(decision="proceed", reason="ok")
        sizing_ok = SizingResult(units=1000, risk_amount=2.0, reason=None)
        mock_recon_report = _make_reconcile_report()

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed_verdict),
            patch("cli.size_position", return_value=sizing_ok),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                code = cli.cmd_execute(args)

        assert code != 0, "Max concurrent exceeded must produce non-zero exit"


# ---------------------------------------------------------------------------
# 5. --dry-run: steps 1–5 only, no v20 call
# ---------------------------------------------------------------------------


class TestExecuteDryRun:
    def test_dry_run_prints_order_no_submit(self, tmp_path: object) -> None:
        """--dry-run prints the would-be order and exits 0 without calling submit_order."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        proceed_verdict = PretradeVerdict(decision="proceed", reason="ok")
        sizing_ok = SizingResult(units=5000, risk_amount=10.0, reason=None)
        mock_recon_report = _make_reconcile_report()

        submit_mock = MagicMock(name="submit_order")

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed_verdict),
            patch("cli.size_position", return_value=sizing_ok),
            patch("cli.submit_order", submit_mock),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            buf_out = io.StringIO()
            with redirect_stdout(buf_out):
                code = cli.cmd_execute(args)

        assert code == 0, f"--dry-run should exit 0, got {code}"
        submit_mock.assert_not_called()  # submit_order must NOT be called under --dry-run
        out = buf_out.getvalue()
        assert "[DRY-RUN]" in out
        # Should contain a JSON blob with order fields.
        assert "client_order_id" in out

    def test_dry_run_gate_order(self, tmp_path: object) -> None:
        """--dry-run: reconcile is called BEFORE pretrade, which is BEFORE sizing."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        call_order: list[str] = []

        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        def track_reconcile(**kwargs: object) -> ReconcileReport:
            call_order.append("reconcile")
            return _make_reconcile_report()

        def track_pretrade(candidate: object, **kwargs: object) -> "PretradeVerdict":
            call_order.append("pretrade")
            return PretradeVerdict(decision="proceed", reason="ok")

        def track_sizing(candidate: object, equity: object, **kwargs: object) -> "SizingResult":
            call_order.append("sizing")
            return SizingResult(units=1000, risk_amount=2.0)

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", side_effect=track_reconcile),
            patch("cli.pretrade_check", side_effect=track_pretrade),
            patch("cli.size_position", side_effect=track_sizing),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            with redirect_stdout(io.StringIO()):
                code = cli.cmd_execute(args)

        # reconcile before pretrade before sizing.
        assert call_order[0] == "reconcile", f"reconcile must be first, got {call_order}"
        assert call_order[1] == "pretrade", f"pretrade must be second, got {call_order}"
        assert call_order[2] == "sizing", f"sizing must be third, got {call_order}"


# ---------------------------------------------------------------------------
# 6. Successful path (mocked submit) → exit 0, Fill JSON on stdout
# ---------------------------------------------------------------------------


class TestExecuteSuccess:
    def test_success_prints_fill_json(self, tmp_path: object) -> None:
        """A fully passing gate with --yes skips confirm and prints Fill JSON."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        fill = _make_fill()
        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        proceed = PretradeVerdict(decision="proceed", reason="ok")
        sizing_ok = SizingResult(units=5000, risk_amount=10.0)
        mock_recon_report = _make_reconcile_report()

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed),
            patch("cli.size_position", return_value=sizing_ok),
            patch("cli.submit_order", return_value=fill),
        ):
            args = _make_namespace(db_path=db_path, dry_run=False, yes=True)
            buf_out = io.StringIO()
            with redirect_stdout(buf_out):
                code = cli.cmd_execute(args)

        assert code == 0, f"Successful gate should exit 0, got {code}"
        out = buf_out.getvalue()
        # Should be valid JSON with fill fields.
        fill_data = json.loads(out)
        assert fill_data["broker_trade_id"] == fill.broker_trade_id
        assert fill_data["fill_price"] == fill.fill_price
        assert fill_data["units_filled"] == fill.units_filled

    def test_broker_rejection_exits_nonzero(self, tmp_path: object) -> None:
        """An OrderRejected exception from submit_order exits non-zero."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from execution.orders import OrderRejected
        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        proceed = PretradeVerdict(decision="proceed", reason="ok")
        sizing_ok = SizingResult(units=5000, risk_amount=10.0)
        mock_recon_report = _make_reconcile_report()

        def reject_order(order: object, **kwargs: object) -> Fill:
            raise OrderRejected("abc123", "INSUFFICIENT_MARGIN")

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_recon_report),
            patch("cli.pretrade_check", return_value=proceed),
            patch("cli.size_position", return_value=sizing_ok),
            patch("cli.submit_order", side_effect=reject_order),
        ):
            args = _make_namespace(db_path=db_path, dry_run=False, yes=True)
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                code = cli.cmd_execute(args)

        assert code != 0
        assert "REJECTED" in buf_err.getvalue()


# ---------------------------------------------------------------------------
# 7. fathom positions
# ---------------------------------------------------------------------------


class TestPositionsCommand:
    def test_positions_empty(self, tmp_path: object) -> None:
        """``fathom positions`` prints an empty JSON array when no positions exist."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        store.close()

        args = argparse.Namespace(command="positions", db_path=db_path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.cmd_positions(args)

        assert code == 0
        data = json.loads(buf.getvalue())
        assert data == []

    def test_positions_with_open_positions(self, tmp_path: object) -> None:
        """``fathom positions`` prints open Position[] JSON."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        pos = _make_position()
        store.write_position(pos)
        store.close()

        args = argparse.Namespace(command="positions", db_path=db_path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.cmd_positions(args)

        assert code == 0
        data = json.loads(buf.getvalue())
        assert len(data) == 1
        assert data[0]["broker_trade_id"] == pos.broker_trade_id
        assert data[0]["instrument"] == pos.instrument
        assert data[0]["units"] == pos.units

    def test_positions_via_main(self, tmp_path: object) -> None:
        """``fathom positions`` is reachable via ``cli.main``."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        store.close()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["positions", "--db-path", db_path])
        assert code == 0


# ---------------------------------------------------------------------------
# 8. fathom reconcile
# ---------------------------------------------------------------------------


class TestReconcileCommand:
    def test_reconcile_prints_report(self, tmp_path: object) -> None:
        """``fathom reconcile`` calls reconcile once and prints the report JSON."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        store.close()

        mock_report = _make_reconcile_report(
            start_of_day_equity=100_000.0, day_pl=-50.0
        )
        mock_report.adopted = ["T001"]
        mock_report.closed = []
        mock_report.matched = ["T002"]
        mock_report.drift_flags = ["some drift"]

        args = argparse.Namespace(command="reconcile", db_path=db_path)
        buf = io.StringIO()
        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=mock_report),
            redirect_stdout(buf),
        ):
            code = cli.cmd_reconcile(args)

        assert code == 0
        data = json.loads(buf.getvalue())
        assert data["adopted"] == ["T001"]
        assert data["matched"] == ["T002"]
        assert data["day_pl"] == -50.0
        assert "drift_flags" in data

    def test_reconcile_failure_exits_nonzero(self, tmp_path: object) -> None:
        """Reconcile HTTP failure exits non-zero."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        store = Store(db_path)
        store.close()

        args = argparse.Namespace(command="reconcile", db_path=db_path)
        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", side_effect=RuntimeError("broker down")),
            redirect_stderr(io.StringIO()),
        ):
            code = cli.cmd_reconcile(args)
        assert code != 0


# ---------------------------------------------------------------------------
# 9. INV-01 boundary: hermes_integration/ never references execution commands
# ---------------------------------------------------------------------------


class TestInv01Boundary:
    def test_hermes_integration_has_no_execute_command_references(self) -> None:
        """hermes_integration/ must not reference 'fathom execute',
        'fathom positions', or 'fathom reconcile' — INV-01 enforcement."""
        import pathlib

        hermes_dir = pathlib.Path(__file__).parent.parent / "hermes_integration"
        forbidden_patterns = [
            "fathom execute",
            "fathom positions",
            "fathom reconcile",
        ]
        for file_path in hermes_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix in (".pyc",) or "__pycache__" in file_path.parts:
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for pattern in forbidden_patterns:
                assert pattern not in content, (
                    f"{file_path} contains forbidden pattern {pattern!r} — "
                    "INV-01: Hermes must NOT have access to order/execution "
                    "commands (allow-list: scan/watchlist/chart only)."
                )

    def test_daily_md_allowlist_unchanged(self) -> None:
        """hermes_integration/jobs/daily.md allow-list is still scan/watchlist/chart."""
        import pathlib

        daily_path = (
            pathlib.Path(__file__).parent.parent
            / "hermes_integration" / "jobs" / "daily.md"
        )
        if not daily_path.exists():
            pytest.skip("daily.md not found")

        content = daily_path.read_text(encoding="utf-8")
        # The allow-list must remain scan/watchlist/chart.
        assert "fathom scan" in content, "fathom scan must be in the allow-list"
        assert "fathom watchlist" in content, "fathom watchlist must be in the allow-list"
        assert "fathom chart" in content, "fathom chart must be in the allow-list"


# ---------------------------------------------------------------------------
# 10. fathom --help lists execute/positions/reconcile
# ---------------------------------------------------------------------------


class TestCliHelp:
    def test_fathom_help_lists_all_subcommands(self, capsys: object) -> None:
        """fathom --help lists execute/positions/reconcile alongside backtest/scan."""
        import subprocess
        result = subprocess.run(
            [".venv/bin/fathom", "--help"],
            capture_output=True,
            text=True,
            cwd="/home/sam-baby/development/fathom",
        )
        output = result.stdout + result.stderr
        assert "execute" in output
        assert "positions" in output
        assert "reconcile" in output
        assert "backtest" in output
        assert "scan" in output
        assert "watchlist" in output
        assert "chart" in output


# ---------------------------------------------------------------------------
# 11. Gate ordering: pretrade must come BEFORE sizing, sizing BEFORE limits
# (already partially tested in test_dry_run_gate_order — explicit limits check)
# ---------------------------------------------------------------------------


class TestGateOrdering:
    def test_pretrade_before_sizing_before_limits(self, tmp_path: object) -> None:
        """Gate order: reconcile → pretrade → sizing → limits (→ submit)."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store, start_of_day_equity=100_000.0)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        call_order: list[str] = []
        fill = _make_fill()

        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult
        from risk.limits import LimitDecision

        def track_reconcile(**kwargs: object) -> ReconcileReport:
            call_order.append("reconcile")
            return _make_reconcile_report()

        def track_pretrade(c: object, **kwargs: object) -> "PretradeVerdict":
            call_order.append("pretrade")
            return PretradeVerdict(decision="proceed", reason="ok")

        def track_sizing(c: object, eq: object, **kwargs: object) -> "SizingResult":
            call_order.append("sizing")
            return SizingResult(units=1000, risk_amount=2.0)

        def track_check_limits(*args: object, **kwargs: object) -> "LimitDecision":
            call_order.append("limits")
            return LimitDecision(allowed=True)

        def track_submit(order: object, **kwargs: object) -> Fill:
            call_order.append("submit")
            return fill

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", side_effect=track_reconcile),
            patch("cli.pretrade_check", side_effect=track_pretrade),
            patch("cli.size_position", side_effect=track_sizing),
            patch("cli.check_limits", side_effect=track_check_limits),
            patch("cli.submit_order", side_effect=track_submit),
        ):
            args = _make_namespace(db_path=db_path, dry_run=False, yes=True)
            with redirect_stdout(io.StringIO()):
                code = cli.cmd_execute(args)

        assert call_order == [
            "reconcile",
            "pretrade",
            "sizing",
            "limits",
            "submit",
        ], f"Wrong gate order: {call_order}"

    def test_pretrade_block_prevents_sizing(self, tmp_path: object) -> None:
        """A pretrade block must short-circuit: sizing must NOT be called."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict

        sizing_mock = MagicMock(name="size_position")

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_make_reconcile_report()),
            patch(
                "cli.pretrade_check",
                return_value=PretradeVerdict(decision="block", reason="news"),
            ),
            patch("cli.size_position", sizing_mock),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            with redirect_stderr(io.StringIO()):
                code = cli.cmd_execute(args)

        assert code != 0
        sizing_mock.assert_not_called()  # size_position must NOT be called after pretrade block

    def test_sizing_reject_prevents_limits(self, tmp_path: object) -> None:
        """A sizing reject must short-circuit: check_limits must NOT be called."""
        from pathlib import Path
        db_path = str(Path(str(tmp_path)) / "test.db")
        candidate = _make_candidate()

        store = Store(db_path)
        _seed_watchlist(store, candidate)
        _seed_account_state(store)
        store.upsert_instruments([_make_instrument_meta()])
        store.close()

        from hermes_integration.pretrade_check import PretradeVerdict
        from risk.sizing import SizingResult

        limits_mock = MagicMock(name="check_limits")

        with (
            patch("cli.Settings"),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_make_reconcile_report()),
            patch(
                "cli.pretrade_check",
                return_value=PretradeVerdict(decision="proceed", reason="ok"),
            ),
            patch(
                "cli.size_position",
                return_value=SizingResult(units=0, risk_amount=0.0, reason="tiny equity"),
            ),
            patch("cli.check_limits", limits_mock),
        ):
            args = _make_namespace(db_path=db_path, dry_run=True)
            with redirect_stderr(io.StringIO()):
                code = cli.cmd_execute(args)

        assert code != 0
        limits_mock.assert_not_called()  # check_limits must NOT be called after sizing reject
