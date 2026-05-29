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

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data.oanda_client import CandleRow, InstrumentMeta


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
        """Create the ``candles`` and ``instruments`` tables if needed."""
        self._conn.execute(self._CREATE_CANDLES_SQL)
        self._conn.execute(self._CREATE_INSTRUMENTS_SQL)
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
