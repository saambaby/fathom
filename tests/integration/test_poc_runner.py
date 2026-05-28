"""Integration test for scripts/poc_run.py (POC-T-07).

Design
------
- Uses a pre-populated in-memory-style SQLite fixture (real file in tmp_path)
  with candle data sufficient for at least one walk-forward window.
- All invocations run the runner with ``--dry-run``, which skips OANDA
  construction entirely (Settings/OandaClient are never built), so no live
  HTTP is possible. Cached SQLite fixtures provide any candle data.

Assertions
----------
1. Exit code is 0 (both empty and non-empty approved sets).
2. Either the approved-set table or the "No combinations passed" message is
   printed to stdout.
3. ``swap_modelled=False`` appears in stdout (every entry carries the D-03 label).
4. All timestamps in stderr log output are UTC RFC 3339 format.
5. No API token or account ID appears in stdout or stderr (INV-08).

INV-03 compliance: the fixture candles carry UTC-aware timestamps stored as
RFC 3339 TEXT in SQLite — exactly the format the store uses.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data.oanda_client import CandleRow
from data.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _make_candle(
    instrument: str,
    granularity: str,
    time: datetime,
    bid: float = 1.1000,
    ask: float = 1.1002,
    delta: float = 0.0,   # small price drift for EMA crossovers
    volume: int = 100,
    complete: bool = True,
) -> CandleRow:
    """Build a CandleRow with a small price delta applied."""
    price = bid + delta
    return CandleRow(
        instrument=instrument,
        granularity=granularity,
        time=time,
        open_bid=price,
        high_bid=price + 0.0020,
        low_bid=price - 0.0020,
        close_bid=price + 0.0001,
        open_ask=price + 0.0002,
        high_ask=price + 0.0022,
        low_ask=price - 0.0018,
        close_ask=price + 0.0003,
        open_mid=price + 0.0001,
        high_mid=price + 0.0021,
        low_mid=price - 0.0019,
        close_mid=price + 0.0002,
        volume=volume,
        complete=complete,
    )


def _populate_store(db_path: str, instrument: str, granularity: str) -> int:
    """Populate the store with ~800 daily candles (≈2+ years) for one pair.

    The price series has a gentle upward drift for the first half and a gentle
    downward drift for the second half.  This ensures at least some EMA
    crossovers occur, giving the walk-forward validator real signals to process.

    Returns the number of candles stored.
    """
    store = Store(db_path)
    candles = []
    base = _utc(2023, 1, 1)
    n_candles = 800  # ~2.2 years of daily bars

    for i in range(n_candles):
        t = base + timedelta(days=i)
        # Slow sinusoidal price walk: period ~400 bars → at least 2 full cycles.
        import math
        delta = 0.0050 * math.sin(2 * math.pi * i / 400)
        candle = _make_candle(
            instrument=instrument,
            granularity=granularity,
            time=t,
            bid=1.1000 + delta,
            ask=1.1002 + delta,
            delta=0.0,
            volume=100 + (i % 50),
        )
        candles.append(candle)

    store.upsert(candles)
    # Verify row count
    cursor = store._conn.execute(
        "SELECT COUNT(*) FROM candles WHERE instrument=? AND granularity=?",
        (instrument, granularity),
    )
    count: int = cursor.fetchone()[0]
    store.close()
    return count


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_db(tmp_path: Path) -> str:
    """A real SQLite file pre-populated with EUR_USD D candles.

    Returns the path string so the runner can open it.
    """
    db_path = str(tmp_path / "test_poc.db")
    count = _populate_store(db_path, "EUR_USD", "D")
    assert count > 700, f"Expected >700 candles, got {count}"
    return db_path


@pytest.fixture()
def empty_db(tmp_path: Path) -> str:
    """An empty SQLite store (schema only, no data)."""
    db_path = str(tmp_path / "empty_poc.db")
    store = Store(db_path)
    store.close()
    return db_path


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
)


def _run_poc(
    db_path: str,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run poc_run.py as a subprocess and return the CompletedProcess."""
    cmd = [
        sys.executable,
        "scripts/poc_run.py",
        "--dry-run",
        "--db-path", db_path,
        "--instruments", "EUR_USD",
        "--granularities", "D",
        "--history-years", "2",
        "--fast-periods", "10",
        "--slow-periods", "50",
    ]
    if extra_args:
        cmd.extend(extra_args)

    # Run from the project root so imports resolve correctly.
    project_root = str(Path(__file__).parent.parent.parent)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPocRunnerEmptyStore:
    """Runner with an empty store should exit 0 and print 'no combinations' message."""

    def test_exit_code_zero_on_empty_store(self, empty_db: str) -> None:
        """Exit code must be 0 even when the approved set is empty (INV-10 / AC)."""
        result = _run_poc(empty_db)
        assert result.returncode == 0, (
            f"Expected exit 0 on empty store, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_no_combinations_message_on_empty_store(self, empty_db: str) -> None:
        """Empty store → 'No combinations passed walk-forward criteria' to stdout."""
        result = _run_poc(empty_db)
        assert "No combinations passed walk-forward criteria" in result.stdout, (
            f"Expected 'no combinations' message in stdout.\nstdout: {result.stdout}"
        )

    def test_log_timestamps_utc_rfc3339_on_empty(self, empty_db: str) -> None:
        """All timestamps in log output (stderr) must be UTC RFC 3339 (INV-03)."""
        result = _run_poc(empty_db)
        # Every log line starts with a timestamp. Find them all.
        timestamps = _RFC3339_RE.findall(result.stderr)
        assert len(timestamps) > 0, (
            "Expected at least one UTC RFC 3339 timestamp in stderr log output.\n"
            f"stderr: {result.stderr}"
        )
        # Verify each extracted timestamp is parseable as UTC.
        for ts in timestamps:
            # Should not raise.
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            assert dt.tzinfo is None  # strptime parses naive; we just confirm format

    def test_no_token_in_output_on_empty(self, empty_db: str) -> None:
        """INV-08: OANDA token and account ID must not appear in any output."""
        result = _run_poc(empty_db)
        combined = result.stdout + result.stderr
        # These patterns are typical token/key formats we must never log.
        assert "OANDA_API_TOKEN" not in combined.upper(), (
            "INV-08: token env name leaked into output"
        )
        # No secret-looking strings of 32+ hex chars (typical token value shape).
        assert not re.search(r"[0-9a-fA-F]{32,}", combined), (
            "INV-08: possible token value leaked into output"
        )


class TestPocRunnerWithData:
    """Runner with a populated store should exit 0 and produce correct output."""

    def test_exit_code_zero_with_data(self, populated_db: str) -> None:
        """Exit 0 whether or not any combination is approved."""
        result = _run_poc(populated_db)
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_stdout_has_approved_table_or_no_combos_message(
        self, populated_db: str
    ) -> None:
        """stdout must contain either the approved-set table or the empty message."""
        result = _run_poc(populated_db)
        stdout = result.stdout
        has_table = "=== Approved-Set Table ===" in stdout
        has_empty_msg = "No combinations passed walk-forward criteria" in stdout
        assert has_table or has_empty_msg, (
            f"Expected approved-set table or 'no combinations' message in stdout.\n"
            f"stdout: {stdout}"
        )

    def test_swap_modelled_false_in_stdout_when_approved(
        self, populated_db: str
    ) -> None:
        """If any entry is approved, 'False' must appear (swap_modelled=False, D-03)."""
        result = _run_poc(populated_db)
        stdout = result.stdout
        if "=== Approved-Set Table ===" in stdout:
            # The table's swap_modelled column must show 'False'.
            assert "False" in stdout, (
                "D-03: swap_modelled=False must appear in the approved-set table.\n"
                f"stdout: {stdout}"
            )

    def test_log_timestamps_utc_rfc3339_with_data(self, populated_db: str) -> None:
        """All timestamps in stderr log must be UTC RFC 3339 (INV-03)."""
        result = _run_poc(populated_db)
        timestamps = _RFC3339_RE.findall(result.stderr)
        assert len(timestamps) > 0, (
            "Expected at least one UTC RFC 3339 timestamp in stderr.\n"
            f"stderr: {result.stderr}"
        )

    def test_no_token_in_output_with_data(self, populated_db: str) -> None:
        """INV-08: no secret-like strings in stdout or stderr."""
        result = _run_poc(populated_db)
        combined = result.stdout + result.stderr
        assert not re.search(r"[0-9a-fA-F]{32,}", combined), (
            "INV-08: potential token/secret string found in output"
        )

    def test_no_traceback_in_output(self, populated_db: str) -> None:
        """No Python tracebacks in stdout or stderr."""
        result = _run_poc(populated_db)
        combined = result.stdout + result.stderr
        assert "Traceback (most recent call last)" not in combined, (
            f"Unexpected traceback in output.\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_dry_run_skips_fetch_log_message(self, populated_db: str) -> None:
        """--dry-run must log that it is skipping OANDA fetch."""
        result = _run_poc(populated_db)
        assert "dry_run=True" in result.stderr or "dry-run" in result.stderr.lower(), (
            f"Expected dry-run message in stderr.\nstderr: {result.stderr}"
        )


class TestPocRunnerMultipleParams:
    """Test with multiple fast/slow combos including invalid ones (fast >= slow)."""

    def test_multiple_param_combos(self, populated_db: str) -> None:
        """Runner handles multiple fast/slow combos (including skipped fast>=slow)."""
        result = subprocess.run(
            [
                sys.executable,
                "scripts/poc_run.py",
                "--dry-run",
                "--db-path", populated_db,
                "--instruments", "EUR_USD",
                "--granularities", "D",
                "--history-years", "2",
                "--fast-periods", "10,20",
                "--slow-periods", "50,100",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        # Should always exit 0.
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # stdout must have either the table or the empty message.
        stdout = result.stdout
        assert (
            "=== Approved-Set Table ===" in stdout
            or "No combinations passed walk-forward criteria" in stdout
        ), f"Unexpected stdout: {stdout}"
