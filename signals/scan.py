"""Order-free scan entrypoint — ``signals/scan.py::run_scan`` (P4-T-01).

This module provides the **only** scan entrypoint that the admin panel (and any
other always-on surface) may call.  It is deliberately kept **order-free**:

* It imports ONLY data/signals/config modules.
* It NEVER imports ``execution.orders``, ``execution.models.build_bracket``,
  ``risk.sizing``, or ``risk.limits`` placement paths.
* It does NOT import ``cli`` (which carries the order path at module level).

INV-01 enforcement clause (Phase 4 addition)
---------------------------------------------
No always-on or operator-facing read surface (``panel/``, the deviation
monitor, any future dashboard) may reach order-placement or risk
sizing/placement code — directly or transitively.  ``run_scan`` is the
**order-free entrypoint** they must use; ``cli.cmd_scan`` is a thin argparse
adapter over it, NOT the callable.

A transitive-import boundary test in ``tests/test_scan.py`` walks
``signals.scan``'s module graph (via a subprocess) and asserts the execution /
risk placement modules are unreachable from this module.

INV-03: all timestamps UTC RFC 3339 (``datetime.now(timezone.utc)``).
INV-08: OANDA token / account ID never logged.
INV-10: empty approved-set → empty watchlist, no exception.
INV-13: returns ``Candidate[]`` — the frozen Hermes-facing wire contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from signals.ranker import Candidate

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default scan history window (mirrors cli._DEFAULT_HISTORY_YEARS)
# ---------------------------------------------------------------------------

#: Default years of candle history to request/cache.  Must comfortably exceed
#: the longest walk-forward train+test window (D: 30 months) so at least one
#: window forms.  Overridable via the ``history_years`` kwarg.
_DEFAULT_HISTORY_YEARS: int = 3


# ---------------------------------------------------------------------------
# Internal helpers — order-free, import-clean
# ---------------------------------------------------------------------------


def _build_date_range(history_years: int) -> tuple[datetime, datetime]:
    """Return a (start, end) UTC date range for candle fetching.

    ``end`` is today at midnight UTC; ``start`` is ``history_years`` × 365
    days before that.  INV-03: both are UTC-aware.
    """
    end = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = end - timedelta(days=history_years * 365)
    return start, end


def _discover_instruments(instruments_arg: str, db_path: str) -> list[str]:
    """Return the instrument list from the cache (dry-run / scan always reads cache).

    Unlike the CLI's ``_discover_universe``, this function NEVER hits the live
    OANDA API — it only reads the cached ``instruments`` table.  The admin panel
    (and any caller that wants a purely order-free, no-HTTP path) uses this.

    A non-ALL value is split and returned verbatim.
    """
    if instruments_arg.strip().upper() != "ALL":
        return [s.strip() for s in instruments_arg.split(",") if s.strip()]

    # ALL → read from the cached instruments table (no HTTP, no Settings).
    from data.store import Store

    store = Store(db_path)
    try:
        cached = [m.name for m in store.load_instruments()]
    finally:
        store.close()

    _log.info(
        "Universe (ALL, cache-only): %d instruments from cached metadata.",
        len(cached),
    )
    return sorted(cached)


def _fetch_candles_for_instruments(
    instruments: list[str],
    timeframes: list[str],
    db_path: str,
    start: datetime,
    end: datetime,
) -> None:
    """Populate the candle store for every (instrument, timeframe) pair.

    Lazy-imports ``Settings`` / ``OandaClient`` / ``fetch_and_cache`` so that
    callers using ``dry_run=True`` never touch the Settings / token path at all.

    INV-08: never log the token or account ID.
    INV-03: ``start`` / ``end`` must be UTC-aware.
    """
    # These imports are order-free: config/data only, no execution/risk.
    from config.settings import Settings
    from data.candles import fetch_and_cache
    from data.oanda_client import OandaClient
    from data.store import Store

    settings = Settings()
    _log.info("Fetching candles for scan (env=%s).", settings.env)  # INV-08: env not token
    client = OandaClient(settings)
    store = Store(db_path)
    try:
        pairs = [(inst, tf) for inst in sorted(instruments) for tf in sorted(timeframes)]
        for inst, tf in pairs:
            _log.info(
                "Fetching %s/%s from %s to %s …",
                inst,
                tf,
                start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            df = fetch_and_cache(
                client=client,
                store=store,
                instrument=inst,
                granularity=tf,
                start=start,
                end=end,
                write_parquet=False,
            )
            _log.info("Fetched %s/%s: %d candles cached.", inst, tf, len(df))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_scan(
    *,
    db_path: str,
    instruments: str = "ALL",
    timeframes: str = "H1,H4,D",
    history_years: int = _DEFAULT_HISTORY_YEARS,
    dry_run: bool = False,
) -> list[Candidate]:
    """Run the order-free scan pipeline and return the ranked ``Candidate[]``.

    This is the **single entrypoint** the admin panel (and any other non-CLI
    surface) must use.  It is intentionally order-free: it never imports or
    calls ``execution.*``, ``risk.*``, or ``cli``.

    Parameters
    ----------
    db_path:
        Path to the SQLite store (candles + approved_set + watchlist).
    instruments:
        ``"ALL"`` to use the cached universe; otherwise a comma-separated list
        of OANDA instrument identifiers (e.g. ``"EUR_USD,GBP_USD"``).
    timeframes:
        Comma-separated granularity codes, e.g. ``"H1,H4,D"``.
    history_years:
        Years of candle history to fetch/cache (live mode only; ignored under
        ``dry_run``).
    dry_run:
        If ``True``, skip the live candle fetch entirely.  The ranker runs
        against whatever candles are already cached.

    Returns
    -------
    list[Candidate]
        The ranked, portfolio-limited candidate list (may be empty — INV-10).

    Raises
    ------
    Exception
        Any exception from the ranker / portfolio step propagates to the caller.
        The CLI adapter (``cli.cmd_scan``) catches these and converts them to
        exit-code 1.

    Notes
    -----
    INV-01: this function is **order-free**.  It never imports ``execution.*``,
        ``risk.*``, or ``cli``.
    INV-03: the ``run_dt`` timestamp is UTC-aware.
    INV-08: Settings / token are accessed only in the live candle fetch path;
        they are NEVER logged.
    INV-10: an empty approved-set is a valid result; ``[]`` is returned, not an
        exception.
    INV-13: the returned objects are ``Candidate`` instances (the frozen
        Hermes-facing wire contract).
    """
    run_dt = datetime.now(tz=timezone.utc)
    _log.info("run_scan started at %s", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    tf_list = [s.strip() for s in timeframes.split(",") if s.strip()]

    # ---- Optional candle refresh (live mode only; skipped under dry_run). ---
    if not dry_run:
        instruments_list = _discover_instruments(instruments, db_path)
        if instruments_list:
            start_fetch, end_fetch = _build_date_range(history_years)
            _fetch_candles_for_instruments(
                instruments=instruments_list,
                timeframes=tf_list,
                db_path=db_path,
                start=start_fetch,
                end=end_fetch,
            )

    # ---- Ranker + PortfolioLimiter (lazy imports; order-free). --------------
    # These are imported lazily so --dry-run never constructs Settings/OandaClient
    # unnecessarily, and to keep the module's top-level import graph clean.
    from data.calendar import FairEconomyCalendar
    from data.store import Store
    from signals.portfolio import PortfolioLimiter
    from signals.ranker import Ranker

    store = Store(db_path)
    try:
        # FairEconomyCalendar.upcoming_events returns list[CalendarEvent] while
        # Ranker's _CalendarLike Protocol declares list[object].  Structurally
        # compatible but mypy's strict covariance check rejects it — suppress.
        ranker = Ranker(store=store, calendar=FairEconomyCalendar(db_path=db_path))  # type: ignore[arg-type]
        limiter = PortfolioLimiter(store=store)

        _log.info("Running Ranker at %s …", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        ranked: list[Candidate] = ranker.rank(now=run_dt)
        candidates: list[Candidate] = limiter.apply(ranked)

        _log.info(
            "Ranker produced %d candidate(s); %d after portfolio limits.",
            len(ranked),
            len(candidates),
        )

        # Persist to watchlist table (single transaction).
        written = store.write_watchlist(candidates, run_timestamp=run_dt)
        _log.info(
            "Persisted %d watchlist row(s) (run_timestamp=%s).",
            written,
            run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    finally:
        store.close()

    _log.info(
        "run_scan finished at %s. Candidates: %d.",
        run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        len(candidates),
    )
    return candidates
