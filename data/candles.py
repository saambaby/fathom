"""Candle fetch-and-cache logic.

Scope: Phase 1 (P1A-T-01 data-layer-expansion; extends PoC POC-T-03).

This module is the single entry point for obtaining candle data.  It is
gap-aware: it inspects the SQLite store first and only calls OANDA for
rows that are not already cached.  A second call for the same range makes
ZERO HTTP requests (cache-hit path).

Dual-write: ``fetch_and_cache`` now writes through to both the SQLite
operational store (for gap detection) and the Parquet archive (for bulk
research scans).  The return contract — a ``pd.DataFrame`` — is unchanged.

D-02: data is returned as ``pd.DataFrame`` with ``time`` dtype
    ``datetime64[ns, UTC]``.
INV-03: all timestamps are UTC RFC 3339.  ``start`` / ``end`` must be
    UTC-aware; the function raises ``ValueError`` otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from data.oanda_client import CandleRow, OandaClient
from data.store import Store, _to_rfc3339


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


def _expected_times_for_range(
    client: OandaClient,
    store: Store,
    instrument: str,
    granularity: str,
    start: datetime,
    end: datetime,
) -> tuple[datetime | None, datetime | None]:
    """Determine the sub-range of [start, end] that is not yet cached.

    Strategy:
    - Ask the store for all time strings it holds in [start, end].
    - If the store is empty for this range, the full range is the gap.
    - If the store has data, find the earliest start gap and the latest
      end gap:
        * gap_start: None if the store has at least one row at ``start``,
          else ``start``.
        * gap_end: None if the store has at least one row at ``end``,
          else ``end``.

    For simplicity the gap is defined as a single contiguous block:
    [first missing start .. last missing end].  OANDA pagination and
    upsert idempotency mean that re-fetching a partial overlap is safe —
    duplicate rows are silently replaced.

    Returns:
        A ``(fetch_start, fetch_end)`` pair.  Both are ``None`` if the
        entire requested range is already cached (no fetch needed).
    """
    cached = store.get_cached_times(instrument, granularity, start, end)

    if not cached:
        # Nothing cached — fetch the entire range.
        return start, end

    # Convert cached strings back to UTC datetimes for comparison.
    cached_dts = {
        datetime.fromisoformat(t.rstrip("Z")).replace(tzinfo=timezone.utc)
        for t in cached
    }

    # Find min and max of what is already cached.
    min_cached = min(cached_dts)
    max_cached = max(cached_dts)

    # We need to fetch any leading gap (start .. min_cached - 1 bar)
    # and any trailing gap (max_cached + 1 bar .. end).
    # Because bar sizes vary (H1, D, etc.) and we cannot know the exact
    # bar boundary without fetching, we use the cached min/max as pivots:
    #   - If min_cached > start, there is a leading gap.
    #   - If max_cached < end, there is a trailing gap.
    # We collapse both into a single fetch of [gap_start, gap_end] where:
    #   gap_start = start (if leading gap) else None
    #   gap_end   = end   (if trailing gap) else None
    # If both exist we fetch [start, end] wholesale (safe — upsert is
    # idempotent and overlapping rows are simply re-written).

    has_leading = min_cached > start
    has_trailing = max_cached < end

    if has_leading and has_trailing:
        return start, end
    if has_leading:
        return start, min_cached - timedelta(seconds=1)
    if has_trailing:
        return max_cached + timedelta(seconds=1), end

    # Entire range is cached.
    return None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_and_cache(
    client: OandaClient,
    store: Store,
    instrument: str,
    granularity: str,
    start: datetime,
    end: datetime,
    write_parquet: bool = True,
) -> pd.DataFrame:
    """Fetch candles for [start, end], cache them, and return as DataFrame.

    Gap-aware: only the rows missing from the SQLite store are fetched from
    OANDA.  A second call for the same ``(instrument, granularity, start,
    end)`` makes ZERO HTTP requests — all data comes from the cache.

    Only ``complete=True`` candles are stored and returned.

    Dual-write: fetched rows are written to both the SQLite operational
    store (source of truth for gap detection) and the Parquet archive
    (columnar bulk store for research scans), unless ``write_parquet=False``.

    Args:
        client: Initialised ``OandaClient``.
        store: Initialised ``Store`` instance.
        instrument: OANDA instrument identifier, e.g. ``"EUR_USD"``.
        granularity: OANDA granularity string, e.g. ``"H1"`` or ``"D"``.
        start: Inclusive range start, UTC-aware.
        end: Inclusive range end, UTC-aware.
        write_parquet: If ``True`` (default) also write newly-fetched rows
            to the Parquet archive.  Set to ``False`` to skip (e.g. for
            tests that do not configure an archive directory).

    Returns:
        ``pd.DataFrame`` with columns::

            time (datetime64[ns, UTC])
            open_bid, high_bid, low_bid, close_bid   (float64)
            open_ask, high_ask, low_ask, close_ask   (float64)
            volume                                   (int64)

        Rows are sorted by ``time`` ascending and cover the full [start, end]
        range (modulo any gaps in OANDA's own data history).

    Raises:
        ValueError: If ``start`` or ``end`` are not UTC-aware.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError(
            "start and end must be UTC-aware datetimes (INV-03). "
            "Use datetime(..., tzinfo=timezone.utc)."
        )

    # Determine which sub-range (if any) needs to be fetched from OANDA.
    fetch_start, fetch_end = _expected_times_for_range(
        client, store, instrument, granularity, start, end
    )

    if fetch_start is not None and fetch_end is not None:
        # Calculate an upper-bound count.  For H1, max 2 years ≈ 17,520 bars.
        # For D, max 2 years ≈ 730 bars.  We pass a generous upper bound;
        # OandaClient paginates automatically and stops when OANDA returns
        # no more data.
        #
        # We cannot know the exact bar count without knowing the granularity's
        # period length, so we use a large sentinel (50_000) that will never
        # be exceeded in practice for a 2-year PoC window.
        rows: list[CandleRow] = client.get_candles(
            instrument=instrument,
            granularity=granularity,
            count=50_000,
            from_time=fetch_start,
        )

        # Filter to the requested end boundary and complete-only.
        # (complete filtering is also done in store.upsert, but we do it
        # here too so the subsequent load_candles is not confused by rows
        # that were fetched but not stored.)
        rows_in_range = [
            r for r in rows
            if r.complete and r.time <= end
        ]

        store.upsert(rows_in_range)

        # Dual-write: also persist to Parquet archive for research scans.
        if write_parquet and rows_in_range:
            df_new = store.load_candles(instrument, granularity, start, end)
            store.write_parquet(instrument, granularity, df_new)

    # Return the full requested range from the store (single source of truth).
    return store.load_candles(instrument, granularity, start, end)
