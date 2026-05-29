"""Fathom CLI — ``fathom backtest`` (P1A-T-08, the Phase 1A capstone).

The full-universe backtest runner: it walk-forward-validates **every**
requested (strategy × instrument × timeframe) combination and persists the
resulting approved-set table to SQLite.  That table is the **INV-10 gate** —
Phase 2's ranker loads it and refuses to operate if it is empty.

This module composes already-tested Phase 1A pieces:

* ``OandaClient.list_instruments()`` / ``Store.load_instruments()`` — universe
  discovery (or an explicit ``--instruments`` list).
* The six shipped strategies (``MACrossover``, ``DonchianBreakout``,
  ``BollingerReversion``, ``RSIReversion``, ``ROCMomentum``,
  ``SessionRangeBreakout``), each instantiated with a documented default param
  grid.
* ``BacktestEngine`` + the swap-aware ``CostParams`` (P1A-T-03) — every result
  is INV-06-valid (``swap_modelled=True`` wherever financing data is supplied).
* ``WalkForwardValidator`` with the strict per-window gate (every OOS window:
  Sharpe > 0 AND ≥ 5 trades), carried unchanged from the PoC.

Concurrency model (INV-12, single-writer / parent-serialized)
-------------------------------------------------------------
Each combo is embarrassingly parallel, so the runner fans out over a
``concurrent.futures.ProcessPoolExecutor``.  **Worker processes never touch the
database for writing** — each worker opens its OWN read connection to the
candle store, runs the validator, and returns a picklable ``ApprovedSetEntry``
(or ``None``) to the parent.  The PARENT collects *every* future first, then
performs ALL inserts in ONE transaction via ``Store.write_approved_set``.  A
partially-failed concurrent write (which INV-10 could not distinguish from a
legitimately small approved set) is therefore impossible.

Determinism
-----------
The approved set is independent of ``--workers``: the combo list is built in a
fixed, sorted order; ``ProcessPoolExecutor.map`` preserves input order; and the
parent additionally sorts the collected entries by
``(strategy_name, instrument, granularity)`` before writing.  ``--workers 1``
and ``--workers 4`` produce byte-identical tables.

Per-timeframe window sizing (D-P1-2 ruling)
-------------------------------------------
The daily timeframe was structurally starved on 3-month test windows in the
PoC (0–2 trades), so slower timeframes get longer windows:

    H1 → train 12m / test 3m
    H4 → train 18m / test 6m
    D  → train 24m / test 6m

INV-03 (all timestamps UTC RFC 3339) and INV-08 (never log the token/account
ID) are enforced throughout.

Usage
-----
    fathom backtest [--instruments ALL|EUR_USD,...] [--timeframes H1,H4,D]
                    [--strategies all|macrossover,donchian,...]
                    [--workers N] [--db-path PATH] [--history-years N]
                    [--dry-run]

An empty approved set is a valid, exit-0 result (INV-10: empty means "no
signals", not "all signals").
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from backtest.costs import CostParams
from backtest.engine import BacktestEngine
from backtest.walkforward import ApprovedSetEntry, WalkForwardValidator
from data.store import Store
from strategies.base import Strategy
from strategies.breakout import SessionRangeBreakout
from strategies.mean_reversion import BollingerReversion, RSIReversion
from strategies.momentum import ROCMomentum
from strategies.trend import DonchianBreakout, MACrossover

# ---------------------------------------------------------------------------
# Logging — UTC RFC 3339 timestamps (INV-03)
# ---------------------------------------------------------------------------


class _UTCFormatter(logging.Formatter):
    """Emit log records with UTC RFC 3339 timestamps (INV-03)."""

    def formatTime(  # noqa: N802 (logging API name)
        self, record: logging.LogRecord, datefmt: Optional[str] = None
    ) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_UTCFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    # Idempotent: don't stack handlers if called twice (e.g. in tests).
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


_log = logging.getLogger(__name__)


def _utc_now_rfc3339() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Per-timeframe walk-forward window config (D-P1-2 ruling)
# ---------------------------------------------------------------------------
# Slower timeframes get longer train/test windows so the daily timeframe is
# not structurally starved (the PoC's daily combos got 0–2 trades on 3-month
# windows). The strict per-window gate (Sharpe>0 AND >=5 trades) is unchanged.


@dataclass(frozen=True)
class WindowConfig:
    train_months: int
    test_months: int


WINDOW_CONFIG: dict[str, WindowConfig] = {
    "H1": WindowConfig(train_months=12, test_months=3),
    "H4": WindowConfig(train_months=18, test_months=6),
    "D": WindowConfig(train_months=24, test_months=6),
}

#: History to fetch/scan per timeframe must comfortably exceed the longest
#: train+test span so at least one window forms. The default below (3 years)
#: covers D (24m+6m = 30m) with margin; overridable via --history-years.
_DEFAULT_HISTORY_YEARS = 3


# ---------------------------------------------------------------------------
# Strategy param grids (documented defaults — one combo each unless noted)
# ---------------------------------------------------------------------------
# A "strategy spec" is a (key, label, kwargs) tuple. Kwargs are plain JSON-able
# values so the spec is trivially picklable; the worker rebuilds the strategy
# from the registry below. Each entry produces one strategy instance per
# (instrument, timeframe). Expand the lists here to widen the grid.

_StrategyFactory = Callable[..., Strategy]

#: Maps a strategy spec key → the constructor. The worker uses this to rebuild
#: the strategy from a picklable spec (constructors themselves are picklable,
#: but routing through a key keeps the spec a pure data object).
_STRATEGY_REGISTRY: dict[str, _StrategyFactory] = {
    "macrossover": MACrossover,
    "donchian": DonchianBreakout,
    "bollinger": BollingerReversion,
    "rsi": RSIReversion,
    "roc": ROCMomentum,
    "session": SessionRangeBreakout,
}


def _default_param_grid() -> dict[str, list[dict[str, object]]]:
    """Return the documented default param grid, keyed by strategy key.

    Each value is a list of kwargs dicts; one strategy instance is built per
    kwargs dict per (instrument, timeframe). ``instrument`` and ``timeframe``
    are injected per-combo by the worker, so they are omitted here.
    """
    return {
        "macrossover": [
            {"fast_period": 10, "slow_period": 50},
            {"fast_period": 20, "slow_period": 100},
        ],
        "donchian": [
            {"channel_period": 20},
            {"channel_period": 55},
        ],
        "bollinger": [
            {"period": 20, "num_std": 2.0},
        ],
        "rsi": [
            {"period": 14, "oversold": 30.0, "overbought": 70.0},
        ],
        "roc": [
            # ROCMomentum requires instrument/timeframe positionally — the
            # worker injects them; here we set only the tuning params.
            {
                "roc_period": 10,
                "roc_threshold": 0.5,
                "atr_filter_period": 14,
            },
        ],
        "session": [
            {"range_lookback": 20},
        ],
    }


def _build_strategy(
    key: str, params: dict[str, object], instrument: str, timeframe: str
) -> Strategy:
    """Instantiate a strategy from its registry key + params + combo context.

    ``instrument``/``timeframe`` are passed to every strategy; ``ROCMomentum``
    takes them positionally (its signature requires them), the rest as keyword
    args with empty-string defaults.
    """
    factory = _STRATEGY_REGISTRY[key]
    if key == "roc":
        return factory(instrument=instrument, timeframe=timeframe, **params)
    return factory(instrument=instrument, timeframe=timeframe, **params)


# ---------------------------------------------------------------------------
# Combo spec — the picklable unit of work sent to a worker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComboSpec:
    """One unit of work: a (strategy, instrument, timeframe) combination.

    All fields are plain data so the spec pickles cleanly across the process
    pool. The worker reconstructs the Store / engine / strategy from it.
    """

    strategy_key: str
    strategy_params: tuple[tuple[str, object], ...]  # frozen kwargs
    instrument: str
    timeframe: str
    # Cost-model inputs mapped from InstrumentMeta at build time (parent side):
    spread_pips: float
    slippage_pips: float
    pip_value: float
    swap_long_rate: float
    swap_short_rate: float
    commission_pips: float
    # Run context:
    db_path: str
    start_iso: str
    end_iso: str
    train_months: int
    test_months: int

    def params_dict(self) -> dict[str, object]:
        return dict(self.strategy_params)


# ---------------------------------------------------------------------------
# Worker — runs ONE combo and returns an ApprovedSetEntry or None
# ---------------------------------------------------------------------------
# Module-level (picklable) so ProcessPoolExecutor can dispatch it. Each worker
# opens its OWN Store connection (SQLite connections are not shareable across
# processes) and NEVER writes — it returns the result to the parent (INV-12).


def _run_combo(spec: ComboSpec) -> Optional[ApprovedSetEntry]:
    """Run walk-forward for one combo. Returns its ApprovedSetEntry or None.

    Opens a private read connection to the candle store, builds the swap-aware
    ``CostParams`` (mapping already done on the parent side), constructs the
    strategy + engine, and runs the validator with this timeframe's window
    sizing. Any per-combo failure is swallowed into ``None`` so one bad combo
    cannot abort the whole universe run; the parent logs counts.

    THE WORKER NEVER WRITES TO THE DB (INV-12).
    """
    # The walk-forward scan deliberately runs many short windows; the
    # low-trade-count UserWarning from compute_metrics is expected per-combo
    # noise here (the strict gate rejects those windows anyway). Silence it
    # within the worker so the universe run's logs stay readable. This does NOT
    # change which combos are approved.
    import warnings

    warnings.filterwarnings(
        "ignore",
        message=r"compute_metrics:.*statistically meaningless",
        category=UserWarning,
    )

    store = Store(spec.db_path)
    try:
        cost_params = CostParams(
            spread_pips=spec.spread_pips,
            slippage_pips=spec.slippage_pips,
            pip_value=spec.pip_value,
            swap_long_rate=spec.swap_long_rate,
            swap_short_rate=spec.swap_short_rate,
            commission_pips=spec.commission_pips,
        )
        engine = BacktestEngine(store=store, cost_params=cost_params)
        strategy = _build_strategy(
            spec.strategy_key,
            spec.params_dict(),
            spec.instrument,
            spec.timeframe,
        )
        validator = WalkForwardValidator(engine=engine, strategy=strategy)
        start = datetime.fromisoformat(spec.start_iso)
        end = datetime.fromisoformat(spec.end_iso)
        result = validator.run(
            instrument=spec.instrument,
            granularity=spec.timeframe,
            start=start,
            end=end,
            train_months=spec.train_months,
            test_months=spec.test_months,
        )
        return result.approved_set_entry
    except Exception:  # noqa: BLE001 — one bad combo must not kill the run
        return None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Cost-model mapping: InstrumentMeta → CostParams inputs (the T-03 boundary)
# ---------------------------------------------------------------------------
# Documented defaults for spread/slippage (per-instrument typical spread could
# be substituted later). pip_value is derived from pip_location: a pip is
# 10**pip_location (e.g. -4 → 0.0001 for majors, -2 → 0.01 for JPY pairs).
# long_rate/short_rate map straight onto swap_long_rate/swap_short_rate.

_DEFAULT_SPREAD_PIPS = 1.5
_DEFAULT_SLIPPAGE_PIPS = 0.5
_DEFAULT_COMMISSION_PIPS = 0.0


def _pip_value_from_location(pip_location: int) -> float:
    """A pip is ``10 ** pip_location`` (−4 → 0.0001, −2 → 0.01)."""
    return float(10.0**pip_location)


@dataclass(frozen=True)
class InstrumentCost:
    """Cost-model inputs for one instrument (mapped from InstrumentMeta)."""

    pip_value: float
    swap_long_rate: float
    swap_short_rate: float
    spread_pips: float = _DEFAULT_SPREAD_PIPS
    slippage_pips: float = _DEFAULT_SLIPPAGE_PIPS
    commission_pips: float = _DEFAULT_COMMISSION_PIPS


def _instrument_costs(store: Store) -> dict[str, InstrumentCost]:
    """Build per-instrument cost inputs from cached InstrumentMeta rows.

    Maps ``InstrumentMeta.long_rate → swap_long_rate``,
    ``short_rate → swap_short_rate``, and derives ``pip_value`` from
    ``pip_location`` (the T-03-deferred boundary mapping, owned here).
    """
    costs: dict[str, InstrumentCost] = {}
    for meta in store.load_instruments():
        costs[meta.name] = InstrumentCost(
            pip_value=_pip_value_from_location(meta.pip_location),
            swap_long_rate=meta.long_rate,
            swap_short_rate=meta.short_rate,
        )
    return costs


def _fallback_cost(instrument: str) -> InstrumentCost:
    """Cost inputs when an instrument has no cached metadata.

    pip_value falls back to the JPY-aware default: JPY pairs use 0.01, others
    0.0001. Financing rates default to 0.0 (→ swap_modelled=False for that
    combo) — honest: we did not model financing we have no data for.
    """
    pip_value = 0.01 if instrument.endswith("_JPY") else 0.0001
    return InstrumentCost(
        pip_value=pip_value, swap_long_rate=0.0, swap_short_rate=0.0
    )


# ---------------------------------------------------------------------------
# Candle fetch — parent-side, gap-aware, live only
# ---------------------------------------------------------------------------


def _fetch_candles_for_universe(
    instruments: list[str],
    timeframes: list[str],
    db_path: str,
    start: datetime,
    end: datetime,
) -> None:
    """Populate the candle store for every (instrument, timeframe) pair.

    Called by the parent process in LIVE mode (NOT ``--dry-run``) before the
    combo fan-out.  Workers only READ from the store; they must find candles
    already present — this is the step that puts them there.

    Design notes
    ------------
    * One ``OandaClient`` is created from ``Settings()`` (env-scoped, INV-09).
    * ``fetch_and_cache`` is gap-aware: re-runs are cheap (only missing rows
      trigger HTTP).  The ``start`` arg is derived from ``--history-years`` and
      always comfortably exceeds the longest per-timeframe train+test span
      (D: 24 + 6 = 30 months; default ``--history-years 3`` ≥ 36 months).
    * Fetches are sequential (one (instrument, timeframe) at a time) — avoids
      saturating the OANDA rate-limit and keeps progress log readable.
    * Parquet write is skipped (``write_parquet=False``) — the runner only needs
      the SQLite operational store; research scans are a separate concern.

    INV-03: ``start`` / ``end`` must be UTC-aware (enforced by
        ``fetch_and_cache`` itself; the caller builds them via
        ``_build_date_range`` which uses ``datetime.now(tz=timezone.utc)``).
    INV-08: never log the token or account ID.
    INV-09: one client from Settings(), env-scoped.
    """
    # Lazy imports so --dry-run NEVER constructs Settings or OandaClient.
    from config.settings import Settings
    from data.candles import fetch_and_cache
    from data.oanda_client import OandaClient

    settings = Settings()
    _log.info("Fetching candles (env=%s).", settings.env)  # INV-08: log env, not token
    client = OandaClient(settings)
    store = Store(db_path)
    try:
        pairs = [(inst, tf) for inst in sorted(instruments) for tf in sorted(timeframes)]
        for inst, tf in pairs:
            _log.info("Fetching %s/%s from %s to %s …", inst, tf,
                      start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            df = fetch_and_cache(
                client=client,
                store=store,
                instrument=inst,
                granularity=tf,
                start=start,
                end=end,
                write_parquet=False,
            )
            _log.info(
                "Fetched %s/%s: %d candles cached.", inst, tf, len(df)
            )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Universe discovery
# ---------------------------------------------------------------------------


def _discover_universe(
    instruments_arg: str,
    db_path: str,
    dry_run: bool,
) -> list[str]:
    """Return the list of instrument identifiers to run.

    * An explicit comma-separated ``--instruments`` value is used verbatim.
    * ``ALL`` (or ``all``) discovers the full FX universe. In ``--dry-run`` we
      read the cached ``instruments`` table (no HTTP); otherwise we fetch via
      ``OandaClient.list_instruments()`` and cache the result.

    INV-08: the OANDA token / account ID are never logged here.
    """
    if instruments_arg.strip().upper() != "ALL":
        return [s.strip() for s in instruments_arg.split(",") if s.strip()]

    store = Store(db_path)
    try:
        cached = [m.name for m in store.load_instruments()]
    finally:
        store.close()

    if dry_run:
        _log.info(
            "Universe (ALL, dry-run): %d instruments from cached metadata.",
            len(cached),
        )
        return sorted(cached)

    # Live discovery — import lazily so --dry-run never constructs Settings.
    from config.settings import Settings
    from data.oanda_client import OandaClient

    settings = Settings()
    # INV-08: log only non-secret config.
    _log.info("Discovering universe via OANDA (env=%s).", settings.env)
    client = OandaClient(settings)
    metas = client.list_instruments()
    store = Store(db_path)
    try:
        store.upsert_instruments(metas)
    finally:
        store.close()
    names = sorted(m.name for m in metas)
    _log.info("Universe (ALL): %d FX instruments discovered.", len(names))
    return names


# ---------------------------------------------------------------------------
# Combo list construction (parent side — deterministic order)
# ---------------------------------------------------------------------------


def _resolve_strategy_keys(strategies_arg: str) -> list[str]:
    if strategies_arg.strip().lower() == "all":
        return list(_STRATEGY_REGISTRY.keys())
    keys = [s.strip().lower() for s in strategies_arg.split(",") if s.strip()]
    unknown = [k for k in keys if k not in _STRATEGY_REGISTRY]
    if unknown:
        raise ValueError(
            f"Unknown strategy key(s): {unknown}. "
            f"Valid: {sorted(_STRATEGY_REGISTRY)}"
        )
    return keys


def _build_combos(
    instruments: list[str],
    timeframes: list[str],
    strategy_keys: list[str],
    instrument_costs: dict[str, InstrumentCost],
    db_path: str,
    start: datetime,
    end: datetime,
) -> list[ComboSpec]:
    """Build the full, deterministically-ordered combo list.

    Order is fixed (sorted instrument, timeframe, strategy key, param index) so
    the run is reproducible regardless of worker count. Each timeframe pulls
    its own train/test window sizing from ``WINDOW_CONFIG``.
    """
    grid = _default_param_grid()
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    combos: list[ComboSpec] = []

    for instrument in sorted(instruments):
        cost = instrument_costs.get(instrument) or _fallback_cost(instrument)
        for timeframe in timeframes:
            wc = WINDOW_CONFIG[timeframe]
            for strategy_key in strategy_keys:
                for params in grid[strategy_key]:
                    combos.append(
                        ComboSpec(
                            strategy_key=strategy_key,
                            strategy_params=tuple(sorted(params.items())),
                            instrument=instrument,
                            timeframe=timeframe,
                            spread_pips=cost.spread_pips,
                            slippage_pips=cost.slippage_pips,
                            pip_value=cost.pip_value,
                            swap_long_rate=cost.swap_long_rate,
                            swap_short_rate=cost.swap_short_rate,
                            commission_pips=cost.commission_pips,
                            db_path=db_path,
                            start_iso=start_iso,
                            end_iso=end_iso,
                            train_months=wc.train_months,
                            test_months=wc.test_months,
                        )
                    )
    return combos


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_approved_table(entries: list[ApprovedSetEntry]) -> None:
    """Print the approved-set table to stdout."""
    if not entries:
        print(
            "No (strategy, pair, timeframe) combination passed walk-forward "
            "validation. Approved set is empty (a valid result)."
        )
        return

    header = (
        f"{'Strategy':<40} {'Instrument':<10} {'Gran':<5} "
        f"{'OOS Sharpe':>12} {'OOS Trades':>11} {'Swap':<6}"
    )
    separator = "-" * len(header)
    print()
    print("=== Approved-Set Table ===")
    print(separator)
    print(header)
    print(separator)
    for e in entries:
        print(
            f"{e.strategy_name:<40} {e.instrument:<10} {e.granularity:<5} "
            f"{e.oos_sharpe_mean:>12.4f} {e.oos_trade_count_total:>11} "
            f"{str(e.swap_modelled):<6}"
        )
    print(separator)
    print(f"Total approved entries: {len(entries)}")
    print()


def _sort_key(e: ApprovedSetEntry) -> tuple[str, str, str]:
    """Deterministic ordering for the approved set (worker-count independent)."""
    return (e.strategy_name, e.instrument, e.granularity)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fathom",
        description="Fathom CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser(
        "backtest",
        help="Run full-universe walk-forward validation; persist approved-set.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    bt.add_argument(
        "--instruments",
        default="ALL",
        help="ALL to discover the full FX universe, or a comma-separated list.",
    )
    bt.add_argument(
        "--timeframes",
        default="H1,H4,D",
        help="Comma-separated timeframes (each must have a window config).",
    )
    bt.add_argument(
        "--strategies",
        default="all",
        help=(
            "'all' or a comma-separated subset of: "
            + ",".join(_STRATEGY_REGISTRY.keys())
        ),
    )
    bt.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="ProcessPoolExecutor worker count (results are worker-independent).",
    )
    bt.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite candle + approved_set store.",
    )
    bt.add_argument(
        "--history-years",
        type=int,
        default=_DEFAULT_HISTORY_YEARS,
        metavar="N",
        help="Years of history to scan (must exceed the longest train+test span).",
    )
    bt.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Cache-only: never fetch from OANDA and never construct Settings; "
            "run walk-forward against whatever candles are already cached."
        ),
    )

    # ---- scan ---------------------------------------------------------------
    sc = sub.add_parser(
        "scan",
        help=(
            "Refresh candles, rank approved strategies → PortfolioLimiter, "
            "persist watchlist, print Candidate[] JSON."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sc.add_argument(
        "--instruments",
        default="ALL",
        help="ALL to discover from cache, or a comma-separated list.",
    )
    sc.add_argument(
        "--timeframes",
        default="H1,H4,D",
        help="Comma-separated timeframes for the candle refresh.",
    )
    sc.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store (candles + approved_set + watchlist).",
    )
    sc.add_argument(
        "--history-years",
        type=int,
        default=_DEFAULT_HISTORY_YEARS,
        metavar="N",
        help="Years of history to fetch/cache (passed to fetch_and_cache).",
    )
    sc.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Cache-only: skip live candle fetch (mirror backtest --dry-run). "
            "Run ranker against whatever is already cached."
        ),
    )

    # ---- watchlist ----------------------------------------------------------
    wl = sub.add_parser(
        "watchlist",
        help="Print the latest persisted watchlist as Candidate[] JSON (INV-13).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    wl.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store.",
    )

    # ---- chart --------------------------------------------------------------
    ch = sub.add_parser(
        "chart",
        help="Render a candidate's chart PNG and print its path.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ch.add_argument(
        "instrument",
        help="OANDA instrument identifier, e.g. EUR_USD.",
    )
    ch.add_argument(
        "--timeframe",
        default="H1",
        help="Granularity of the candle data to plot.",
    )
    ch.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store (candles + watchlist).",
    )
    ch.add_argument(
        "--out-dir",
        default="charts",
        help="Directory in which to save the PNG.",
    )
    ch.add_argument(
        "--history-years",
        type=int,
        default=1,
        metavar="N",
        help="Years of candle history to load for the chart window.",
    )

    return parser


def _build_date_range(history_years: int) -> tuple[datetime, datetime]:
    end = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = end - timedelta(days=history_years * 365)
    return start, end


# ---------------------------------------------------------------------------
# backtest command
# ---------------------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> int:
    """Execute the ``fathom backtest`` command. Returns the process exit code."""
    run_dt = datetime.now(tz=timezone.utc)
    _log.info("fathom backtest started at %s", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    timeframes = [s.strip() for s in args.timeframes.split(",") if s.strip()]
    for tf in timeframes:
        if tf not in WINDOW_CONFIG:
            _log.error(
                "Timeframe %r has no window config. Known: %s",
                tf,
                sorted(WINDOW_CONFIG),
            )
            return 2

    try:
        strategy_keys = _resolve_strategy_keys(args.strategies)
    except ValueError as exc:
        _log.error("%s", exc)
        return 2

    db_path: str = args.db_path
    dry_run: bool = args.dry_run

    # Universe discovery (cache-only under --dry-run; no HTTP, no Settings).
    try:
        instruments = _discover_universe(args.instruments, db_path, dry_run)
    except Exception as exc:  # noqa: BLE001
        _log.error("Universe discovery failed: %s", exc)
        return 1

    if not instruments:
        _log.warning(
            "No instruments to run (empty universe). Approved set is empty."
        )
        _print_approved_table([])
        return 0

    # Per-instrument cost inputs from cached metadata (T-03 boundary mapping).
    store = Store(db_path)
    try:
        instrument_costs = _instrument_costs(store)
    finally:
        store.close()

    start, end = _build_date_range(args.history_years)
    _log.info(
        "Range %s → %s | instruments=%d timeframes=%s strategies=%s workers=%d",
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        len(instruments),
        timeframes,
        strategy_keys,
        args.workers,
    )

    # ---- Candle fetch (LIVE mode only; parent-side, gap-aware). -------------
    # In LIVE mode the store starts empty on the first run; workers must find
    # candles already present, so the parent fetches them here before dispatch.
    # Under --dry-run this step is SKIPPED entirely — the run proceeds against
    # whatever candles are already cached (no HTTP, no Settings construction).
    if not dry_run:
        try:
            _fetch_candles_for_universe(
                instruments=instruments,
                timeframes=timeframes,
                db_path=db_path,
                start=start,
                end=end,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("Candle fetch failed: %s", exc)
            return 1

    combos = _build_combos(
        instruments=instruments,
        timeframes=timeframes,
        strategy_keys=strategy_keys,
        instrument_costs=instrument_costs,
        db_path=db_path,
        start=start,
        end=end,
    )
    _log.info("Built %d (strategy, pair, timeframe) combos.", len(combos))

    # ---- Fan out over the process pool. Workers return entries; the PARENT
    # ---- collects ALL of them before any DB write (INV-12). --------------
    results: list[Optional[ApprovedSetEntry]] = []
    workers = max(1, args.workers)
    if workers == 1:
        # Serial path — identical results, simpler stack traces, and used by
        # the determinism test as the reference.
        results = [_run_combo(c) for c in combos]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            # map preserves input order → deterministic collection.
            results = list(pool.map(_run_combo, combos))

    approved = [r for r in results if r is not None]
    # Parent-side deterministic sort: the persisted table is byte-identical
    # regardless of --workers.
    approved.sort(key=_sort_key)
    _log.info(
        "Walk-forward complete: %d/%d combos approved.",
        len(approved),
        len(combos),
    )

    # ---- Single-writer, single-transaction persist (INV-10 + INV-12). -----
    store = Store(db_path)
    try:
        written = store.write_approved_set(approved, run_timestamp=run_dt)
    finally:
        store.close()
    _log.info(
        "Persisted %d approved_set rows (run_timestamp=%s).",
        written,
        run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    _print_approved_table(approved)

    _log.info(
        "fathom backtest finished at %s. Approved entries: %d.",
        _utc_now_rfc3339(),
        len(approved),
    )
    # Empty approved set is a valid result — exit 0 (INV-10).
    return 0


# ---------------------------------------------------------------------------
# scan command
# ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    """Execute the ``fathom scan`` command.

    Refreshes candles (unless ``--dry-run``), runs ``Ranker`` →
    ``PortfolioLimiter``, persists the ranked ``Candidate`` list to the
    ``watchlist`` SQLite table, and prints the list as ``Candidate[]`` JSON to
    stdout.

    Empty approved-set → empty watchlist, clear message, **exit 0** (INV-10).

    INV-01: no order placement — candidates only.
    INV-03: all timestamps UTC.
    INV-08: token/account never logged.
    """
    run_dt = datetime.now(tz=timezone.utc)
    _log.info("fathom scan started at %s", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    db_path: str = args.db_path
    dry_run: bool = args.dry_run
    timeframes = [s.strip() for s in args.timeframes.split(",") if s.strip()]

    # ---- Optional candle refresh (LIVE mode only; mirrors backtest). --------
    if not dry_run:
        instruments_to_fetch: list[str]
        if args.instruments.strip().upper() == "ALL":
            # In LIVE mode for ALL, discover via the OANDA API and cache.
            try:
                instruments_to_fetch = _discover_universe(
                    args.instruments, db_path, dry_run=False
                )
            except Exception as exc:  # noqa: BLE001
                _log.error("Universe discovery failed: %s", exc)
                return 1
        else:
            instruments_to_fetch = [
                s.strip() for s in args.instruments.split(",") if s.strip()
            ]
        if instruments_to_fetch:
            start_fetch, end_fetch = _build_date_range(args.history_years)
            try:
                _fetch_candles_for_universe(
                    instruments=instruments_to_fetch,
                    timeframes=timeframes,
                    db_path=db_path,
                    start=start_fetch,
                    end=end_fetch,
                )
            except Exception as exc:  # noqa: BLE001
                _log.error("Candle fetch failed: %s", exc)
                return 1

    # ---- Build Ranker + PortfolioLimiter (lazy imports — no HTTP in tests). -
    from data.calendar import FairEconomyCalendar
    from signals.portfolio import PortfolioLimiter
    from signals.ranker import Ranker

    store = Store(db_path)
    try:
        try:
            # FairEconomyCalendar.upcoming_events returns list[CalendarEvent] while
            # Ranker's _CalendarLike Protocol declares list[object].  Structurally
            # compatible (CalendarEvent IS an object) but mypy's strict return-type
            # covariance check rejects it — suppress with ignore on this line only.
            ranker = Ranker(store=store, calendar=FairEconomyCalendar(db_path=db_path))  # type: ignore[arg-type]
            limiter = PortfolioLimiter(store=store)

            _log.info(
                "Running Ranker at %s …", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            ranked = ranker.rank(now=run_dt)
            candidates = limiter.apply(ranked)

            _log.info(
                "Ranker produced %d candidate(s); %d after portfolio limits.",
                len(ranked),
                len(candidates),
            )

            # Persist to watchlist table (single transaction, mirrors approved_set).
            written = store.write_watchlist(candidates, run_timestamp=run_dt)
            _log.info(
                "Persisted %d watchlist row(s) (run_timestamp=%s).",
                written,
                run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("Ranker/portfolio step failed: %s", exc)
            return 1
    finally:
        store.close()

    if not candidates:
        print(
            "Scan complete: approved-set is empty or no strategies produced "
            "signals. Watchlist is empty (a valid result — INV-10)."
        )
        _log.info("fathom scan finished at %s. Watchlist is empty.", _utc_now_rfc3339())
        return 0

    # Print Candidate[] JSON to stdout (the Hermes-facing wire contract, INV-13).
    output = json.dumps(
        [c.model_dump() for c in candidates],
        indent=2,
        default=str,
    )
    print(output)

    _log.info(
        "fathom scan finished at %s. Candidates: %d.",
        _utc_now_rfc3339(),
        len(candidates),
    )
    return 0


# ---------------------------------------------------------------------------
# watchlist command
# ---------------------------------------------------------------------------


def cmd_watchlist(args: argparse.Namespace) -> int:
    """Execute the ``fathom watchlist`` command.

    Reads the **latest** persisted watchlist from the ``watchlist`` SQLite
    table and emits it as ``Candidate[]`` JSON (INV-13 wire shape).

    No live HTTP — pure DB read.  Empty watchlist → prints empty JSON array
    and exits 0 (INV-10).
    """
    _log.info("fathom watchlist started at %s", _utc_now_rfc3339())

    db_path: str = args.db_path
    store = Store(db_path)
    try:
        rows = store.load_watchlist()
    finally:
        store.close()

    if not rows:
        _log.info("No watchlist rows found; emitting empty JSON array.")
        print("[]")
        return 0

    # Reconstruct Candidate objects from raw dicts for full validation, then
    # serialise — this ensures the JSON output matches the INV-13 shape exactly.
    from signals.ranker import Candidate

    candidates = [Candidate(**row) for row in rows]
    output = json.dumps(
        [c.model_dump() for c in candidates],
        indent=2,
        default=str,
    )
    print(output)

    _log.info(
        "fathom watchlist finished at %s. Emitted %d candidate(s).",
        _utc_now_rfc3339(),
        len(candidates),
    )
    return 0


# ---------------------------------------------------------------------------
# chart command
# ---------------------------------------------------------------------------


def cmd_chart(args: argparse.Namespace) -> int:
    """Execute the ``fathom chart <instrument>`` command.

    Reads the latest watchlist entry for ``<instrument>`` (optionally filtered
    by ``--timeframe``), loads its candles, renders a PNG via
    ``render_candidate_chart``, and prints the PNG path to stdout.

    No live HTTP — reads from the SQLite store (candles + watchlist).
    """
    _log.info("fathom chart started at %s", _utc_now_rfc3339())

    instrument: str = args.instrument
    timeframe: str = args.timeframe
    db_path: str = args.db_path
    out_dir: str = args.out_dir

    from signals.charts import render_candidate_chart
    from signals.ranker import Candidate

    store = Store(db_path)
    try:
        rows = store.load_watchlist()
    finally:
        store.close()

    # Find the matching candidate from the latest watchlist run.
    matching = [
        r for r in rows
        if r["instrument"] == instrument and r["timeframe"] == timeframe
    ]
    if not matching:
        _log.error(
            "No watchlist entry found for instrument=%r timeframe=%r "
            "(run 'fathom scan' first, or check --timeframe).",
            instrument,
            timeframe,
        )
        print(
            f"No watchlist entry for {instrument}/{timeframe}. "
            "Run 'fathom scan' first.",
            file=sys.stderr,
        )
        return 1

    # Use the first (highest-ranked) matching candidate.
    candidate = Candidate(**matching[0])

    # Load candles for the chart window.
    start_chart, end_chart = _build_date_range(args.history_years)
    store2 = Store(db_path)
    try:
        candles = store2.load_candles(
            instrument=instrument,
            granularity=timeframe,
            start=start_chart,
            end=end_chart,
        )
    finally:
        store2.close()

    if candles.empty:
        _log.error(
            "No candles in store for %s/%s. "
            "Run 'fathom scan' (without --dry-run) first.",
            instrument,
            timeframe,
        )
        print(
            f"No candles in store for {instrument}/{timeframe}.",
            file=sys.stderr,
        )
        return 1

    out_path = render_candidate_chart(
        candidate=candidate,
        candles=candles,
        out_dir=out_dir,
    )
    # Print the PNG path to stdout — the caller/Hermes reads it.
    print(out_path)

    _log.info(
        "fathom chart finished at %s. PNG: %s",
        _utc_now_rfc3339(),
        out_path,
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    _configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "backtest":
        return cmd_backtest(args)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "watchlist":
        return cmd_watchlist(args)
    if args.command == "chart":
        return cmd_chart(args)
    parser.error(f"Unknown command: {args.command}")
    return 2  # unreachable — parser.error exits


if __name__ == "__main__":
    sys.exit(main())
