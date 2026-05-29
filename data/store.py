"""SQLite persistence layer for candle data and instrument metadata,
plus Parquet candle archive.

Scope: Phase 1 (P1A-T-01 data-layer-expansion).

Storage design:
  - SQLite ``candles`` table: source of truth for gap detection and
    operational/cache state.  Unchanged contract from PoC.
  - SQLite ``instruments`` table: metadata cache for ``InstrumentMeta``.
    Refreshable (upsert on re-fetch).
  - Parquet archive: bulk columnar store for full-universe research scans.
    Partitioned as ``{archive_dir}/{instrument}/{granularity}/{YYYY-MM-DD}.parquet``.
    Written via ``pyarrow``; read back as ``pd.DataFrame`` with the same
    dtype contract as ``load_candles``.

INV-03 compliance: ``time`` values are stored as UTC RFC 3339 TEXT strings
    in SQLite and as ``datetime64[ns, UTC]`` in Parquet (pyarrow stores the
    timezone in column metadata; the round-trip is verified in tests).  On
    SQLite load, timestamps are parsed explicitly with
    ``pd.to_datetime(..., utc=True)`` — we do NOT rely on sqlite3 PARSE_DECLTYPES.

D-02: all data is returned as ``pd.DataFrame`` with ``time`` dtype
    ``datetime64[ns, UTC]``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data.oanda_client import CandleRow, InstrumentMeta

if TYPE_CHECKING:
    # Import for typing only — importing at runtime would create a cycle
    # (store → walkforward → engine → store).  ``write_approved_set`` uses
    # only attribute access on the entries, so a TYPE_CHECKING import is safe.
    from backtest.walkforward import ApprovedSetEntry
    from signals.ranker import Candidate
    from execution.models import Fill, Order, Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_rfc3339(dt: datetime) -> str:
    """Convert a UTC-aware datetime to an RFC 3339 string ending in ``Z``.

    Examples
    --------
    >>> _to_rfc3339(datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc))
    '2024-01-15T14:00:00Z'

    Args:
        dt: A UTC-aware datetime.  If it is naive, ``timezone.utc`` is assumed
            and a runtime warning would be appropriate, but this function
            treats naive as UTC as a safety net (callers must enforce INV-03).

    Returns:
        RFC 3339 UTC string, e.g. ``"2024-01-15T14:00:00Z"``.
    """
    if dt.tzinfo is None:
        # Callers must pass UTC-aware datetimes. Attach UTC defensively.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """SQLite-backed candle store + Parquet candle archive.

    Creates and manages a ``candles`` table (gap detection / operational state)
    and an ``instruments`` table (metadata cache) in SQLite.

    Parquet files are written to
    ``{archive_dir}/{instrument}/{granularity}/{YYYY-MM-DD}.parquet``
    when ``write_parquet`` is called.  The archive directory is created on first
    write if it does not exist.

    Args:
        db_path: File path for the SQLite database.  Pass ``":memory:"`` for
            an in-memory database (useful in tests).
        archive_dir: Directory root for the Parquet archive.  Defaults to a
            sibling ``archive/`` directory next to ``db_path``.  For in-memory
            databases a temporary directory must be supplied explicitly if
            Parquet methods are used.
    """

    #: SQL to create the candles table if it does not already exist.
    _CREATE_CANDLES_SQL: str = """
        CREATE TABLE IF NOT EXISTS candles (
            instrument   TEXT    NOT NULL,
            granularity  TEXT    NOT NULL,
            time         TEXT    NOT NULL,
            open_bid     REAL    NOT NULL,
            high_bid     REAL    NOT NULL,
            low_bid      REAL    NOT NULL,
            close_bid    REAL    NOT NULL,
            open_ask     REAL    NOT NULL,
            high_ask     REAL    NOT NULL,
            low_ask      REAL    NOT NULL,
            close_ask    REAL    NOT NULL,
            volume       INTEGER NOT NULL,
            complete     INTEGER NOT NULL,
            PRIMARY KEY (instrument, granularity, time)
        )
    """

    #: SQL to create the instruments metadata table if it does not exist.
    _CREATE_INSTRUMENTS_SQL: str = """
        CREATE TABLE IF NOT EXISTS instruments (
            name                     TEXT    NOT NULL PRIMARY KEY,
            pip_location             INTEGER NOT NULL,
            min_trade_size           REAL    NOT NULL,
            margin_rate              REAL    NOT NULL,
            display_precision        INTEGER NOT NULL,
            long_rate                REAL    NOT NULL,
            short_rate               REAL    NOT NULL,
            financing_days_of_week   TEXT    NOT NULL,
            fetched_at               TEXT    NOT NULL
        )
    """

    #: SQL to create the approved_set table if it does not already exist.
    #: Mirrors the shipped ``ApprovedSetEntry`` model (strategy_name,
    #: instrument, granularity, oos_sharpe_mean, oos_trade_count_total,
    #: swap_modelled) plus a DB-table-only ``run_timestamp`` (UTC RFC 3339).
    #: This is the INV-10 gate Phase 2's ranker reads.  The column is named
    #: ``granularity`` (the shipped field name — not "timeframe").
    _CREATE_APPROVED_SET_SQL: str = """
        CREATE TABLE IF NOT EXISTS approved_set (
            run_timestamp          TEXT    NOT NULL,
            strategy_name          TEXT    NOT NULL,
            instrument             TEXT    NOT NULL,
            granularity            TEXT    NOT NULL,
            oos_sharpe_mean        REAL    NOT NULL,
            oos_trade_count_total  INTEGER NOT NULL,
            swap_modelled          INTEGER NOT NULL,
            PRIMARY KEY (run_timestamp, strategy_name, instrument, granularity)
        )
    """

    #: SQL to insert one approved_set row.  ``INSERT OR REPLACE`` keeps a re-run
    #: with the same ``run_timestamp`` idempotent.
    _INSERT_APPROVED_SET_SQL: str = """
        INSERT OR REPLACE INTO approved_set
            (run_timestamp, strategy_name, instrument, granularity,
             oos_sharpe_mean, oos_trade_count_total, swap_modelled)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
    """

    #: SQL to create the watchlist table — run-timestamped, mirrors the
    #: ``approved_set`` pattern.  Each row stores one ``Candidate`` from a
    #: ``fathom scan`` run, identified by ``run_timestamp``.  The INV-13
    #: Candidate fields are stored as columns; the Candidate model is unchanged.
    _CREATE_WATCHLIST_SQL: str = """
        CREATE TABLE IF NOT EXISTS watchlist (
            run_timestamp    TEXT    NOT NULL,
            instrument       TEXT    NOT NULL,
            timeframe        TEXT    NOT NULL,
            strategy_name    TEXT    NOT NULL,
            direction        TEXT    NOT NULL,
            entry_ref        REAL    NOT NULL,
            stop_distance    REAL    NOT NULL,
            target_distance  REAL    NOT NULL,
            oos_sharpe_mean  REAL    NOT NULL,
            quality_score    REAL    NOT NULL,
            rank             INTEGER NOT NULL,
            spread_ok        INTEGER NOT NULL,
            session_ok       INTEGER NOT NULL,
            news_flag        INTEGER NOT NULL,
            generated_at     TEXT    NOT NULL,
            PRIMARY KEY (run_timestamp, instrument, timeframe, strategy_name)
        )
    """

    #: SQL to insert one watchlist row (INSERT OR REPLACE for idempotent re-runs).
    _INSERT_WATCHLIST_SQL: str = """
        INSERT OR REPLACE INTO watchlist
            (run_timestamp, instrument, timeframe, strategy_name, direction,
             entry_ref, stop_distance, target_distance, oos_sharpe_mean,
             quality_score, rank, spread_ok, session_ok, news_flag, generated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    # ------------------------------------------------------------------
    # Execution tables (Phase 3 — order-placement owns this migration).
    # Column lists are pinned by docs/features/order-placement.md
    # (DRIFT-01/02).  All timestamps are UTC RFC 3339 TEXT (INV-03).
    # ------------------------------------------------------------------

    #: ``orders`` — the intent to open a bracketed position.  ``client_order_id``
    #: is the PK; the deterministic INV-15 idempotency key.
    _CREATE_ORDERS_SQL: str = """
        CREATE TABLE IF NOT EXISTS orders (
            client_order_id    TEXT    NOT NULL PRIMARY KEY,
            instrument         TEXT    NOT NULL,
            direction          TEXT    NOT NULL,
            units              INTEGER NOT NULL,
            stop_loss_price    REAL    NOT NULL,
            take_profit_price  REAL    NOT NULL,
            candidate_ref      TEXT    NOT NULL,
            created_at         TEXT    NOT NULL,
            status             TEXT    NOT NULL
        )
    """

    #: ``fills`` — the broker's confirmation, keyed by ``client_order_id`` so the
    #: pre-submit idempotency read is a single PK lookup (INV-15).  A rejected
    #: order still records a row (status="rejected") with no position.
    _CREATE_FILLS_SQL: str = """
        CREATE TABLE IF NOT EXISTS fills (
            client_order_id    TEXT    NOT NULL PRIMARY KEY,
            broker_trade_id    TEXT    NOT NULL,
            fill_price         REAL    NOT NULL,
            units_filled       INTEGER NOT NULL,
            slippage           REAL    NOT NULL,
            status             TEXT    NOT NULL,
            filled_at          TEXT    NOT NULL
        )
    """

    #: ``positions`` — open/closed bracketed position.  PK ``broker_trade_id``.
    #: ``realized_pl`` is nullable until close (written by reconciliation).
    _CREATE_POSITIONS_SQL: str = """
        CREATE TABLE IF NOT EXISTS positions (
            broker_trade_id    TEXT    NOT NULL PRIMARY KEY,
            instrument         TEXT    NOT NULL,
            units              INTEGER NOT NULL,
            entry_price        REAL    NOT NULL,
            stop_loss_price    REAL    NOT NULL,
            take_profit_price  REAL    NOT NULL,
            candidate_ref      TEXT    NOT NULL,
            opened_at          TEXT    NOT NULL,
            unrealized_pl      REAL    NOT NULL,
            closed_at          TEXT,
            realized_pl        REAL
        )
    """

    #: Insert one order row.  ``INSERT OR REPLACE`` keeps a retry of the same
    #: ``client_order_id`` idempotent at the store layer.
    _INSERT_ORDER_SQL: str = """
        INSERT OR REPLACE INTO orders
            (client_order_id, instrument, direction, units, stop_loss_price,
             take_profit_price, candidate_ref, created_at, status)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    #: Insert one fill row (idempotent on ``client_order_id``).
    _INSERT_FILL_SQL: str = """
        INSERT OR REPLACE INTO fills
            (client_order_id, broker_trade_id, fill_price, units_filled,
             slippage, status, filled_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
    """

    #: Insert one position row (idempotent on ``broker_trade_id``).
    _INSERT_POSITION_SQL: str = """
        INSERT OR REPLACE INTO positions
            (broker_trade_id, instrument, units, entry_price, stop_loss_price,
             take_profit_price, candidate_ref, opened_at, unrealized_pl,
             closed_at, realized_pl)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    #: SQL to upsert a single candle row (replace on PK conflict).
    _UPSERT_CANDLE_SQL: str = """
        INSERT OR REPLACE INTO candles
            (instrument, granularity, time,
             open_bid,  high_bid,  low_bid,  close_bid,
             open_ask,  high_ask,  low_ask,  close_ask,
             volume, complete)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    #: Backward-compat alias used by some test helpers (PoC era).
    _UPSERT_SQL: str = _UPSERT_CANDLE_SQL

    #: SQL to upsert instrument metadata (replace on name PK conflict).
    _UPSERT_INSTRUMENT_SQL: str = """
        INSERT OR REPLACE INTO instruments
            (name, pip_location, min_trade_size, margin_rate, display_precision,
             long_rate, short_rate, financing_days_of_week, fetched_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def __init__(
        self,
        db_path: str | Path,
        archive_dir: str | Path | None = None,
    ) -> None:
        self._db_path = str(db_path)
        # Derive archive_dir from db_path unless explicitly provided.
        self._archive_dir: Path | None
        if archive_dir is not None:
            self._archive_dir = Path(archive_dir)
        elif self._db_path == ":memory:":
            # For in-memory DBs, no default archive dir (caller must supply
            # archive_dir if they want Parquet operations).
            self._archive_dir = None
        else:
            self._archive_dir = Path(self._db_path).parent / "archive"

        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path,
            # Explicitly NOT using detect_types — we parse TEXT timestamps
            # ourselves (see library_defaults note in taskgraph).
        )
        self._create_tables()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create the ``candles``, ``instruments``, ``approved_set``, and
        ``watchlist`` tables if they do not already exist."""
        self._conn.execute(self._CREATE_CANDLES_SQL)
        self._conn.execute(self._CREATE_INSTRUMENTS_SQL)
        self._conn.execute(self._CREATE_APPROVED_SET_SQL)
        self._conn.execute(self._CREATE_WATCHLIST_SQL)
        self._conn.execute(self._CREATE_ORDERS_SQL)
        self._conn.execute(self._CREATE_FILLS_SQL)
        self._conn.execute(self._CREATE_POSITIONS_SQL)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Candle write
    # ------------------------------------------------------------------

    def upsert(self, rows: Iterable[CandleRow]) -> None:
        """Upsert one or more ``CandleRow`` objects into the store.

        Uses ``INSERT OR REPLACE`` so re-ingesting the same candle is
        idempotent (last-writer wins on any field update).

        Only rows with ``complete=True`` are stored — incomplete (half-formed)
        bars must not feed the backtester.

        Args:
            rows: An iterable of ``CandleRow`` instances to persist.
        """
        params = [
            (
                row.instrument,
                row.granularity,
                _to_rfc3339(row.time),     # TEXT, UTC RFC 3339 (INV-03)
                row.open_bid,
                row.high_bid,
                row.low_bid,
                row.close_bid,
                row.open_ask,
                row.high_ask,
                row.low_ask,
                row.close_ask,
                row.volume,
                int(row.complete),         # SQLite stores bool as INTEGER
            )
            for row in rows
            if row.complete                 # only complete candles
        ]
        if params:
            self._conn.executemany(self._UPSERT_CANDLE_SQL, params)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Candle read (SQLite)
    # ------------------------------------------------------------------

    def load_candles(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Load candles from SQLite for a given instrument/granularity/range.

        Timestamps are parsed explicitly from their TEXT representation using
        ``pd.to_datetime(..., utc=True)`` — we do not rely on sqlite3
        PARSE_DECLTYPES (library_defaults note in taskgraph).

        Args:
            instrument: OANDA instrument identifier, e.g. ``"EUR_USD"``.
            granularity: OANDA granularity string, e.g. ``"H1"``.
            start: Inclusive start of the range (UTC-aware).
            end: Inclusive end of the range (UTC-aware).

        Returns:
            A ``pd.DataFrame`` with columns::

                time (datetime64[ns, UTC])
                open_bid, high_bid, low_bid, close_bid   (float64)
                open_ask, high_ask, low_ask, close_ask   (float64)
                volume                                   (int64)

            Rows are sorted by ``time`` ascending.  If no rows match, an
            empty DataFrame with the same columns and dtypes is returned.

        Raises:
            ValueError: If ``start`` or ``end`` are not UTC-aware.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError(
                "start and end must be UTC-aware datetimes (INV-03)."
            )

        start_str = _to_rfc3339(start)
        end_str = _to_rfc3339(end)

        cursor = self._conn.execute(
            """
            SELECT time,
                   open_bid, high_bid, low_bid, close_bid,
                   open_ask, high_ask, low_ask, close_ask,
                   volume
            FROM   candles
            WHERE  instrument  = ?
              AND  granularity = ?
              AND  time >= ?
              AND  time <= ?
            ORDER  BY time ASC
            """,
            (instrument, granularity, start_str, end_str),
        )
        rows = cursor.fetchall()

        columns = [
            "time",
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
            "volume",
        ]

        if not rows:
            # Return an empty DataFrame with the correct schema and dtypes.
            df = pd.DataFrame(columns=columns)
            df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")
            float_cols = [
                "open_bid", "high_bid", "low_bid", "close_bid",
                "open_ask", "high_ask", "low_ask", "close_ask",
            ]
            df[float_cols] = df[float_cols].astype("float64")
            df["volume"] = df["volume"].astype("int64")
            return df

        df = pd.DataFrame(rows, columns=columns)

        # Parse TEXT timestamps explicitly to datetime64[ns, UTC] (INV-03).
        df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")

        float_cols = [
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
        ]
        df[float_cols] = df[float_cols].astype("float64")
        df["volume"] = df["volume"].astype("int64")

        return df

    def get_cached_times(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> set[str]:
        """Return the set of RFC 3339 time strings already in the store.

        Used by ``fetch_and_cache`` to identify gaps (missing rows) without
        loading full price data.

        Args:
            instrument: OANDA instrument identifier.
            granularity: OANDA granularity string.
            start: Inclusive range start (UTC-aware).
            end: Inclusive range end (UTC-aware).

        Returns:
            Set of RFC 3339 strings for rows that exist in the DB.
        """
        start_str = _to_rfc3339(start)
        end_str = _to_rfc3339(end)

        cursor = self._conn.execute(
            """
            SELECT time FROM candles
            WHERE  instrument  = ?
              AND  granularity = ?
              AND  time >= ?
              AND  time <= ?
            """,
            (instrument, granularity, start_str, end_str),
        )
        return {row[0] for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # Instrument metadata (SQLite)
    # ------------------------------------------------------------------

    def upsert_instruments(
        self,
        instruments: Iterable[InstrumentMeta],
        fetched_at: datetime | None = None,
    ) -> None:
        """Cache instrument metadata to the ``instruments`` SQLite table.

        Idempotent — re-fetching the universe simply replaces existing rows.

        Args:
            instruments: An iterable of ``InstrumentMeta`` instances.
            fetched_at: UTC-aware timestamp recording when the metadata was
                fetched.  Defaults to ``datetime.now(timezone.utc)`` if not
                provided.
        """
        ts = fetched_at or datetime.now(timezone.utc)
        ts_str = _to_rfc3339(ts)

        params = [
            (
                m.name,
                m.pip_location,
                m.min_trade_size,
                m.margin_rate,
                m.display_precision,
                m.long_rate,
                m.short_rate,
                json.dumps(m.financing_days_of_week),  # store list as JSON
                ts_str,
            )
            for m in instruments
        ]
        if params:
            self._conn.executemany(self._UPSERT_INSTRUMENT_SQL, params)
            self._conn.commit()

    def load_instruments(self) -> list[InstrumentMeta]:
        """Load all cached instrument metadata from SQLite.

        Returns:
            A list of ``InstrumentMeta`` instances, one per row.  Returns an
            empty list if no instruments have been cached yet.
        """
        cursor = self._conn.execute(
            """
            SELECT name, pip_location, min_trade_size, margin_rate,
                   display_precision, long_rate, short_rate,
                   financing_days_of_week
            FROM   instruments
            ORDER  BY name ASC
            """
        )
        rows = cursor.fetchall()
        result: list[InstrumentMeta] = []
        for row in rows:
            (
                name,
                pip_location,
                min_trade_size,
                margin_rate,
                display_precision,
                long_rate,
                short_rate,
                financing_days_json,
            ) = row
            result.append(
                InstrumentMeta(
                    name=name,
                    pip_location=pip_location,
                    min_trade_size=min_trade_size,
                    margin_rate=margin_rate,
                    display_precision=display_precision,
                    long_rate=long_rate,
                    short_rate=short_rate,
                    financing_days_of_week=json.loads(financing_days_json),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Approved-set table (INV-10 gate; INV-12 single-writer)
    # ------------------------------------------------------------------

    def write_approved_set(
        self,
        entries: Iterable["ApprovedSetEntry"],
        run_timestamp: datetime,
    ) -> int:
        """Persist a batch of approved-set entries in ONE transaction (INV-12).

        This is the single-writer commit point for the ``approved_set`` table.
        The full-universe backtest runner collects every ``ApprovedSetEntry``
        from its worker processes into a list (no worker touches the DB) and
        hands the complete batch here; this method performs all inserts inside
        one ``executemany`` + ``commit`` so the table is written atomically —
        either every approved combination lands or none does.  A partial write
        (which INV-10 could not distinguish from a legitimately small set) is
        therefore impossible.

        The DB-only ``run_timestamp`` column is supplied here at the
        persistence layer; the ``ApprovedSetEntry`` pydantic model is unchanged
        (audit DRIFT-03).  The same ``run_timestamp`` is stamped on every row of
        the batch.

        Args:
            entries: The complete batch of ``ApprovedSetEntry`` objects to
                persist.  May be empty (an empty approved set is a valid result
                — INV-10: empty means "no signals", not "all signals").
            run_timestamp: UTC-aware timestamp for this run, stored as RFC 3339
                TEXT on every row (INV-03).

        Returns:
            The number of rows written.
        """
        ts_str = _to_rfc3339(run_timestamp)
        params = [
            (
                ts_str,
                e.strategy_name,
                e.instrument,
                e.granularity,
                float(e.oos_sharpe_mean),
                int(e.oos_trade_count_total),
                1 if e.swap_modelled else 0,
            )
            for e in entries
        ]
        # Single transaction: executemany + one commit. Even an empty batch is
        # committed (a no-op) so the call site has uniform semantics.
        self._conn.executemany(self._INSERT_APPROVED_SET_SQL, params)
        self._conn.commit()
        return len(params)

    def load_approved_set(
        self,
        run_timestamp: datetime | None = None,
    ) -> list[dict[str, object]]:
        """Load approved-set rows (the INV-10 gate Phase 2 reads).

        Args:
            run_timestamp: If given, return only rows for that run (matched on
                the RFC 3339 string); otherwise return all rows.

        Returns:
            A list of dicts, one per row, with keys ``run_timestamp``,
            ``strategy_name``, ``instrument``, ``granularity``,
            ``oos_sharpe_mean``, ``oos_trade_count_total``, ``swap_modelled``
            (``swap_modelled`` coerced back to ``bool``).  Empty list when the
            table has no matching rows.
        """
        if run_timestamp is not None:
            cursor = self._conn.execute(
                """
                SELECT run_timestamp, strategy_name, instrument, granularity,
                       oos_sharpe_mean, oos_trade_count_total, swap_modelled
                FROM   approved_set
                WHERE  run_timestamp = ?
                ORDER  BY strategy_name, instrument, granularity
                """,
                (_to_rfc3339(run_timestamp),),
            )
        else:
            cursor = self._conn.execute(
                """
                SELECT run_timestamp, strategy_name, instrument, granularity,
                       oos_sharpe_mean, oos_trade_count_total, swap_modelled
                FROM   approved_set
                ORDER  BY run_timestamp, strategy_name, instrument, granularity
                """
            )
        result: list[dict[str, object]] = []
        for row in cursor.fetchall():
            result.append(
                {
                    "run_timestamp": row[0],
                    "strategy_name": row[1],
                    "instrument": row[2],
                    "granularity": row[3],
                    "oos_sharpe_mean": row[4],
                    "oos_trade_count_total": row[5],
                    "swap_modelled": bool(row[6]),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Watchlist table (Phase 2 — fathom scan persists here; fathom watchlist reads)
    # ------------------------------------------------------------------

    def write_watchlist(
        self,
        candidates: "list[Candidate]",
        run_timestamp: datetime,
    ) -> int:
        """Persist a ranked ``Candidate`` list from ``fathom scan`` (single tx).

        Mirrors the ``write_approved_set`` pattern: all rows for one scan run
        are inserted in ONE ``executemany`` + ``commit``.  ``INSERT OR REPLACE``
        keeps a re-run with the same ``run_timestamp`` idempotent.

        The INV-13 ``Candidate`` model is NOT modified; the mapping from model
        fields → table columns lives here at the persistence layer.

        Args:
            candidates: The ranked ``Candidate`` list from
                ``PortfolioLimiter.apply(Ranker.rank(now))``.  May be empty
                (INV-10: an empty scan is a valid result).
            run_timestamp: UTC-aware datetime for this scan run, stamped on
                every row (INV-03).

        Returns:
            The number of rows written (== ``len(candidates)``).
        """
        ts_str = _to_rfc3339(run_timestamp)
        params = [
            (
                ts_str,
                c.instrument,
                c.timeframe,
                c.strategy_name,
                c.direction,
                c.entry_ref,
                c.stop_distance,
                c.target_distance,
                c.oos_sharpe_mean,
                c.quality_score,
                c.rank,
                1 if c.spread_ok else 0,
                1 if c.session_ok else 0,
                1 if c.news_flag else 0,
                c.generated_at,
            )
            for c in candidates
        ]
        self._conn.executemany(self._INSERT_WATCHLIST_SQL, params)
        self._conn.commit()
        return len(params)

    def load_watchlist(
        self,
        run_timestamp: datetime | None = None,
    ) -> "list[dict[str, object]]":
        """Load watchlist rows as plain dicts (each maps to one ``Candidate``).

        Args:
            run_timestamp: If given, return only rows for that run; otherwise
                return rows for the **latest** run (highest ``run_timestamp``
                lexicographically — works because RFC 3339 strings sort
                correctly).

        Returns:
            A list of dicts keyed by the INV-13 ``Candidate`` field names
            (minus ``run_timestamp``, which is DB-internal).  Rows are
            ordered by ``rank`` ascending.  Empty list when the table has no
            matching rows.
        """
        if run_timestamp is not None:
            ts_str = _to_rfc3339(run_timestamp)
            cursor = self._conn.execute(
                """
                SELECT instrument, timeframe, strategy_name, direction,
                       entry_ref, stop_distance, target_distance,
                       oos_sharpe_mean, quality_score, rank,
                       spread_ok, session_ok, news_flag, generated_at
                FROM   watchlist
                WHERE  run_timestamp = ?
                ORDER  BY rank ASC
                """,
                (ts_str,),
            )
        else:
            # Latest run: MAX(run_timestamp) as a scalar, then fetch its rows.
            cursor = self._conn.execute(
                """
                SELECT instrument, timeframe, strategy_name, direction,
                       entry_ref, stop_distance, target_distance,
                       oos_sharpe_mean, quality_score, rank,
                       spread_ok, session_ok, news_flag, generated_at
                FROM   watchlist
                WHERE  run_timestamp = (SELECT MAX(run_timestamp) FROM watchlist)
                ORDER  BY rank ASC
                """
            )
        result: list[dict[str, object]] = []
        for row in cursor.fetchall():
            (
                instrument, timeframe, strategy_name, direction,
                entry_ref, stop_distance, target_distance,
                oos_sharpe_mean, quality_score, rank,
                spread_ok_int, session_ok_int, news_flag_int, generated_at,
            ) = row
            result.append(
                {
                    "instrument": instrument,
                    "timeframe": timeframe,
                    "strategy_name": strategy_name,
                    "direction": direction,
                    "entry_ref": float(entry_ref),
                    "stop_distance": float(stop_distance),
                    "target_distance": float(target_distance),
                    "oos_sharpe_mean": float(oos_sharpe_mean),
                    "quality_score": float(quality_score),
                    "rank": int(rank),
                    "spread_ok": bool(spread_ok_int),
                    "session_ok": bool(session_ok_int),
                    "news_flag": bool(news_flag_int),
                    "generated_at": generated_at,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Execution tables (Phase 3 — order-placement persists; reconciliation
    # later updates positions.realized_pl/closed_at).
    # ------------------------------------------------------------------

    def write_order(self, order: "Order", status: str) -> None:
        """Persist one ``Order`` row with its current ``status``.

        Idempotent on ``client_order_id`` (``INSERT OR REPLACE``): re-submitting
        the same order updates the stored status rather than duplicating it
        (INV-15).  Timestamps are written as UTC RFC 3339 TEXT (INV-03).

        Args:
            order: the ``Order`` being submitted/recorded.
            status: lifecycle status, e.g. ``"submitted"`` | ``"filled"`` |
                ``"partial"`` | ``"rejected"``.
        """
        self._conn.execute(
            self._INSERT_ORDER_SQL,
            (
                order.client_order_id,
                order.instrument,
                str(order.direction.value),
                int(order.units),
                float(order.stop_loss_price),
                float(order.take_profit_price),
                order.candidate_ref,
                _to_rfc3339(order.created_at),
                status,
            ),
        )
        self._conn.commit()

    def write_fill(self, fill: "Fill") -> None:
        """Persist one filled/partial ``Fill`` row (idempotent on
        ``client_order_id``).

        ``filled_at`` is stored as UTC RFC 3339 (INV-03).  A *rejected* order is
        not a valid ``Fill`` (the INV-14 model forbids an empty broker-trade-id
        and zero units) — use :meth:`write_rejection` for that so the broker's
        verdict is recorded faithfully without synthesising a fill.
        """
        self._conn.execute(
            self._INSERT_FILL_SQL,
            (
                fill.client_order_id,
                fill.broker_trade_id,
                float(fill.fill_price),
                int(fill.units_filled),
                float(fill.slippage),
                str(fill.status.value),
                _to_rfc3339(fill.filled_at),
            ),
        )
        self._conn.commit()

    def write_rejection(
        self,
        client_order_id: str,
        rejected_at: datetime,
        reason: str = "",
    ) -> None:
        """Record a broker rejection in ``fills`` without inventing a fill.

        A rejection is terminal: no ``Position`` is created and the
        ``Fill`` model cannot represent it (it requires a non-empty
        broker-trade-id and non-zero units).  We persist a row with
        ``status="rejected"``, ``broker_trade_id`` holding the (possibly empty)
        reason for audit, ``fill_price`` 0 and ``units_filled`` 0 — these are
        store-layer sentinels, never reconstructed into a ``Fill``
        (:meth:`get_fill_by_client_order_id` returns ``None`` for them).
        ``rejected_at`` is sourced from the caller's UTC clock (INV-03).
        """
        self._conn.execute(
            self._INSERT_FILL_SQL,
            (
                client_order_id,
                reason,
                0.0,
                0,
                0.0,
                "rejected",
                _to_rfc3339(rejected_at),
            ),
        )
        self._conn.commit()

    def write_position(self, position: "Position") -> None:
        """Persist one ``Position`` row (idempotent on ``broker_trade_id``).

        ``realized_pl`` / ``closed_at`` are nullable and remain ``NULL`` while
        the position is open — reconciliation writes them on close (INV-16).
        Timestamps are UTC RFC 3339 (INV-03).
        """
        self._conn.execute(
            self._INSERT_POSITION_SQL,
            (
                position.broker_trade_id,
                position.instrument,
                int(position.units),
                float(position.entry_price),
                float(position.stop_loss_price),
                float(position.take_profit_price),
                position.candidate_ref,
                _to_rfc3339(position.opened_at),
                float(position.unrealized_pl),
                _to_rfc3339(position.closed_at)
                if position.closed_at is not None
                else None,
                float(position.realized_pl)
                if position.realized_pl is not None
                else None,
            ),
        )
        self._conn.commit()

    def get_fill_by_client_order_id(self, client_order_id: str) -> "Fill | None":
        """Return the persisted *filled/partial* ``Fill`` for a
        ``client_order_id``, or ``None``.

        This is the pre-submit idempotency read (INV-15): ``submit_order`` calls
        it before any broker write; a hit short-circuits and returns the
        existing fill, so a retry or an operator re-run never opens a second
        position.  Reconstructs a validated ``Fill`` (the same frozen INV-14
        shape that was written).

        A *rejected* row (written by :meth:`write_rejection`) is **not** a valid
        ``Fill`` and is treated as absent here — it returns ``None`` so the
        idempotency check never resurrects a non-fill as a fill.  (A rejection
        is terminal; whether a re-run re-attempts is the caller's policy, and
        the duplicate ``clientExtensions.id`` guards the broker either way.)

        Args:
            client_order_id: the deterministic idempotency key.

        Returns:
            The reconstructed ``Fill`` if a filled/partial row exists, else
            ``None``.
        """
        from execution.models import Fill, FillStatus  # local: avoid import cycle

        cursor = self._conn.execute(
            """
            SELECT client_order_id, broker_trade_id, fill_price, units_filled,
                   slippage, status, filled_at
            FROM   fills
            WHERE  client_order_id = ?
              AND  status IN ('filled', 'partial')
            """,
            (client_order_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return Fill(
            client_order_id=row[0],
            broker_trade_id=row[1],
            fill_price=float(row[2]),
            units_filled=int(row[3]),
            slippage=float(row[4]),
            status=FillStatus(row[5]),
            filled_at=pd.to_datetime(row[6], utc=True).to_pydatetime(),
        )

    # ------------------------------------------------------------------
    # Parquet archive
    # ------------------------------------------------------------------

    def _parquet_path(self, instrument: str, granularity: str, date_str: str) -> Path:
        """Return the Parquet file path for a given instrument, granularity, and date.

        Layout: ``{archive_dir}/{instrument}/{granularity}/{date_str}.parquet``

        Including the granularity in the path prevents collisions between
        different granularities for the same instrument and date (e.g. H1 and
        H4 on the same calendar day would otherwise write to the same file).

        Args:
            instrument: OANDA instrument identifier, e.g. ``"EUR_USD"``.
            granularity: OANDA granularity string, e.g. ``"H1"``.
            date_str: Date string in ``YYYY-MM-DD`` format.

        Returns:
            Absolute ``Path`` for the Parquet file.

        Raises:
            RuntimeError: If ``archive_dir`` was not configured (in-memory DB
                with no explicit ``archive_dir`` supplied).
        """
        if self._archive_dir is None:
            raise RuntimeError(
                "No archive_dir configured.  Supply archive_dir= when "
                "constructing Store for an in-memory database."
            )
        return self._archive_dir / instrument / granularity / f"{date_str}.parquet"

    def write_parquet(
        self,
        instrument: str,
        granularity: str,
        df: pd.DataFrame,
    ) -> None:
        """Write a candle DataFrame to the Parquet archive.

        Each Parquet file covers one calendar date (UTC) for one instrument +
        granularity combination.  If a file for a given date already exists it
        is overwritten (idempotent).

        The DataFrame must contain a ``time`` column of dtype
        ``datetime64[ns, UTC]``.  The ``granularity`` is stored as a column
        so that files from different granularities partition correctly.

        Args:
            instrument: OANDA instrument identifier, e.g. ``"EUR_USD"``.
            granularity: OANDA granularity string, e.g. ``"H1"``.
            df: DataFrame in the ``load_candles`` contract (plus a ``time``
                column of dtype ``datetime64[ns, UTC]``).

        Raises:
            RuntimeError: If ``archive_dir`` was not configured.
            ValueError: If ``df`` does not contain a ``time`` column or
                ``time`` dtype is not timezone-aware UTC.
        """
        if "time" not in df.columns:
            raise ValueError("DataFrame must contain a 'time' column.")

        # Check archive_dir is configured before doing anything else.
        if self._archive_dir is None:
            raise RuntimeError(
                "No archive_dir configured.  Supply archive_dir= when "
                "constructing Store for an in-memory database."
            )

        if df.empty:
            return  # Nothing to write.

        # Drop the granularity column if present — it is encoded in the path.
        df = df.copy()
        if "granularity" in df.columns:
            df = df.drop(columns=["granularity"])

        # Group by UTC date and write one file per date.
        dates = df["time"].dt.date.unique()
        for date in dates:
            date_str = date.strftime("%Y-%m-%d")
            path = self._parquet_path(instrument, granularity, date_str)
            path.parent.mkdir(parents=True, exist_ok=True)

            day_df = df[df["time"].dt.date == date].copy()
            table = pa.Table.from_pandas(day_df, preserve_index=False)
            pq.write_table(table, path)  # type: ignore[no-untyped-call]

    def load_parquet(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Load candles from the Parquet archive for a given range.

        Reads only the daily Parquet files that overlap with [start, end],
        then filters to the exact range.  Returns an empty DataFrame (with
        the correct schema and dtypes) if no files exist for the range.

        The returned DataFrame has the same dtype contract as
        ``load_candles``: ``time`` is ``datetime64[ns, UTC]``, price columns
        are ``float64``, ``volume`` is ``int64`` — preserving the UTC
        timezone through the Parquet round-trip (INV-03, verified by tests).

        Args:
            instrument: OANDA instrument identifier.
            granularity: OANDA granularity string.
            start: Inclusive range start (UTC-aware).
            end: Inclusive range end (UTC-aware).

        Returns:
            ``pd.DataFrame`` matching the ``load_candles`` dtype contract.

        Raises:
            RuntimeError: If ``archive_dir`` was not configured.
            ValueError: If ``start`` or ``end`` are not UTC-aware.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError(
                "start and end must be UTC-aware datetimes (INV-03)."
            )

        if self._archive_dir is None:
            raise RuntimeError(
                "No archive_dir configured.  Supply archive_dir= when "
                "constructing Store for an in-memory database."
            )

        # Enumerate the date range to find candidate Parquet files.
        from datetime import timedelta

        start_date = start.date()
        end_date = end.date()
        current = start_date
        frames: list[pd.DataFrame] = []

        while current <= end_date:
            path = self._parquet_path(instrument, granularity, current.strftime("%Y-%m-%d"))
            if path.exists():
                day_df = pq.read_table(path).to_pandas()  # type: ignore[no-untyped-call]
                # Ensure the time column is datetime64[ns, UTC] (pyarrow
                # reads back with tz info but may use us resolution).
                day_df["time"] = pd.to_datetime(
                    day_df["time"], utc=True
                ).astype("datetime64[ns, UTC]")
                frames.append(day_df)
            current += timedelta(days=1)

        # Build the canonical column list (matches load_candles contract).
        columns = [
            "time",
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
            "volume",
        ]

        if not frames:
            df = pd.DataFrame(columns=columns)
            df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")
            float_cols = [c for c in columns if c not in ("time", "volume")]
            df[float_cols] = df[float_cols].astype("float64")
            df["volume"] = df["volume"].astype("int64")
            return df

        df = pd.concat(frames, ignore_index=True)

        # Filter to exact [start, end].  Granularity is encoded in the path, so
        # no additional column filter is needed.
        mask = (df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end))
        df = df[mask].copy()

        # Keep only the canonical columns.
        df = df[[c for c in columns if c in df.columns]].copy()

        # Coerce dtypes to match the load_candles contract.
        df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")
        float_cols = [
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
        ]
        existing_float = [c for c in float_cols if c in df.columns]
        df[existing_float] = df[existing_float].astype("float64")
        if "volume" in df.columns:
            df["volume"] = df["volume"].astype("int64")

        df = df.sort_values("time").reset_index(drop=True)
        return df
