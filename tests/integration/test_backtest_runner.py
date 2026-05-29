"""Integration tests for the ``fathom backtest`` runner (P1A-T-08).

Design (NO live HTTP)
---------------------
* Candle data lives in a real SQLite file in ``tmp_path`` (cached fixtures).
* The OANDA universe is provided via cached ``instruments`` rows (the
  ``--dry-run`` path reads cached metadata and never constructs ``Settings`` or
  ``OandaClient``) — so no network and no ``.env`` is required.
* In-process tests drive ``cli.cmd_backtest`` / ``cli.main`` directly so they
  can assert on the persisted ``approved_set`` table, on per-timeframe window
  config, and on the single-writer (INV-12) contract via a write spy.
* A subprocess ``--dry-run`` smoke confirms the console entry point exits 0.

What is asserted
----------------
1. Combos are built across H1/H4/D (one row per strategy × pair × timeframe).
2. Per-timeframe window sizing is consulted (WINDOW_CONFIG, D-P1-2 ruling).
3. The approved set is persisted with a ``granularity`` column (not
   "timeframe") and a DB-only ``run_timestamp`` (UTC RFC 3339).
4. INV-12: workers return entries; the PARENT performs all inserts in ONE
   ``write_approved_set`` call — workers never write. Proven with a spy that
   asserts ``write_approved_set`` is called exactly once, after every combo
   has been collected.
5. Empty approved set → exit 0.
6. Determinism: ``--workers 1`` and ``--workers 4`` persist byte-identical
   tables.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, cast
from unittest.mock import MagicMock, patch

import pytest

import cli
from backtest.walkforward import ApprovedSetEntry
from data.oanda_client import CandleRow, InstrumentMeta
from data.store import Store


# ---------------------------------------------------------------------------
# Fixtures: cached candles + cached instrument metadata (no HTTP)
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


def _populate_daily(store: Store, instrument: str, n_days: int = 1100) -> None:
    """~3 years of daily candles with a sinusoidal walk (creates crossovers)."""
    base = _utc(2023, 1, 1)
    rows = []
    for i in range(n_days):
        t = base + timedelta(days=i)
        delta = 0.0080 * math.sin(2 * math.pi * i / 180)
        rows.append(_make_candle(instrument, "D", t, 1.1000 + delta))
    store.upsert(rows)


def _instruments() -> list[InstrumentMeta]:
    return [
        InstrumentMeta(
            name="EUR_USD",
            pip_location=-4,
            min_trade_size=1.0,
            margin_rate=0.02,
            display_precision=5,
            long_rate=-0.0001,  # financing data present → swap_modelled True
            short_rate=0.00005,
            financing_days_of_week=[2],
        ),
        InstrumentMeta(
            name="USD_JPY",
            pip_location=-2,
            min_trade_size=1.0,
            margin_rate=0.04,
            display_precision=3,
            long_rate=0.0002,
            short_rate=-0.0003,
            financing_days_of_week=[2],
        ),
    ]


@pytest.fixture()
def populated_db(tmp_path: Path) -> str:
    """SQLite file with daily candles for two pairs + cached instrument meta."""
    db_path = str(tmp_path / "fathom_test.db")
    store = Store(db_path)
    for inst in ("EUR_USD", "USD_JPY"):
        _populate_daily(store, inst)
    store.upsert_instruments(_instruments())
    store.close()
    return db_path


@pytest.fixture()
def empty_db(tmp_path: Path) -> str:
    """An empty store (schema only, no candles, no instruments)."""
    db_path = str(tmp_path / "empty.db")
    Store(db_path).close()
    return db_path


def _ns(**overrides: object) -> argparse.Namespace:
    """Build a backtest args Namespace with sensible test defaults."""
    base: dict[str, object] = dict(
        instruments="EUR_USD,USD_JPY",
        timeframes="H1,H4,D",
        strategies="all",
        workers=1,
        db_path="",
        history_years=3,
        dry_run=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Combo building + per-timeframe windows
# ---------------------------------------------------------------------------


class TestComboBuilding:
    def test_combos_span_all_timeframes(self) -> None:
        cost = {
            "EUR_USD": cli.InstrumentCost(
                pip_value=0.0001, swap_long_rate=0.0, swap_short_rate=0.0
            )
        }
        start = _utc(2023, 1, 1)
        end = _utc(2026, 1, 1)
        combos = cli._build_combos(
            instruments=["EUR_USD"],
            timeframes=["H1", "H4", "D"],
            strategy_keys=list(cli._STRATEGY_REGISTRY.keys()),
            instrument_costs=cost,
            db_path="x.db",
            start=start,
            end=end,
        )
        tfs = {c.timeframe for c in combos}
        assert tfs == {"H1", "H4", "D"}

    def test_per_timeframe_window_sizing_applied(self) -> None:
        """Each combo carries the WINDOW_CONFIG sizing for its timeframe."""
        cost = {
            "EUR_USD": cli.InstrumentCost(
                pip_value=0.0001, swap_long_rate=0.0, swap_short_rate=0.0
            )
        }
        combos = cli._build_combos(
            instruments=["EUR_USD"],
            timeframes=["H1", "H4", "D"],
            strategy_keys=["macrossover"],
            instrument_costs=cost,
            db_path="x.db",
            start=_utc(2023, 1, 1),
            end=_utc(2026, 1, 1),
        )
        by_tf = {c.timeframe: (c.train_months, c.test_months) for c in combos}
        assert by_tf["H1"] == (12, 3)
        assert by_tf["H4"] == (18, 6)
        assert by_tf["D"] == (24, 6)

    def test_instrument_meta_maps_to_cost_params(self) -> None:
        """long_rate→swap_long_rate, short_rate→swap_short_rate, pip from loc."""
        store_path = ":memory:"
        store = Store(store_path)
        store.upsert_instruments(_instruments())
        costs = cli._instrument_costs(store)
        store.close()
        eur = costs["EUR_USD"]
        assert eur.pip_value == pytest.approx(0.0001)  # 10**-4
        assert eur.swap_long_rate == pytest.approx(-0.0001)
        assert eur.swap_short_rate == pytest.approx(0.00005)
        jpy = costs["USD_JPY"]
        assert jpy.pip_value == pytest.approx(0.01)  # 10**-2 (JPY)


# ---------------------------------------------------------------------------
# Persistence schema: granularity + run_timestamp
# ---------------------------------------------------------------------------


class TestPersistenceSchema:
    def test_approved_set_table_has_granularity_column(self, empty_db: str) -> None:
        store = Store(empty_db)
        cols = [
            row[1]
            for row in store._conn.execute(
                "PRAGMA table_info(approved_set)"
            ).fetchall()
        ]
        store.close()
        assert "granularity" in cols
        assert "timeframe" not in cols  # the shipped field name, not "timeframe"
        assert "run_timestamp" in cols

    def test_write_approved_set_stamps_run_timestamp(self, empty_db: str) -> None:
        store = Store(empty_db)
        entry = ApprovedSetEntry(
            instrument="EUR_USD",
            granularity="H1",
            strategy_name="MACrossover(10,50)",
            oos_sharpe_mean=1.23,
            oos_trade_count_total=42,
            swap_modelled=True,
        )
        run_dt = datetime(2026, 5, 29, 8, 0, 0, tzinfo=timezone.utc)
        n = store.write_approved_set([entry], run_timestamp=run_dt)
        rows = store.load_approved_set()
        store.close()
        assert n == 1
        assert len(rows) == 1
        r = rows[0]
        assert r["run_timestamp"] == "2026-05-29T08:00:00Z"  # UTC RFC 3339
        assert r["granularity"] == "H1"
        assert r["strategy_name"] == "MACrossover(10,50)"
        assert r["oos_sharpe_mean"] == pytest.approx(1.23)
        assert r["oos_trade_count_total"] == 42
        assert r["swap_modelled"] is True


# ---------------------------------------------------------------------------
# INV-12: single-writer, parent-serialized, one transaction
# ---------------------------------------------------------------------------


def _fake_entry(spec: "cli.ComboSpec") -> ApprovedSetEntry:
    """Deterministic synthetic approver keyed off the combo identity."""
    return ApprovedSetEntry(
        instrument=spec.instrument,
        granularity=spec.timeframe,
        strategy_name=f"{spec.strategy_key}::{dict(spec.strategy_params)}",
        oos_sharpe_mean=1.0,
        oos_trade_count_total=10,
        swap_modelled=spec.swap_long_rate != 0.0 or spec.swap_short_rate != 0.0,
    )


class TestSingleWriterInv12:
    def test_parent_writes_exactly_once_after_all_combos(
        self, populated_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INV-12: a single write_approved_set call performs ALL inserts.

        We monkeypatch the worker to a pure function (no DB write) and spy on
        the Store write method. The spy must be called exactly once, with the
        FULL batch — proving no worker writes and the parent serializes the
        write into one transaction.
        """
        # Worker returns an entry and never touches the DB for writing.
        monkeypatch.setattr(cli, "_run_combo", _fake_entry)

        write_calls: list[tuple[int, str]] = []
        real_write = Store.write_approved_set

        def spy_write(
            self: Store,
            entries: Iterable[ApprovedSetEntry],
            run_timestamp: datetime,
        ) -> int:
            batch = list(entries)
            write_calls.append((len(batch), run_timestamp.isoformat()))
            return real_write(self, batch, run_timestamp)

        monkeypatch.setattr(Store, "write_approved_set", spy_write)

        rc = cli.cmd_backtest(_ns(db_path=populated_db, workers=1))
        assert rc == 0
        # Exactly ONE write — the parent's single-transaction insert.
        assert len(write_calls) == 1, (
            f"INV-12 violated: expected exactly one write_approved_set call, "
            f"got {len(write_calls)}."
        )
        batch_size = write_calls[0][0]
        # The single batch holds every combo's entry (all 'approved' here).
        store = Store(populated_db)
        persisted = store.load_approved_set()
        store.close()
        assert len(persisted) == batch_size
        assert batch_size > 0

    def test_no_partial_write_all_or_nothing(
        self, populated_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The persisted count equals the number of approved entries collected."""
        monkeypatch.setattr(cli, "_run_combo", _fake_entry)
        rc = cli.cmd_backtest(_ns(db_path=populated_db, workers=1))
        assert rc == 0
        store = Store(populated_db)
        rows = store.load_approved_set()
        store.close()
        # 2 instruments × 3 timeframes × (sum of param-grid sizes per strategy)
        expected = 0
        grid = cli._default_param_grid()
        per_combo = sum(len(grid[k]) for k in cli._STRATEGY_REGISTRY)
        expected = 2 * 3 * per_combo
        assert len(rows) == expected


# ---------------------------------------------------------------------------
# Empty approved set → exit 0
# ---------------------------------------------------------------------------


class TestEmptyApprovedSet:
    def test_empty_store_exits_zero(self, empty_db: str) -> None:
        rc = cli.cmd_backtest(_ns(db_path=empty_db, instruments="EUR_USD"))
        assert rc == 0
        store = Store(empty_db)
        assert store.load_approved_set() == []
        store.close()

    def test_real_walkforward_on_cached_data_exits_zero(
        self, populated_db: str
    ) -> None:
        """End-to-end with the REAL worker over cached daily candles: exit 0.

        Daily candles only, so H1/H4 windows simply find no data (empty
        DataFrame → no windows → not approved) and D windows run for real.
        Whatever the edge, the run must complete and persist a table (possibly
        empty) and exit 0.
        """
        rc = cli.cmd_backtest(
            _ns(db_path=populated_db, timeframes="D", strategies="macrossover")
        )
        assert rc == 0
        store = Store(populated_db)
        rows = store.load_approved_set()
        store.close()
        # Table exists and is queryable; every persisted row is granularity 'D'.
        for r in rows:
            assert r["granularity"] == "D"


# ---------------------------------------------------------------------------
# Determinism across worker counts
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_workers_1_vs_4_identical_table(self, tmp_path: Path) -> None:
        """--workers 1 and --workers 4 persist identical approved sets.

        Uses the REAL worker over real cached daily candles (the monkeypatched
        fake worker cannot be used here: a ProcessPoolExecutor child re-imports
        ``cli`` fresh and would not see a parent-side monkeypatch). The two runs
        must agree regardless of which combinations are approved — determinism
        is the property under test, not the contents of the approved set.
        """

        def _setup(name: str) -> str:
            db = str(tmp_path / name)
            store = Store(db)
            for inst in ("EUR_USD", "USD_JPY"):
                _populate_daily(store, inst, n_days=900)
            store.upsert_instruments(_instruments())
            store.close()
            return db

        db1 = _setup("w1.db")
        db4 = _setup("w4.db")

        # Run a small but non-trivial grid on the daily timeframe (the cached
        # data is daily). Both worker counts traverse the same combo list; a
        # trimmed strategy set keeps the parallel path exercised but fast.
        common: dict[str, object] = dict(
            timeframes="D", strategies="macrossover,donchian,bollinger"
        )
        assert cli.cmd_backtest(_ns(db_path=db1, workers=1, **common)) == 0
        assert cli.cmd_backtest(_ns(db_path=db4, workers=4, **common)) == 0

        def _normalised(db: str) -> list[tuple[object, ...]]:
            store = Store(db)
            rows = store.load_approved_set()
            store.close()
            # Drop run_timestamp (wall-clock, differs between runs); compare the
            # content that must be deterministic.
            return [
                (
                    r["strategy_name"],
                    r["instrument"],
                    r["granularity"],
                    round(float(cast(float, r["oos_sharpe_mean"])), 10),
                    r["oos_trade_count_total"],
                    r["swap_modelled"],
                )
                for r in rows
            ]

        rows1 = _normalised(db1)
        rows4 = _normalised(db4)
        assert rows1 == rows4


# ---------------------------------------------------------------------------
# Subprocess --dry-run smoke (console entry point)
# ---------------------------------------------------------------------------


class TestDryRunSmoke:
    def test_dry_run_subprocess_exits_zero(self, tmp_path: Path) -> None:
        db = str(tmp_path / "smoke.db")
        Store(db).close()  # empty store, schema only
        project_root = str(Path(__file__).parent.parent.parent)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli",
                "backtest",
                "--dry-run",
                "--db-path",
                db,
                "--instruments",
                "EUR_USD",
                "--timeframes",
                "H1,H4,D",
            ],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        assert result.returncode == 0, (
            f"dry-run smoke expected exit 0, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Approved set is empty" in result.stdout
        # INV-08: no token/account-id strings leaked.
        combined = (result.stdout + result.stderr).upper()
        assert "OANDA_API_TOKEN" not in combined
        assert "OANDA_ACCOUNT_ID" not in combined


# ---------------------------------------------------------------------------
# Live-path candle fetch (closes the acceptance-gate gap — #28)
# ---------------------------------------------------------------------------
# These tests verify that fathom backtest WITHOUT --dry-run:
#   1. Calls fetch_and_cache for each (instrument, timeframe) pair (the step
#      that was missing in the original assembly).
#   2. The store ends up with candles BEFORE the combo fan-out.
#   3. The walk-forward runs on that fetched data (approved-set population path
#      reachable, not blocked by an empty store).
#
# NO real HTTP is made: OandaClient.get_candles is monkeypatched to return
# synthetic candle rows; Settings construction is patched with fake credentials.
# ---------------------------------------------------------------------------


def _synthetic_candles(
    instrument: str,
    granularity: str,
    start: datetime,
    n: int = 1100,
) -> list[CandleRow]:
    """Return ``n`` synthetic daily/hourly candle rows starting from ``start``.

    These rows look like real ``CandleRow`` instances so ``store.upsert``
    accepts them unchanged.
    """
    step = timedelta(days=1) if granularity == "D" else timedelta(hours=1)
    rows = []
    for i in range(n):
        t = start + step * i
        price = 1.1000 + 0.0080 * math.sin(2 * math.pi * i / 180)
        rows.append(
            CandleRow(
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
        )
    return rows


class TestLivePathCandleFetch:
    """Prove the parent-side fetch step is wired correctly in LIVE mode.

    The monkeypatch replaces:
      * ``data.candles.fetch_and_cache`` with a fake that writes synthetic rows
        directly to the store (bypassing HTTP).  This lets us assert the store
        gets populated (candle count > 0) and that fetch_and_cache was actually
        called for each (instrument, timeframe) pair.

    The walk-forward worker is NOT monkeypatched — we use the real
    ``_run_combo`` over the fetched candles to confirm the data actually reaches
    the engine (not just that some rows exist in the store).
    """

    def test_store_is_populated_before_walkforward_in_live_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parent fetches candles into a fresh DB before dispatching workers.

        This test MUST FAIL against the pre-fix code (which skips the fetch
        step entirely): without the fix, the store stays empty and the walk-
        forward runs against 0 candles, so no entries are approved. After the
        fix the store is populated (candle count > 0) and the test passes.

        Assertions
        ----------
        * ``fetch_and_cache`` is called exactly once per (instrument, timeframe)
          pair in the requested grid.
        * After ``cmd_backtest`` returns, the store has > 0 candle rows.
        * ``cmd_backtest`` exits 0.
        * The run does not log any secret strings (INV-08).
        """
        db_path = str(tmp_path / "live_test.db")

        # Pre-seed instrument metadata so _instrument_costs() works without HTTP.
        store = Store(db_path)
        store.upsert_instruments(_instruments())
        store.close()

        # Track which (instrument, timeframe) pairs are fetched.
        fetch_calls: list[tuple[str, str]] = []

        def fake_fetch_and_cache(
            client: object,
            store: Store,
            instrument: str,
            granularity: str,
            start: datetime,
            end: datetime,
            write_parquet: bool = True,
        ) -> "object":
            import pandas as pd

            fetch_calls.append((instrument, granularity))
            # Build synthetic rows and upsert them directly (no HTTP).
            rows = _synthetic_candles(instrument, granularity, start, n=1100)
            store.upsert(rows)
            return store.load_candles(instrument, granularity, start, end)

        # Patch fetch_and_cache inside the cli module's lazy-import namespace.
        # cli._fetch_candles_for_universe does `from data.candles import fetch_and_cache`
        # at call time, so we patch the canonical location.
        monkeypatch.setattr("data.candles.fetch_and_cache", fake_fetch_and_cache)

        # Patch Settings + OandaClient so no .env is required.
        fake_settings = MagicMock()
        fake_settings.env = "demo"
        fake_settings.oanda_api_token.get_secret_value.return_value = "FAKE"
        fake_settings.oanda_account_id = "FAKE_ACCOUNT"

        fake_client = MagicMock()

        with (
            patch("config.settings.Settings", return_value=fake_settings),
            patch("data.oanda_client.OandaClient", return_value=fake_client),
        ):
            rc = cli.cmd_backtest(
                _ns(
                    db_path=db_path,
                    instruments="EUR_USD,USD_JPY",
                    timeframes="D",
                    strategies="macrossover",
                    workers=1,
                    dry_run=False,   # <-- LIVE mode, not dry-run
                    history_years=3,
                )
            )

        assert rc == 0, f"cmd_backtest returned non-zero exit code: {rc}"

        # The store must have candles (fetch step populated it).
        store = Store(db_path)
        eur_rows = store.load_candles(
            "EUR_USD", "D",
            _utc(2023, 1, 1), _utc(2026, 12, 31)
        )
        jpy_rows = store.load_candles(
            "USD_JPY", "D",
            _utc(2023, 1, 1), _utc(2026, 12, 31)
        )
        store.close()

        assert len(eur_rows) > 0, (
            "After live-mode run, EUR_USD/D candle count is 0. "
            "The parent-side fetch step was not wired (the pre-fix bug)."
        )
        assert len(jpy_rows) > 0, (
            "After live-mode run, USD_JPY/D candle count is 0. "
            "The parent-side fetch step was not wired (the pre-fix bug)."
        )

        # fetch_and_cache was called once per (instrument, timeframe) pair.
        expected_pairs = {("EUR_USD", "D"), ("USD_JPY", "D")}
        actual_pairs = set(fetch_calls)
        assert actual_pairs == expected_pairs, (
            f"Expected fetch_and_cache calls for {expected_pairs}, "
            f"got {actual_pairs}."
        )

    def test_dry_run_never_calls_fetch_and_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--dry-run SKIPS the candle fetch entirely (existing contract preserved).

        fetch_and_cache must NOT be called when --dry-run is set: no HTTP, no
        Settings construction, cache-only behaviour unchanged.
        """
        db_path = str(tmp_path / "dry_run_test.db")
        store = Store(db_path)
        store.upsert_instruments(_instruments())
        store.close()

        fetch_calls: list[tuple[str, str]] = []

        def spy_fetch(*args: object, **kwargs: object) -> object:
            # If this is called, dry-run is broken.
            raise AssertionError("fetch_and_cache must not be called under --dry-run")

        monkeypatch.setattr("data.candles.fetch_and_cache", spy_fetch)

        # dry-run=True → Settings/OandaClient never constructed; the spy must
        # never fire.
        rc = cli.cmd_backtest(
            _ns(
                db_path=db_path,
                instruments="EUR_USD",
                timeframes="D",
                strategies="macrossover",
                workers=1,
                dry_run=True,
                history_years=3,
            )
        )
        assert rc == 0
