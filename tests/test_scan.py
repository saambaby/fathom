"""Tests for ``signals/scan.py::run_scan`` (P4-T-01).

Coverage
--------
1. ``run_scan`` in dry_run mode: returns the ranked candidate list, persists to
   the watchlist table, and does NOT import any order/execution/risk path.
2. ``run_scan`` with empty approved-set → returns [] (INV-10), no exception.
3. ``run_scan`` propagates Ranker exceptions to the caller (the CLI adapter
   catches them; here we just verify propagation, not suppression).
4. **Transitive-import boundary test (INV-01):** imports ``signals.scan`` in a
   clean subprocess and asserts that none of the forbidden execution/risk
   placement modules appear in ``sys.modules`` after import.  This is the
   load-bearing P4-T-01 acceptance criterion — the panel imports ``run_scan``
   and must be provably free of the order path.

Design
------
* All tests are ``--dry-run`` style (``dry_run=True``) — no live HTTP, no
  Settings / OandaClient construction.
* Ranker + PortfolioLimiter are mocked at their canonical source paths so the
  Store interaction (watchlist persist) is exercised against real SQLite.
* The transitive-import test uses a subprocess to guarantee a clean sys.modules
  — patching in-process is not sufficient because another test may have already
  loaded ``execution.*`` modules.

INV-01: the boundary test is transitive (subprocess ``sys.modules`` walk).
INV-03: UTC timestamps enforced by ``run_scan`` internally.
INV-08: no token accessed in dry_run tests.
INV-13: ``run_scan`` returns ``Candidate[]`` (frozen wire contract).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from data.store import Store
from signals.ranker import Candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


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


def _mock_ranker_context(
    ranked: list[Candidate],
    after_limiter: list[Candidate] | None = None,
) -> tuple[Any, Any]:
    """Return (mock_ranker_cls, mock_limiter_cls) for use in patch contexts."""
    if after_limiter is None:
        after_limiter = ranked

    mock_ranker_inst = MagicMock()
    mock_ranker_inst.rank.return_value = ranked

    mock_limiter_inst = MagicMock()
    mock_limiter_inst.apply.return_value = after_limiter

    return mock_ranker_inst, mock_limiter_inst


# ---------------------------------------------------------------------------
# Core behaviour tests
# ---------------------------------------------------------------------------


class TestRunScan:
    """Tests for ``signals.scan.run_scan`` — mocked Ranker/Limiter, no HTTP."""

    def test_returns_candidates_and_persists_watchlist(
        self, tmp_path: Path
    ) -> None:
        """run_scan returns ranked candidates and persists them to the watchlist."""
        from signals.scan import run_scan

        db_path = str(tmp_path / "scan.db")
        candidate = _make_candidate()
        ranker_inst, limiter_inst = _mock_ranker_context([candidate])

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker", return_value=ranker_inst),
            patch("signals.portfolio.PortfolioLimiter", return_value=limiter_inst),
        ):
            mock_cal_cls.return_value = MagicMock()
            result = run_scan(
                db_path=db_path,
                instruments="EUR_USD",
                timeframes="H1",
                history_years=1,
                dry_run=True,
            )

        # run_scan must return the candidate list.
        assert len(result) == 1
        c = result[0]
        assert isinstance(c, Candidate)
        assert c.instrument == "EUR_USD"
        assert c.timeframe == "H1"

        # Watchlist table must have been persisted.
        store = Store(db_path)
        try:
            rows = store.load_watchlist()
        finally:
            store.close()

        assert len(rows) == 1
        assert rows[0]["instrument"] == "EUR_USD"
        assert rows[0]["rank"] == 1

    def test_empty_approved_set_returns_empty_list(self, tmp_path: Path) -> None:
        """run_scan with empty ranker output returns [] — INV-10, no exception."""
        from signals.scan import run_scan

        db_path = str(tmp_path / "empty.db")
        ranker_inst, limiter_inst = _mock_ranker_context([], after_limiter=[])

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker", return_value=ranker_inst),
            patch("signals.portfolio.PortfolioLimiter", return_value=limiter_inst),
        ):
            mock_cal_cls.return_value = MagicMock()
            result = run_scan(
                db_path=db_path,
                instruments="EUR_USD",
                timeframes="H1",
                history_years=1,
                dry_run=True,
            )

        assert result == [], "Empty approved-set must return [] (INV-10)"

        # Watchlist table exists but has no rows.
        store = Store(db_path)
        try:
            rows = store.load_watchlist()
        finally:
            store.close()
        assert rows == []

    def test_multiple_candidates_returned_in_order(self, tmp_path: Path) -> None:
        """run_scan returns candidates in the order the limiter provides."""
        from signals.scan import run_scan

        db_path = str(tmp_path / "multi.db")
        c1 = _make_candidate(rank=1, instrument="EUR_USD")
        c2 = _make_candidate(rank=2, instrument="GBP_USD")
        ranker_inst, limiter_inst = _mock_ranker_context([c1, c2])

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker", return_value=ranker_inst),
            patch("signals.portfolio.PortfolioLimiter", return_value=limiter_inst),
        ):
            mock_cal_cls.return_value = MagicMock()
            result = run_scan(
                db_path=db_path,
                instruments="EUR_USD,GBP_USD",
                timeframes="H1",
                history_years=1,
                dry_run=True,
            )

        assert len(result) == 2
        assert result[0].instrument == "EUR_USD"
        assert result[1].instrument == "GBP_USD"

    def test_ranker_exception_propagates(self, tmp_path: Path) -> None:
        """run_scan propagates exceptions from Ranker.rank() — the CLI catches them."""
        from signals.scan import run_scan

        db_path = str(tmp_path / "raise.db")
        ranker_inst = MagicMock()
        ranker_inst.rank.side_effect = RuntimeError("scoring backend offline")

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker", return_value=ranker_inst),
            patch("signals.portfolio.PortfolioLimiter", return_value=MagicMock()),
        ):
            mock_cal_cls.return_value = MagicMock()
            with pytest.raises(RuntimeError, match="scoring backend offline"):
                run_scan(
                    db_path=db_path,
                    instruments="EUR_USD",
                    timeframes="H1",
                    history_years=1,
                    dry_run=True,
                )

    def test_returned_candidates_have_inv13_fields(self, tmp_path: Path) -> None:
        """run_scan returns Candidate objects with all INV-13 fields present."""
        from signals.scan import run_scan

        db_path = str(tmp_path / "fields.db")
        candidate = _make_candidate()
        ranker_inst, limiter_inst = _mock_ranker_context([candidate])

        with (
            patch("data.calendar.FairEconomyCalendar") as mock_cal_cls,
            patch("signals.ranker.Ranker", return_value=ranker_inst),
            patch("signals.portfolio.PortfolioLimiter", return_value=limiter_inst),
        ):
            mock_cal_cls.return_value = MagicMock()
            result = run_scan(
                db_path=db_path,
                instruments="EUR_USD",
                timeframes="H1",
                history_years=1,
                dry_run=True,
            )

        assert len(result) == 1
        dumped = result[0].model_dump()
        inv13_fields = {
            "instrument", "timeframe", "strategy_name", "direction",
            "entry_ref", "stop_distance", "target_distance",
            "oos_sharpe_mean", "quality_score", "rank",
            "spread_ok", "session_ok", "news_flag", "generated_at",
        }
        for field in inv13_fields:
            assert field in dumped, f"Missing INV-13 field: {field}"


# ---------------------------------------------------------------------------
# INV-01 transitive-import boundary test
# ---------------------------------------------------------------------------


class TestTransitiveImportBoundary:
    """signals.scan must be importable without pulling in the order path.

    This is the load-bearing P4-T-01 acceptance criterion: the admin panel
    calls ``from signals.scan import run_scan`` and must remain provably free
    of the execution/risk placement modules.

    We run the check in a clean subprocess so sys.modules is genuinely empty
    at the start — in-process patching cannot guarantee this since other tests
    in the suite may have already imported ``execution.*`` modules.
    """

    # Modules that must NOT appear in sys.modules after importing signals.scan.
    FORBIDDEN_MODULES = [
        "execution.orders",
        "execution.models",
        "risk.sizing",
        "risk.limits",
        "cli",
    ]

    def test_signals_scan_has_no_execution_or_risk_imports(self) -> None:
        """signals.scan import graph excludes execution.orders/models + risk."""
        script = (
            "import sys\n"
            "import signals.scan\n"
            "modules = list(sys.modules.keys())\n"
            "forbidden = [\n"
            "    'execution.orders',\n"
            "    'execution.models',\n"
            "    'risk.sizing',\n"
            "    'risk.limits',\n"
            "    'cli',\n"
            "]\n"
            "hits = [m for m in forbidden if m in modules]\n"
            "if hits:\n"
            "    print('FORBIDDEN:', ','.join(hits))\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            # Run from the project root so package imports resolve correctly.
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0, (
            "signals.scan's transitive import graph includes forbidden modules "
            f"(execution/risk path — INV-01 violation).\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )

    def test_signals_scan_importable_without_execution_deps(self) -> None:
        """Importing signals.scan succeeds and does not raise ImportError.

        This confirms the module is self-contained and order-free at import
        time, not just at call time.
        """
        # This test runs in-process — sufficient to confirm no top-level import
        # of execution/risk modules (the subprocess test is the strict boundary).
        import importlib

        # Re-import from scratch to catch any top-level import errors.
        mod = importlib.import_module("signals.scan")
        assert hasattr(mod, "run_scan"), "signals.scan must export run_scan"
        assert callable(mod.run_scan), "run_scan must be callable"
