"""SQLite persistence layer for candle data.

Scope: PoC (POC-T-03). Single table: ``candles``.

INV-03 compliance: ``time`` values are stored as UTC RFC 3339 TEXT strings
    (e.g. ``"2024-01-15T14:00:00Z"``).  They are never stored as Unix epoch
    integers or local-time strings.  On load, they are parsed explicitly with
    ``pd.to_datetime(..., utc=True)`` — we do NOT rely on sqlite3 PARSE_DECLTYPES.

D-02: all data is returned as ``pd.DataFrame`` with ``time`` dtype
    ``datetime64[ns, UTC]``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from data.oanda_client import CandleRow


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
    """SQLite-backed candle store.

    Creates and manages a ``candles`` table with a composite primary key
    ``(instrument, granularity, time)``.

    Args:
        db_path: File path for the SQLite database.  Pass ``":memory:"`` for
            an in-memory database (useful in tests).
    """

    #: SQL to create the candles table if it does not already exist.
    _CREATE_TABLE_SQL: str = """
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

    #: SQL to upsert a single candle row (replace on PK conflict).
    _UPSERT_SQL: str = """
        INSERT OR REPLACE INTO candles
            (instrument, granularity, time,
             open_bid,  high_bid,  low_bid,  close_bid,
             open_ask,  high_ask,  low_ask,  close_ask,
             volume, complete)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path,
            # Explicitly NOT using detect_types — we parse TEXT timestamps
            # ourselves (see library_defaults note in taskgraph).
        )
        self._create_table()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _create_table(self) -> None:
        """Create the ``candles`` table if it does not already exist."""
        self._conn.execute(self._CREATE_TABLE_SQL)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Write
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
            if row.complete                 # only complete candles (T-03 AC)
        ]
        if params:
            self._conn.executemany(self._UPSERT_SQL, params)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Read
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
            # Return an empty DataFrame with the correct schema.
            df = pd.DataFrame(columns=columns)
            df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")
            return df

        df = pd.DataFrame(rows, columns=columns)

        # Parse TEXT timestamps explicitly to datetime64[ns, UTC] (INV-03).
        # pd.to_datetime defaults to timezone-unaware — must pass utc=True.
        # Coerce to nanosecond resolution to match the AC dtype contract
        # (pandas 2+ defaults to microseconds; we normalise to ns here).
        df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")

        # Ensure numeric columns have correct dtypes.
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
