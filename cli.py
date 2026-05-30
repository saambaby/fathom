"""Fathom CLI — multi-phase operator entrypoint.

Phase 1A: ``fathom backtest`` (P1A-T-08)
-----------------------------------------
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

Phase 3: ``fathom execute`` / ``fathom positions`` / ``fathom reconcile`` (P3-T-10)
------------------------------------------------------------------------------------
The canonical **INV-01 enforcement point**: ``fathom execute`` is the
human-run CLI command that turns an approved watchlist candidate into a trade.
It is NEVER a Hermes tool — execution authority belongs to the operator.

Gate ordering (pretrade → sizing → limits → submit):
1. Load candidate from the latest persisted watchlist (INV-13).
2. Fresh reconcile → refresh account_state + positions (AMBIGUOUS-03).
3. Pretrade check → ``block`` aborts (exit ≠ 0, reason).
4. Sizing (risk_fraction=0.0025, INV-05 cap) → reject aborts.
5. Limits / kill switch → reject aborts with kill-switch status.
6. Build bracket + submit order → print Fill.

``--dry-run`` runs steps 1–5 and prints the would-be order WITHOUT any v20
submission.  ``--yes`` skips the interactive confirm before a real submit.

``fathom positions`` and ``fathom reconcile`` are read-only operator helpers.

INV-01: execute/positions/reconcile are NEVER registered as Hermes tools.
INV-07: practice endpoint only (INV-09 one-code-path).
INV-03: all timestamps UTC.
INV-08: no secret logged.
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
# Phase 3 execution imports (P3-T-10) — module-level for testability.
# INV-01: these are imported for the operator CLI gate, NEVER registered as
# Hermes tools.  The Hermes allow-list (scan/watchlist/chart) is unchanged.
# ---------------------------------------------------------------------------
# Lazy fallback: these may not be importable in minimal test envs without the
# full stack installed.  They are only used inside the Phase 3 command
# functions (cmd_execute, cmd_positions, cmd_reconcile), never at import time.
try:
    from config.settings import Settings
    from data.oanda_client import OandaClient
    from execution.live_gate import (
        LiveTradingBlocked,
        assert_live_allowed,
        effective_risk_fraction,
    )
    from execution.models import build_bracket
    from execution.orders import OrderRejected, submit_order
    from execution.preflight import run_preflight
    from execution.reconcile import reconcile
    from hermes_integration.pretrade_check import pretrade_check
    from risk.limits import LimitsConfig, check_limits, kill_switch_status
    from risk.sizing import size_position
except ImportError:  # pragma: no cover
    # During module-level import in environments where execution deps are
    # absent, defer to runtime so the non-execution subcommands still work.
    pass

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

    # ---- execute ------------------------------------------------------------
    # INV-01: this subcommand is NEVER registered as a Hermes tool.
    # The canonical human-operator execution gate (P3-T-10).
    ex = sub.add_parser(
        "execute",
        help=(
            "Run a watchlist candidate through the full Phase 3 gate "
            "(pretrade → sizing → limits → submit). INV-01: operator-only, "
            "never a Hermes tool."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ex.add_argument(
        "candidate_ref",
        help=(
            "Candidate reference: instrument:timeframe:strategy_name, "
            "e.g. EUR_USD:H1:macrossover_10_50_eur_usd_h1. "
            "Must be present on the latest persisted watchlist (INV-13)."
        ),
    )
    ex.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store.",
    )
    ex.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Run gate steps 1–5 and print the would-be order WITHOUT "
            "submitting to OANDA. Safe rehearsal."
        ),
    )
    ex.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip the interactive confirm prompt before a real submit.",
    )

    # ---- positions ----------------------------------------------------------
    # INV-01: read-only operator helper; never a Hermes tool.
    pos = sub.add_parser(
        "positions",
        help=(
            "Print open Position[] JSON from the store. "
            "INV-01: operator-only, never a Hermes tool."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    pos.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store.",
    )

    # ---- reconcile ----------------------------------------------------------
    # INV-01: operator-initiated broker-truth sync; never a Hermes tool.
    rec = sub.add_parser(
        "reconcile",
        help=(
            "Run one reconciliation pass against the OANDA broker and print "
            "the ReconcileReport. INV-01: operator-only, never a Hermes tool."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    rec.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store.",
    )

    # ---- preflight ----------------------------------------------------------
    # P5-T-03: read-only go/no-go readiness check before a live cutover.
    # INV-07: never places orders; requires --attest-track-record to go GO.
    pf = sub.add_parser(
        "preflight",
        help=(
            "Read-only go/no-go readiness check before a live cutover. "
            "Verifies account reachability, kill-switch state, bracket/INV-04 "
            "contract, env/flag/token consistency, and requires explicit "
            "track-record attestation (INV-07).  Exits 0 on GO, non-zero on "
            "NO-GO.  Places no orders and writes no state."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    pf.add_argument(
        "--db-path",
        default="data/fathom.db",
        help="Path to the SQLite store.",
    )
    pf.add_argument(
        "--attest-track-record",
        action="store_true",
        default=False,
        help=(
            "Operator attestation: assert the demo track record satisfies INV-07 "
            "(positive, stable edge on demo before live cutover).  Required for GO."
        ),
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

    **Thin argparse adapter** over ``signals.scan.run_scan``.  All scan
    logic (candle refresh → Ranker → PortfolioLimiter → persist) lives in
    the order-free ``run_scan``; this function maps ``args.*`` → kwargs,
    delegates, then does the stdout JSON printing and exit-code conversion.

    Universe resolution (ALL, live mode)
    -------------------------------------
    When ``--instruments ALL`` is given and ``--dry-run`` is False, this
    adapter calls the existing ``_discover_universe`` helper (which triggers a
    live OANDA ``list_instruments()`` call and caches the result) and passes
    the resolved explicit instrument list into ``run_scan``.  This restores the
    original ``cmd_scan`` behaviour: ``fathom scan --instruments ALL`` (live)
    discovers the full tradeable universe live, not just from cache.

    For ``--dry-run`` and for explicit instrument lists, ``args.instruments``
    is passed through to ``run_scan`` unchanged — ``run_scan`` handles both
    the comma-separated and cache-only ALL cases itself.

    ``run_scan`` remains order-free and cache-only for its own ALL discovery
    (correct for the admin panel, which does not need live discovery).  This
    adapter is the only place live discovery fires, keeping ``signals.scan``
    import-clean.

    INV-01: this adapter imports only ``signals.scan`` — the order-free path.
        The order/execution imports in this module (the Phase 3 block) are
        isolated to the execute/positions/reconcile command functions; they do
        NOT affect ``cmd_scan``.
    INV-03: ``run_scan`` handles UTC internally.
    INV-08: token/account never logged.
    INV-10: empty approved-set → empty watchlist, clear message, exit 0.
    INV-13: ``run_scan`` returns ``Candidate[]`` (frozen wire contract).
    """
    _log.info("fathom scan started at %s", _utc_now_rfc3339())

    from signals.scan import run_scan

    # --- Universe resolution: live ALL-discovery in the CLI adapter. ----------
    # When the operator runs `fathom scan --instruments ALL` (live, not
    # --dry-run), we call _discover_universe here (which issues a live
    # list_instruments() call and caches the result) and pass the resolved
    # explicit list to run_scan.  run_scan stays cache-only / order-free.
    # For --dry-run or an explicit list, pass args.instruments through as-is.
    instruments_for_scan: str = args.instruments
    dry_run: bool = args.dry_run
    if args.instruments.strip().upper() == "ALL" and not dry_run:
        try:
            resolved = _discover_universe(
                instruments_arg=args.instruments,
                db_path=args.db_path,
                dry_run=False,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("Universe discovery failed: %s", exc)
            return 1
        # Pass the resolved list as a comma-joined string so run_scan treats it
        # as an explicit (non-ALL) list and skips its own cache discovery.
        instruments_for_scan = ",".join(resolved)
        _log.info(
            "cmd_scan: resolved ALL → %d instruments via live discovery.",
            len(resolved),
        )

    try:
        candidates = run_scan(
            db_path=args.db_path,
            instruments=instruments_for_scan,
            timeframes=args.timeframes,
            history_years=args.history_years,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("Scan failed: %s", exc)
        return 1

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
# Phase 3 — execute, positions, reconcile commands (P3-T-10)
# ---------------------------------------------------------------------------
# INV-01: these three subcommands are operator-only CLI commands.  They are
# NEVER registered as Hermes tools.  The Phase 2 daily.md allow-list is
# scan/watchlist/chart and remains unchanged.
#
# Gate ordering for ``execute`` (exactly as specced):
#   1. Load candidate from latest watchlist (INV-13).
#   2. Fresh reconcile → refresh account_state + open positions (AMBIGUOUS-03).
#   3. Pretrade check  → ``block`` aborts (exit ≠ 0).
#   4. Sizing (risk_fraction=0.0025, INV-05 cap) → reject (units=0) aborts.
#   5. Limits / kill switch → reject aborts with kill-switch status.
#   6. ``--dry-run``? print would-be order, return 0.  Otherwise confirm +
#      build_bracket + submit_order → print Fill.


def _load_candidate(
    candidate_ref: str,
    db_path: str,
) -> "tuple[Optional[object], Optional[tuple[str, int]]]":
    """Load a Candidate from the latest persisted watchlist.

    Returns ``(candidate, None)`` on success or ``(None, (error_msg, exit_code))``
    on failure.  Never executes off-watchlist input (INV-13, first AC).

    The ref format is ``instrument:timeframe:strategy_name`` (DRIFT-04).
    """
    from signals.ranker import Candidate  # lazy — no HTTP in tests

    parts = candidate_ref.split(":", 2)
    if len(parts) != 3:
        return (
            None,
            (
                f"Invalid candidate ref {candidate_ref!r}: expected "
                "instrument:timeframe:strategy_name "
                "(e.g. EUR_USD:H1:macrossover_10_50).",
                2,
            ),
        )
    instrument, timeframe, strategy_name = parts

    store = Store(db_path)
    try:
        rows = store.load_watchlist()  # latest run, run_timestamp=None
    finally:
        store.close()

    if not rows:
        return (
            None,
            ("No watchlist found in store. Run 'fathom scan' first.", 1),
        )

    matches = [
        r for r in rows
        if (
            r["instrument"] == instrument
            and r["timeframe"] == timeframe
            and r["strategy_name"] == strategy_name
        )
    ]
    if not matches:
        all_refs = [
            f"{r['instrument']}:{r['timeframe']}:{r['strategy_name']}"
            for r in rows
        ]
        return (
            None,
            (
                f"Candidate {candidate_ref!r} not found on the latest "
                f"watchlist. Available refs: {all_refs}",
                1,
            ),
        )

    candidate = Candidate(**matches[0])
    return (candidate, None)


def cmd_execute(args: argparse.Namespace) -> int:
    """Execute the ``fathom execute <candidate-ref>`` command.

    Phase 3 gate (P3-T-10) — the canonical INV-01 enforcement point.
    This command is NEVER a Hermes tool.

    Gate ordering: load → reconcile → pretrade → sizing → limits → submit.

    Returns the process exit code (0 = success / dry-run success,
    non-zero = abort at any gate stage).
    """
    run_dt = datetime.now(tz=timezone.utc)
    _log.info(
        "fathom execute started at %s (dry_run=%s, yes=%s, candidate_ref=%r)",
        run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        args.dry_run,
        args.yes,
        args.candidate_ref,
    )

    db_path: str = args.db_path
    dry_run: bool = args.dry_run
    skip_confirm: bool = args.yes

    # ------------------------------------------------------------------
    # Step 1: Load candidate from latest persisted watchlist (INV-13).
    # ------------------------------------------------------------------
    candidate_or_none, err = _load_candidate(args.candidate_ref, db_path)
    if err is not None:
        err_msg, exit_code = err
        _log.error("execute: candidate resolution failed: %s", err_msg)
        print(f"ERROR: {err_msg}", file=sys.stderr)
        return exit_code

    from signals.ranker import Candidate as _Candidate

    if not isinstance(candidate_or_none, _Candidate):
        # Should never happen: _load_candidate returns (Candidate, None) on success.
        print("ERROR: unexpected internal error in candidate resolution.", file=sys.stderr)
        return 1

    candidate = candidate_or_none

    _log.info(
        "execute: loaded candidate %s/%s/%s from watchlist.",
        candidate.instrument,
        candidate.timeframe,
        candidate.strategy_name,
    )

    # ------------------------------------------------------------------
    # Step 2: Fresh reconcile BEFORE limits (AMBIGUOUS-03).
    # Refresh account_state (day_pl, start_of_day_equity) and open
    # positions from the broker so the kill switch reads current data.
    # ------------------------------------------------------------------
    settings = Settings()
    _log.info("execute: connecting to OANDA (env=%s).", settings.env)  # INV-08: no token
    client = OandaClient(settings)
    store = Store(db_path)
    try:
        recon_report = reconcile(client=client, store=store, now=run_dt)
    except Exception as exc:  # noqa: BLE001
        _log.error("execute: reconcile failed: %s", exc)
        print(f"ERROR: reconciliation failed: {exc}", file=sys.stderr)
        store.close()
        return 1
    _log.info(
        "execute: reconcile complete — adopted=%d closed=%d matched=%d "
        "day_pl=%.4f start_of_day_equity=%.4f",
        len(recon_report.adopted),
        len(recon_report.closed),
        len(recon_report.matched),
        recon_report.day_pl,
        recon_report.start_of_day_equity,
    )

    # Read the freshly-reconciled state.
    account_state = store.load_account_state()
    open_positions = store.load_open_positions()
    store.close()

    if account_state is None:
        _log.error("execute: no account_state after reconcile — cannot continue.")
        print(
            "ERROR: account_state not available after reconcile.",
            file=sys.stderr,
        )
        return 1

    day_pl: float = float(account_state["day_pl"])  # type: ignore[arg-type]
    start_of_day_equity: float = float(account_state["start_of_day_equity"])  # type: ignore[arg-type]
    # NAV from the reconcile broker fetch is the current equity for sizing.
    equity: float = start_of_day_equity + day_pl  # nav = snapshot + today's delta

    # ------------------------------------------------------------------
    # Step 2.5: LIVE-TRADING GATE (P5-T-02) — defense-in-depth, real money.
    # Only runs on ENV=live; on demo this whole block is skipped, so the demo
    # path is byte-identical to Phase 3.  Four independent gates, all required:
    #   env=="live" AND live_trading_enabled AND preflight.go AND typed confirm.
    # Bias is always to REFUSE: any failure (incl. a preflight EXCEPTION, B-1)
    # exits non-zero with no order placed and is never interpreted as GO.
    # The typed account-id confirmation is NOT guarded by --yes (N-3): live
    # always requires it; the [y/N] confirm below remains demo-only.
    # ------------------------------------------------------------------
    if settings.env == "live":
        # (a) run_preflight — any exception → refuse (never GO).  B-1.
        store_pf = Store(db_path)
        try:
            preflight_report = run_preflight(
                settings=settings,
                store=store_pf,
                client=client,
                attested=True,  # operator runs `fathom preflight` separately;
                # the live confirm + the gate are the deliberate operator act here.
            )
        except Exception as exc:  # noqa: BLE001  — fail closed.
            _log.error("execute: live preflight raised, refusing: %s", exc)
            print(
                f"LIVE REFUSED: preflight failed unexpectedly ({exc}). "
                "No order placed.",
                file=sys.stderr,
            )
            store_pf.close()
            return 1
        finally:
            store_pf.close()

        # (b) Typed confirmation — operator types the account id.  NOT --yes
        #     bypassable (N-3).  Anything but an exact match → confirmed=False.
        expected_account = settings.oanda_account_id
        print(
            "\nLIVE order requested (ENV=live).  Type the OANDA account id "
            f"to confirm (account: {expected_account}):",
        )
        try:
            typed = input("Account id: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nLIVE REFUSED: no confirmation provided.", file=sys.stderr)
            return 1
        confirmed = bool(expected_account) and typed == expected_account

        # (c) The pure four-gate assertion.  LiveTradingBlocked → refuse.
        try:
            assert_live_allowed(
                settings=settings,
                preflight_report=preflight_report,
                confirmed=confirmed,
            )
        except LiveTradingBlocked as exc:
            _log.warning("execute: live gate blocked: %s", exc)
            print(f"LIVE REFUSED: {exc}", file=sys.stderr)
            return 1
        _log.info("execute: live gate PASSED — all four gates satisfied.")

    # ------------------------------------------------------------------
    # Step 3: Pretrade check.
    # ``block`` aborts with a clear reason and non-zero exit.
    # ------------------------------------------------------------------
    verdict = pretrade_check(candidate)
    if verdict.decision == "block":
        _log.warning(
            "execute: pretrade-check blocked: %s", verdict.reason
        )
        print(
            f"BLOCKED by pretrade check: {verdict.reason}",
            file=sys.stderr,
        )
        return 1

    _log.info("execute: pretrade check → proceed (%s).", verdict.reason)

    # ------------------------------------------------------------------
    # Step 4: Sizing (risk_fraction = DEFAULT_RISK_FRACTION = 0.0025,
    # the INV-05 cap — never above it).
    # ``units == 0`` rejects with a reason; abort.
    # ------------------------------------------------------------------
    # Resolve instrument metadata from the cached store for min_trade_size
    # and display_precision.
    store2 = Store(db_path)
    try:
        instrument_metas = {m.name: m for m in store2.load_instruments()}
    finally:
        store2.close()

    inst_meta = instrument_metas.get(candidate.instrument)
    if inst_meta is None:
        _log.error(
            "execute: no InstrumentMeta cached for %s — run 'fathom backtest' "
            "or 'fathom scan' (live) first.",
            candidate.instrument,
        )
        print(
            f"ERROR: no instrument metadata for {candidate.instrument}. "
            "Run 'fathom scan' (live) first.",
            file=sys.stderr,
        )
        return 1

    # quote_to_account_rate: for pairs where the quote currency is USD
    # (e.g. EUR_USD) rate = 1.0; for others (e.g. USD_JPY) rate = 1/mid.
    # Simple heuristic: if instrument ends in "_USD", rate = 1.0; otherwise
    # load the latest close_mid from the candle store as the proxy rate.
    # If unavailable, fall back to 1.0 with a warning (safe — sizing still runs,
    # may reject on the minimum-trade-size floor if the rate is very wrong).
    rate: float = 1.0
    if not candidate.instrument.endswith("_USD"):
        store3 = Store(db_path)
        try:
            end_dt = datetime.now(tz=timezone.utc)
            start_dt = end_dt - timedelta(days=3)
            candles = store3.load_candles(
                instrument=candidate.instrument,
                granularity=candidate.timeframe,
                start=start_dt,
                end=end_dt,
            )
            if not candles.empty and "close_mid" in candles.columns:
                last_mid = float(candles["close_mid"].iloc[-1])
                if last_mid > 0:
                    rate = 1.0 / last_mid
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "execute: could not derive quote→account rate for %s "
                "(using 1.0 fallback): %s",
                candidate.instrument,
                exc,
            )
        finally:
            store3.close()

    sizing_result = size_position(
        candidate,
        equity,
        instrument_meta=inst_meta,
        rate=rate,
        # B-2 / INV-09: the env-aware fraction is selected ONLY here (the gate
        # layer).  Demo → DEFAULT_RISK_FRACTION (0.0025, numerically unchanged);
        # live → live_risk_fraction (validated ≤ 0.0025).  size_position itself
        # is unchanged — the same mechanics run demo and live.
        risk_fraction=effective_risk_fraction(settings),
    )
    if sizing_result.units == 0:
        _log.warning("execute: sizing rejected: %s", sizing_result.reason)
        print(
            f"REJECTED by sizing: {sizing_result.reason}",
            file=sys.stderr,
        )
        return 1

    _log.info(
        "execute: sizing approved — units=%d risk_amount=%.6g.",
        sizing_result.units,
        sizing_result.risk_amount,
    )

    # ------------------------------------------------------------------
    # Build the bracketed Order (INV-04, INV-15).
    # build_bracket is called here — before the limits check — so the
    # fully-formed Order (with correct bracket prices) is passed to
    # check_limits.  This is still within step 5: the Order is NOT
    # submitted yet; it is used as the limits-gate input.
    # ------------------------------------------------------------------
    order = build_bracket(
        candidate,
        sizing_result.units,
        execution_date=run_dt,
        precision=inst_meta.display_precision,
    )

    # ------------------------------------------------------------------
    # Step 5: Limits / kill switch.
    # Any rejection aborts with a clear reason and non-zero exit.
    # ------------------------------------------------------------------
    limits_config = LimitsConfig()
    limit_decision = check_limits(
        order,
        open_positions=open_positions,
        day_pl=day_pl,
        equity=equity,
        start_of_day_equity=start_of_day_equity,
        config=limits_config,
        now=run_dt,
        order_risk=sizing_result.risk_amount,
    )

    if not limit_decision.allowed:
        if limit_decision.kill_switch_active:
            ks = kill_switch_status(
                day_pl=day_pl,
                start_of_day_equity=start_of_day_equity,
                config=limits_config,
                now=run_dt,
            )
            _log.warning(
                "execute: kill switch ACTIVE — day_pl=%.6g cap_amount=%.6g "
                "reset_at=%s",
                ks.day_pl,
                ks.cap_amount,
                ks.reset_at.isoformat(),
            )
            print(
                f"REJECTED by limits (kill switch ACTIVE): {limit_decision.reason}\n"
                f"Kill switch resets at {ks.reset_at.isoformat()}.",
                file=sys.stderr,
            )
        else:
            _log.warning("execute: limits rejected: %s", limit_decision.reason)
            print(
                f"REJECTED by limits: {limit_decision.reason}",
                file=sys.stderr,
            )
        return 1

    _log.info("execute: limits check → allowed.")

    # ------------------------------------------------------------------
    # --dry-run: print the would-be order and exit 0 (no v20 call).
    # ------------------------------------------------------------------
    if dry_run:
        order_dict = order.model_dump()
        # Convert non-serialisable types for display.
        order_dict["created_at"] = order.created_at.isoformat()
        order_dict["direction"] = str(order.direction.value)
        order_dict["entry_type"] = str(order.entry_type.value)
        print("[DRY-RUN] Would submit the following order (no v20 call made):")
        print(json.dumps(order_dict, indent=2, default=str))
        _log.info(
            "execute --dry-run: gate passed, order NOT submitted "
            "(client_order_id=%s).",
            order.client_order_id,
        )
        return 0

    # ------------------------------------------------------------------
    # Step 6: Interactive confirm (unless --yes) + submit.
    # N-3: this [y/N] confirm is DEMO-ONLY.  Live confirmation is the typed
    # account-id prompt in the live gate above, which --yes does NOT bypass.
    # ------------------------------------------------------------------
    if settings.env != "live" and not skip_confirm:
        summary = (
            f"  Instrument : {order.instrument}\n"
            f"  Direction  : {order.direction.value}\n"
            f"  Units      : {order.units}\n"
            f"  Stop       : {order.stop_loss_price}\n"
            f"  Target     : {order.take_profit_price}\n"
            f"  ID         : {order.client_order_id}\n"
        )
        print(f"\nAbout to submit order to OANDA ({settings.env}):\n{summary}")
        try:
            answer = input("Confirm submit? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 1
        if answer not in ("y", "yes"):
            print("Aborted by operator.", file=sys.stderr)
            return 1

    store_submit = Store(db_path)
    try:
        fill = submit_order(
            order,
            client=client,
            store=store_submit,
            entry_ref=candidate.entry_ref,
            precision=inst_meta.display_precision,
            now=run_dt,
        )
    except OrderRejected as exc:
        _log.error("execute: broker rejected order: %s", exc)
        print(f"ORDER REJECTED by broker: {exc}", file=sys.stderr)
        store_submit.close()
        return 1
    except Exception as exc:  # noqa: BLE001
        _log.error("execute: order submission failed: %s", exc)
        print(f"ERROR: order submission failed: {exc}", file=sys.stderr)
        store_submit.close()
        return 1
    finally:
        store_submit.close()

    # Print the Fill to stdout (INV-14 contract).
    fill_dict = {
        "client_order_id": fill.client_order_id,
        "broker_trade_id": fill.broker_trade_id,
        "fill_price": fill.fill_price,
        "units_filled": fill.units_filled,
        "slippage": fill.slippage,
        "status": str(fill.status.value),
        "filled_at": fill.filled_at.isoformat(),
    }
    print(json.dumps(fill_dict, indent=2))
    _log.info(
        "fathom execute finished at %s. Fill: %s/%s price=%.5f slippage=%.5f.",
        _utc_now_rfc3339(),
        fill.broker_trade_id,
        fill.status.value,
        fill.fill_price,
        fill.slippage,
    )
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    """Execute the ``fathom positions`` command.

    Prints the store's open ``Position[]`` as JSON (INV-14 shape).
    No live HTTP — pure DB read.

    INV-01: never a Hermes tool.
    """
    _log.info("fathom positions started at %s.", _utc_now_rfc3339())

    db_path: str = args.db_path
    store = Store(db_path)
    try:
        open_positions = store.load_open_positions()
    finally:
        store.close()

    output_list = []
    for pos in open_positions:
        pos_dict = {
            "broker_trade_id": pos.broker_trade_id,
            "instrument": pos.instrument,
            "units": pos.units,
            "entry_price": pos.entry_price,
            "stop_loss_price": pos.stop_loss_price,
            "take_profit_price": pos.take_profit_price,
            "opened_at": pos.opened_at.isoformat(),
            "unrealized_pl": pos.unrealized_pl,
            "closed_at": pos.closed_at.isoformat() if pos.closed_at else None,
            "realized_pl": pos.realized_pl,
            "candidate_ref": pos.candidate_ref,
        }
        output_list.append(pos_dict)

    print(json.dumps(output_list, indent=2, default=str))
    _log.info(
        "fathom positions finished at %s. Open positions: %d.",
        _utc_now_rfc3339(),
        len(open_positions),
    )
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Execute the ``fathom reconcile`` command.

    Runs one broker-truth reconciliation pass and prints the
    ``ReconcileReport`` as JSON.

    INV-01: never a Hermes tool.
    INV-07: practice endpoint only (settings.env).
    INV-08: no token logged.
    INV-03: all timestamps UTC.
    """
    run_dt = datetime.now(tz=timezone.utc)
    _log.info("fathom reconcile started at %s.", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    db_path: str = args.db_path
    settings = Settings()
    _log.info("reconcile: connecting to OANDA (env=%s).", settings.env)  # INV-08

    client = OandaClient(settings)
    store = Store(db_path)
    try:
        report = reconcile(client=client, store=store, now=run_dt)
    except Exception as exc:  # noqa: BLE001
        _log.error("reconcile: failed: %s", exc)
        print(f"ERROR: reconcile failed: {exc}", file=sys.stderr)
        store.close()
        return 1
    finally:
        store.close()

    report_dict = {
        "adopted": report.adopted,
        "closed": report.closed,
        "matched": report.matched,
        "drift_flags": report.drift_flags,
        "start_of_day_equity": report.start_of_day_equity,
        "day_pl": report.day_pl,
        "snapshotted_today": report.snapshotted_today,
        "as_of": run_dt.isoformat(),
    }
    print(json.dumps(report_dict, indent=2))
    _log.info(
        "fathom reconcile finished at %s. adopted=%d closed=%d matched=%d "
        "day_pl=%.4f.",
        _utc_now_rfc3339(),
        len(report.adopted),
        len(report.closed),
        len(report.matched),
        report.day_pl,
    )
    return 0


# ---------------------------------------------------------------------------
# preflight command (P5-T-03)
# ---------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> int:
    """Execute the ``fathom preflight`` command.  Returns the process exit code.

    Read-only: no order is placed, no state is written.  Exits 0 on GO,
    non-zero (1) on NO-GO.  INV-07: requires ``--attest-track-record`` for GO.
    INV-08: never prints the OANDA token.
    """
    from execution.preflight import run_preflight

    run_dt = datetime.now(tz=timezone.utc)
    _log.info("fathom preflight started at %s", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    db_path = args.db_path
    attested: bool = args.attest_track_record

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not load settings: {exc}", file=sys.stderr)
        return 1

    store = Store(db_path)
    try:
        # Build a real OandaClient for the reachability check.
        try:
            client: "Optional[OandaClient]" = OandaClient(settings)
        except Exception as exc:  # noqa: BLE001
            _log.warning("preflight: could not construct OandaClient: %s", exc)
            client = None

        report = run_preflight(
            settings=settings,
            store=store,
            client=client,
            attested=attested,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("preflight: unexpected error: %s", exc)
        print(f"ERROR: preflight failed unexpectedly: {exc}", file=sys.stderr)
        store.close()
        return 1
    finally:
        store.close()

    # Print per-check status table.
    print(f"\nFathom preflight check — {report.checked_at.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC\n")
    print(f"{'Check':<35}  {'Status':<8}  Detail")
    print("-" * 90)
    for check in report.checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{check.name:<35}  {status:<8}  {check.detail}")

    print()
    if report.go:
        print("OVERALL: GO — all checks passed.  System is mechanically ready.")
        _log.info("fathom preflight: GO at %s", report.checked_at.isoformat())
        return 0
    else:
        failing = [c.name for c in report.checks if not c.ok]
        print(
            f"OVERALL: NO-GO — failing check(s): {', '.join(failing)}",
            file=sys.stderr,
        )
        _log.info(
            "fathom preflight: NO-GO at %s (failing: %s)",
            report.checked_at.isoformat(),
            ", ".join(failing),
        )
        return 1


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
    if args.command == "execute":
        return cmd_execute(args)
    if args.command == "positions":
        return cmd_positions(args)
    if args.command == "reconcile":
        return cmd_reconcile(args)
    if args.command == "preflight":
        return cmd_preflight(args)
    parser.error(f"Unknown command: {args.command}")
    return 2  # unreachable — parser.error exits


if __name__ == "__main__":
    sys.exit(main())
