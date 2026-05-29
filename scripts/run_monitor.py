"""Always-on deviation monitor entrypoint (P3-T-08).

Constructs the live ``PriceStream``, store loader, alerter, and ``Watcher``
and calls ``watcher.run()`` indefinitely.

Usage::

    ./.venv/bin/python scripts/run_monitor.py [--instruments EUR_USD,GBP_USD]
                                              [--db-path PATH]
                                              [--heartbeat-timeout SECS]
                                              [--debounce-secs SECS]
                                              [--reconcile-interval SECS]
                                              [--severe-response alert_only|auto_flatten|tighten_stop]

Invariants
----------
* **INV-01** — never opens a position; alert-only by default.
* **INV-03** — all event timestamps UTC-aware.
* **INV-08** — credentials from ``Settings``/.env; never printed/logged.
* **INV-09** — single code path; demo/live differentiated by ``settings.env``.

The alerter is a ``NoOpAlerter`` stub (logs only) until monitor-alerts T-09
ships.  To wire a real alerter, replace the stub with the T-09 implementation
and pass it in.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
from pathlib import Path
from typing import Callable

# Allow running as `python scripts/run_monitor.py` without installing.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import Settings
from data.store import Store
from data.stream import PriceStream
from monitoring.watcher import (
    NoOpAlerter,
    NoOpExecutionResponder,
    PositionSnapshot,
    Watcher,
    WatcherConfig,
)

logger = logging.getLogger("fathom.scripts.run_monitor")


def _build_store_loader(store: Store) -> "Callable[[], list[PositionSnapshot]]":
    """Return a callable that loads open positions as PositionSnapshots."""

    def _load() -> list[PositionSnapshot]:
        raw = store.load_open_positions()
        snapshots: list[PositionSnapshot] = []
        for pos in raw:
            snapshots.append(
                PositionSnapshot(
                    broker_trade_id=pos.broker_trade_id,
                    instrument=pos.instrument,
                    units=pos.units,
                    entry_price=pos.entry_price,
                    stop_loss_price=pos.stop_loss_price,
                    take_profit_price=pos.take_profit_price,
                    fill_slippage=0.0,  # slippage not yet wired from fills table
                )
            )
        return snapshots

    return _load


def main() -> None:
    """Parse args, build components, run the watcher (blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Fathom deviation monitor — always-on position watcher"
    )
    parser.add_argument(
        "--instruments",
        default="EUR_USD",
        help="Comma-separated OANDA instruments to monitor (default: EUR_USD)",
    )
    parser.add_argument(
        "--db-path",
        default="fathom.db",
        help="Path to the SQLite database (default: fathom.db)",
    )
    parser.add_argument(
        "--heartbeat-timeout",
        type=float,
        default=15.0,
        help="Feed-health heartbeat timeout in seconds (default: 15.0)",
    )
    parser.add_argument(
        "--debounce-secs",
        type=float,
        default=300.0,
        help="Debounce window in seconds (default: 300.0)",
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=60.0,
        help="Position refresh interval in seconds (default: 60.0)",
    )
    parser.add_argument(
        "--severe-response",
        choices=["alert_only", "auto_flatten", "tighten_stop"],
        default="alert_only",
        help="Response on severe deviation (default: alert_only, INV-01)",
    )
    args = parser.parse_args()

    instruments = [i.strip() for i in args.instruments.split(",") if i.strip()]

    settings = Settings()  # reads from .env (INV-08)
    store = Store(db_path=args.db_path)

    config = WatcherConfig(
        heartbeat_timeout_seconds=args.heartbeat_timeout,
        debounce_seconds=args.debounce_secs,
        reconcile_interval_seconds=args.reconcile_interval,
        severe_response=args.severe_response,
    )

    stream = PriceStream(settings=settings, instruments=instruments)
    stream.start()

    from data.stream import PriceTick as _PriceTick

    alerter = NoOpAlerter()  # T-09 will replace this with the real alerter
    responder = NoOpExecutionResponder()

    logger.info(
        "run_monitor: starting watcher for instruments=%s severe_response=%s",
        instruments,
        config.severe_response,
    )

    # Build a typed queue; the bridge thread feeds it from the PriceStream.
    typed_queue: queue.Queue[_PriceTick | None] = queue.Queue()

    def _stream_to_queue() -> None:
        """Bridge: feed ticks from PriceStream into the typed queue."""
        try:
            for tick in stream:
                typed_queue.put(tick)
        finally:
            typed_queue.put(None)  # sentinel: stream stopped

    bridge = threading.Thread(target=_stream_to_queue, daemon=True, name="monitor-bridge")
    bridge.start()

    watcher = Watcher(
        tick_source=typed_queue,
        store_loader=_build_store_loader(store),
        alerter=alerter,
        config=config,
        execution_responder=responder,
        instruments=instruments,
    )

    try:
        watcher.run()
    except KeyboardInterrupt:
        logger.info("run_monitor: KeyboardInterrupt — shutting down")
    finally:
        stream.stop()
        store.close()


if __name__ == "__main__":
    main()
