"""PoC runner — end-to-end: fetch candles → walk-forward → approved-set table.

Scope: POC-T-07.

This script wires together all PoC components:
  1. Build ``Settings`` → ``OandaClient`` → ``Store``.
  2. For each instrument × granularity, ``fetch_and_cache`` candles for the
     requested history window.
  3. Run ``MACrossover`` for every (fast, slow) combination through
     ``WalkForwardValidator``.
  4. Collect approved-set entries and print a human-readable table.

Empty approved set (all combinations failed walk-forward criteria) is a
**valid, non-error result** — the script exits 0 and prints a clear message.
It is NOT a failure; the PoC hypothesis is simply unproven for this parameter
space.

INV-03: all log timestamps are UTC RFC 3339.
INV-08: the OANDA API token and account ID are NEVER printed or logged.

Usage
-----
    python scripts/poc_run.py [--instruments ...] [--granularities ...]
                              [--history-years N] [--fast-periods ...]
                              [--slow-periods ...] [--dry-run]

Defaults match the PoC parameters from docs/phases/poc.md:
    --instruments    EUR_USD,GBP_USD,USD_JPY
    --granularities  H1,D
    --history-years  2
    --fast-periods   10,20
    --slow-periods   50,100,200
    --dry-run        (flag, default off) — skips OANDA fetch, runs cached only
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backtest.costs import CostParams
from backtest.engine import BacktestEngine
from backtest.walkforward import ApprovedSetEntry, WalkForwardValidator
from config.settings import Settings
from data.oanda_client import OandaClient
from data.store import Store
from strategies.trend import MACrossover

# ---------------------------------------------------------------------------
# Logging setup — UTC RFC 3339 timestamps (INV-03)
# ---------------------------------------------------------------------------


class _UTCFormatter(logging.Formatter):
    """Emit log records with UTC RFC 3339 timestamps (INV-03)."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _UTCFormatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-instrument cost parameters
# ---------------------------------------------------------------------------
# These are PoC defaults: 1.5 pip spread, 0.5 pip slippage.
# pip_value differs for JPY pairs (0.01) vs. other majors (0.0001).

_COST_PARAMS: dict[str, CostParams] = {
    "EUR_USD": CostParams(spread_pips=1.5, slippage_pips=0.5, pip_value=0.0001),
    "GBP_USD": CostParams(spread_pips=1.5, slippage_pips=0.5, pip_value=0.0001),
    "USD_JPY": CostParams(spread_pips=1.5, slippage_pips=0.5, pip_value=0.01),
}
_DEFAULT_COST_PARAMS = CostParams(
    spread_pips=1.5, slippage_pips=0.5, pip_value=0.0001
)


def _cost_params_for(instrument: str) -> CostParams:
    return _COST_PARAMS.get(instrument, _DEFAULT_COST_PARAMS)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fathom PoC runner — fetch candles, backtest, print approved-set table.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--instruments",
        default="EUR_USD,GBP_USD,USD_JPY",
        help="Comma-separated OANDA instrument identifiers.",
    )
    parser.add_argument(
        "--granularities",
        default="H1,D",
        help="Comma-separated OANDA granularity strings.",
    )
    parser.add_argument(
        "--history-years",
        type=int,
        default=2,
        metavar="N",
        help="Number of years of history to fetch/use.",
    )
    parser.add_argument(
        "--fast-periods",
        default="10,20",
        help="Comma-separated fast EMA periods.",
    )
    parser.add_argument(
        "--slow-periods",
        default="50,100,200",
        help="Comma-separated slow EMA periods.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Skip OANDA fetch entirely — run walk-forward only against cached "
            "data in the store (or an empty store). No HTTP requests are made."
        ),
    )
    parser.add_argument(
        "--db-path",
        default="data/fathom_poc.db",
        help="Path to the SQLite candle store.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _build_date_range(history_years: int) -> tuple[datetime, datetime]:
    """Return (start, end) as UTC-aware datetimes for ``history_years`` back."""
    end = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Approximate years as 365 days each — close enough for the PoC window.
    from datetime import timedelta
    start = end - timedelta(days=history_years * 365)
    return start, end


def _print_approved_table(entries: list[ApprovedSetEntry]) -> None:
    """Print the approved-set table to stdout in a readable tabular format."""
    if not entries:
        print("No combinations passed walk-forward criteria.")
        return

    # Header
    header = (
        f"{'Instrument':<12} {'Gran':<6} {'Fast':>5} {'Slow':>5} "
        f"{'OOS Sharpe':>12} {'Trade Count':>12} {'Swap Modelled':<14}"
    )
    separator = "-" * len(header)

    print()
    print("=== Approved-Set Table ===")
    print(separator)
    print(header)
    print(separator)

    for e in entries:
        # Extract fast and slow periods from strategy_name e.g. "MACrossover(10,50)"
        fast_str, slow_str = _parse_periods(e.strategy_name)
        print(
            f"{e.instrument:<12} {e.granularity:<6} {fast_str:>5} {slow_str:>5} "
            f"{e.oos_sharpe_mean:>12.4f} {e.oos_trade_count_total:>12} "
            f"{str(e.swap_modelled):<14}"
        )

    print(separator)
    print(f"Total entries: {len(entries)}")
    print()


def _parse_periods(strategy_name: str) -> tuple[str, str]:
    """Extract fast and slow period strings from 'MACrossover(fast,slow)'."""
    try:
        inner = strategy_name.split("(")[1].rstrip(")")
        parts = inner.split(",")
        return parts[0].strip(), parts[1].strip()
    except (IndexError, ValueError):
        return "?", "?"


# ---------------------------------------------------------------------------
# Main run logic
# ---------------------------------------------------------------------------


def run(argv: Optional[list[str]] = None) -> int:
    """Main entry point. Returns the exit code (0 on success)."""
    _configure_logging()
    args = _parse_args(argv)

    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    granularities = [s.strip() for s in args.granularities.split(",") if s.strip()]
    fast_periods = [int(s.strip()) for s in args.fast_periods.split(",") if s.strip()]
    slow_periods = [int(s.strip()) for s in args.slow_periods.split(",") if s.strip()]
    history_years: int = args.history_years
    dry_run: bool = args.dry_run
    db_path: str = args.db_path

    start_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log.info("PoC runner started at %s", start_ts)
    _log.info(
        "Parameters: instruments=%s granularities=%s history_years=%d "
        "fast_periods=%s slow_periods=%s dry_run=%s",
        instruments,
        granularities,
        history_years,
        fast_periods,
        slow_periods,
        dry_run,
    )

    # Build Settings — validation will raise if .env is absent and required
    # fields are missing (unless we're in dry_run mode).  In dry_run mode the
    # Settings object is still built (we may need the store path), but we never
    # call OandaClient (INV-08 — no token access in dry_run is fine because
    # the client is never constructed, but Settings is harmless to build with a
    # dummy token if needed).
    #
    # For dry_run, we still try to build Settings; if it fails (no .env) we
    # substitute a no-op client below.
    settings: Optional[Settings] = None
    client: Optional[OandaClient] = None

    if not dry_run:
        try:
            settings = Settings()
            # INV-08: never log token or account ID.
            _log.info(
                "Settings loaded: env=%s base_url=%s",
                settings.env,
                settings.oanda_base_url,
            )
            client = OandaClient(settings)
            _log.info("OandaClient ready.")
        except Exception as exc:
            _log.error("Failed to build Settings/OandaClient: %s", exc)
            return 1

    # Initialise the store.
    store = Store(db_path)
    _log.info("Store opened at %s", db_path)

    date_start, date_end = _build_date_range(history_years)
    _log.info(
        "Date range: %s → %s",
        date_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        date_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    # Phase 1: fetch / load candles for all combinations.
    if not dry_run and client is not None:
        _log.info("Fetching/caching candles (dry_run=False)...")
        from data.candles import fetch_and_cache

        for instrument in instruments:
            for gran in granularities:
                _log.info("  Fetching %s %s ...", instrument, gran)
                try:
                    df = fetch_and_cache(
                        client, store, instrument, gran, date_start, date_end
                    )
                    _log.info(
                        "  %s %s: %d candles cached.",
                        instrument,
                        gran,
                        len(df),
                    )
                except Exception as exc:
                    _log.error(
                        "  Fetch failed for %s %s: %s — skipping.",
                        instrument,
                        gran,
                        exc,
                    )
    else:
        _log.info(
            "dry_run=True — skipping OANDA fetch; using cached data only."
        )

    # Phase 2: walk-forward validation for every instrument × granularity ×
    # (fast, slow) combination.
    approved_entries: list[ApprovedSetEntry] = []

    total_combos = len(instruments) * len(granularities) * len(fast_periods) * len(slow_periods)
    _log.info("Running walk-forward for %d combinations...", total_combos)

    combo_idx = 0
    for instrument in instruments:
        cost_params = _cost_params_for(instrument)
        engine = BacktestEngine(store=store, cost_params=cost_params)

        for gran in granularities:
            for fast in fast_periods:
                for slow in slow_periods:
                    if fast >= slow:
                        # Invalid combination — fast must be < slow.
                        continue

                    combo_idx += 1
                    strategy = MACrossover(
                        fast_period=fast,
                        slow_period=slow,
                        instrument=instrument,
                        timeframe=gran,
                    )
                    validator = WalkForwardValidator(engine=engine, strategy=strategy)

                    _log.info(
                        "  [%d/%d] %s %s fast=%d slow=%d ...",
                        combo_idx,
                        total_combos,
                        instrument,
                        gran,
                        fast,
                        slow,
                    )

                    try:
                        result = validator.run(
                            instrument=instrument,
                            granularity=gran,
                            start=date_start,
                            end=date_end,
                        )
                    except Exception as exc:
                        _log.warning(
                            "  Walk-forward failed for %s %s (%d/%d): %s — skipping.",
                            instrument,
                            gran,
                            fast,
                            slow,
                            exc,
                        )
                        continue

                    if result.approved_set_entry is not None:
                        approved_entries.append(result.approved_set_entry)
                        _log.info(
                            "  APPROVED: %s %s fast=%d slow=%d "
                            "OOS_sharpe=%.4f trades=%d swap_modelled=%s",
                            instrument,
                            gran,
                            fast,
                            slow,
                            result.approved_set_entry.oos_sharpe_mean,
                            result.approved_set_entry.oos_trade_count_total,
                            result.approved_set_entry.swap_modelled,
                        )
                    else:
                        _log.info(
                            "  Not approved: %s %s fast=%d slow=%d "
                            "(%d windows, no qualifying OOS metrics).",
                            instrument,
                            gran,
                            fast,
                            slow,
                            len(result.windows),
                        )

    # Phase 3: print results.
    _print_approved_table(approved_entries)

    end_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log.info("PoC runner finished at %s. Approved entries: %d", end_ts, len(approved_entries))

    # Empty approved set is a valid result — exit 0 (not 1).
    store.close()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(run())
