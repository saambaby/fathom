"""Tests for data/store.py and data/candles.py.

AC coverage (POC-T-03):
- Round-trip: store → load → timestamps are UTC-aware and equal to originals.
- Cache-hit: second ``fetch_and_cache`` call issues zero client calls.
- Partial gap fill: only the missing sub-range triggers a client call.
- Schema: returned DataFrame has the correct columns and dtypes.
- complete=False candles are filtered out (not stored, not returned).
- Empty range: ``load_candles`` on an empty store returns an empty DataFrame.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data.candles import fetch_and_cache
from data.oanda_client import CandleRow
from data.store import Store, _to_rfc3339


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    """Shorthand for a UTC-aware datetime."""
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _make_candle(
    instrument: str,
    granularity: str,
    time: datetime,
    bid: float = 1.1000,
    ask: float = 1.1002,
    volume: int = 100,
    complete: bool = True,
) -> CandleRow:
    """Build a minimal ``CandleRow`` for testing."""
    return CandleRow(
        instrument=instrument,
        granularity=granularity,
        time=time,
        open_bid=bid,
        high_bid=bid + 0.0005,
        low_bid=bid - 0.0005,
        close_bid=bid + 0.0001,
        open_ask=ask,
        high_ask=ask + 0.0005,
        low_ask=ask - 0.0005,
        close_ask=ask + 0.0001,
        open_mid=(bid + ask) / 2,
        high_mid=(bid + ask) / 2 + 0.0005,
        low_mid=(bid + ask) / 2 - 0.0005,
        close_mid=(bid + ask) / 2 + 0.0001,
        volume=volume,
        complete=complete,
    )


@pytest.fixture()
def mem_store() -> Store:
    """An in-memory SQLite store, fresh for each test."""
    return Store(":memory:")


# ---------------------------------------------------------------------------
# Helper: mock OandaClient
# ---------------------------------------------------------------------------


def _mock_client(candles: list[CandleRow]) -> MagicMock:
    """Return a MagicMock OandaClient whose ``get_candles`` returns ``candles``."""
    client = MagicMock()
    client.get_candles.return_value = candles
    return client


# ---------------------------------------------------------------------------
# Store unit tests
# ---------------------------------------------------------------------------


class TestStoreRoundTrip:
    """Verify that timestamps survive the store → load round-trip as UTC-aware."""

    def test_timestamps_are_utc_aware_after_load(self, mem_store: Store) -> None:
        """INV-03: loaded timestamps must be UTC-aware (not naive)."""
        t1 = _utc(2024, 1, 15, 14)
        t2 = _utc(2024, 1, 15, 15)
        rows = [
            _make_candle("EUR_USD", "H1", t1),
            _make_candle("EUR_USD", "H1", t2),
        ]
        mem_store.upsert(rows)

        df = mem_store.load_candles(
            "EUR_USD", "H1",
            start=_utc(2024, 1, 15),
            end=_utc(2024, 1, 16),
        )

        assert len(df) == 2, "Expected two rows"
        # Verify dtype — datetime64[ns, UTC] means the series is tz-aware.
        assert str(df["time"].dtype) == "datetime64[ns, UTC]", (
            f"Expected datetime64[ns, UTC], got {df['time'].dtype}"
        )
        # Each individual timestamp must be UTC-aware.
        for ts in df["time"]:
            assert ts.tzinfo is not None, "Timestamp tzinfo must not be None"
            assert ts.tzinfo.utcoffset(None).total_seconds() == 0, (
                "Timestamp must be UTC (offset 0)"
            )

    def test_round_trip_values_intact(self, mem_store: Store) -> None:
        """Price and volume values are unchanged after a store→load cycle."""
        t = _utc(2024, 3, 20, 9)
        row = _make_candle("GBP_USD", "H1", t, bid=1.2700, ask=1.2703, volume=250)
        mem_store.upsert([row])

        df = mem_store.load_candles(
            "GBP_USD", "H1",
            start=_utc(2024, 3, 20),
            end=_utc(2024, 3, 21),
        )

        assert len(df) == 1
        loaded = df.iloc[0]
        # Time equality: loaded must equal original (same UTC instant).
        assert loaded["time"] == pd.Timestamp(t)
        assert abs(loaded["open_bid"] - 1.2700) < 1e-6
        assert abs(loaded["open_ask"] - 1.2703) < 1e-6
        assert loaded["volume"] == 250

    def test_stored_time_is_rfc3339_utc(self, mem_store: Store) -> None:
        """The raw TEXT value in SQLite must be a UTC RFC 3339 string (INV-03)."""
        t = _utc(2024, 6, 1, 0)
        mem_store.upsert([_make_candle("EUR_USD", "H1", t)])

        # Peek directly at the raw SQLite value.
        cursor = mem_store._conn.execute(
            "SELECT time FROM candles WHERE instrument='EUR_USD'"
        )
        raw = cursor.fetchone()[0]

        assert raw == "2024-06-01T00:00:00Z", (
            f"Expected RFC 3339 UTC string, got {raw!r}"
        )

    def test_incomplete_candles_not_stored(self, mem_store: Store) -> None:
        """``complete=False`` rows must be silently filtered by ``upsert``."""
        t = _utc(2024, 1, 15, 14)
        incomplete_row = _make_candle("EUR_USD", "H1", t, complete=False)
        mem_store.upsert([incomplete_row])

        df = mem_store.load_candles(
            "EUR_USD", "H1",
            start=_utc(2024, 1, 15),
            end=_utc(2024, 1, 16),
        )
        assert len(df) == 0, "Incomplete candles must not appear in load results"

    def test_empty_range_returns_empty_dataframe(self, mem_store: Store) -> None:
        """load_candles with no matching rows returns an empty DataFrame.

        The empty path must return exactly the same dtypes as the populated
        path — object columns would poison pd.concat and violate the AC dtype
        contract expected by T-05.
        """
        df = mem_store.load_candles(
            "EUR_USD", "H1",
            start=_utc(2020, 1, 1),
            end=_utc(2020, 1, 2),
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0
        # Schema must be correct even when empty.
        assert "time" in df.columns
        # Dtype contract: empty path must match populated path exactly.
        assert str(df["time"].dtype) == "datetime64[ns, UTC]", (
            f"time must be datetime64[ns, UTC], got {df['time'].dtype}"
        )
        float_price_cols = [
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
        ]
        for col in float_price_cols:
            assert df[col].dtype == "float64", (
                f"{col} must be float64 on empty DataFrame, got {df[col].dtype}"
            )
        assert df["volume"].dtype == "int64", (
            f"volume must be int64 on empty DataFrame, got {df['volume'].dtype}"
        )

    def test_upsert_idempotent(self, mem_store: Store) -> None:
        """Re-inserting the same candle does not create duplicate rows."""
        t = _utc(2024, 1, 15, 14)
        row = _make_candle("EUR_USD", "H1", t)
        mem_store.upsert([row])
        mem_store.upsert([row])  # second upsert of same row

        df = mem_store.load_candles(
            "EUR_USD", "H1",
            start=_utc(2024, 1, 15),
            end=_utc(2024, 1, 16),
        )
        assert len(df) == 1, "Upsert must be idempotent (no duplicates)"

    def test_load_requires_utc_aware_bounds(self, mem_store: Store) -> None:
        """``load_candles`` must raise ValueError for naive datetime bounds."""
        with pytest.raises(ValueError, match="UTC-aware"):
            mem_store.load_candles(
                "EUR_USD", "H1",
                start=datetime(2024, 1, 15),   # naive — no tzinfo
                end=datetime(2024, 1, 16),
            )


class TestStoreSchema:
    """Verify the DataFrame dtype contract for loaded candles."""

    def test_dataframe_columns_and_dtypes(self, mem_store: Store) -> None:
        """Returned DataFrame must have the AC-mandated columns and dtypes."""
        t = _utc(2024, 5, 1, 12)
        mem_store.upsert([_make_candle("USD_JPY", "D", t)])

        df = mem_store.load_candles(
            "USD_JPY", "D",
            start=_utc(2024, 5, 1),
            end=_utc(2024, 5, 2),
        )

        expected_columns = [
            "time",
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
            "volume",
        ]
        for col in expected_columns:
            assert col in df.columns, f"Missing column: {col}"

        assert str(df["time"].dtype) == "datetime64[ns, UTC]"
        for col in expected_columns[1:-1]:   # float price columns
            assert df[col].dtype == "float64", f"{col} must be float64"
        assert df["volume"].dtype == "int64"


# ---------------------------------------------------------------------------
# fetch_and_cache tests (mocked client — no HTTP)
# ---------------------------------------------------------------------------


class TestCacheHit:
    """A second fetch_and_cache for the same range must make zero HTTP calls."""

    def test_second_call_no_client_call(self, mem_store: Store) -> None:
        """Cache-hit AC: second call must not invoke ``client.get_candles``.

        The range is set so that [start, end] exactly matches what OANDA
        returns (start=t1, end=t3).  After the first fetch the store has all
        rows in the range, so the gap detector returns (None, None) on the
        second call and no HTTP request is issued.
        """
        t1 = _utc(2024, 2, 1, 0)
        t2 = _utc(2024, 2, 1, 1)
        t3 = _utc(2024, 2, 1, 2)

        candles = [
            _make_candle("EUR_USD", "H1", t1),
            _make_candle("EUR_USD", "H1", t2),
            _make_candle("EUR_USD", "H1", t3),
        ]
        client = _mock_client(candles)

        # Use the exact bounds of the returned data so no trailing gap exists.
        start = t1
        end = t3

        # First call — should call client once.
        df1 = fetch_and_cache(client, mem_store, "EUR_USD", "H1", start, end,
                              write_parquet=False)
        assert client.get_candles.call_count == 1

        # Second call — must NOT call client again.
        df2 = fetch_and_cache(client, mem_store, "EUR_USD", "H1", start, end,
                              write_parquet=False)
        assert client.get_candles.call_count == 1, (
            "Second call for the same range must make ZERO HTTP requests (cache hit)"
        )

        # Both calls return the same data.
        assert len(df1) == len(df2) == 3
        pd.testing.assert_frame_equal(df1.reset_index(drop=True),
                                      df2.reset_index(drop=True))

    def test_cache_hit_timestamps_utc_aware(self, mem_store: Store) -> None:
        """Timestamps returned on a cache hit must still be UTC-aware (INV-03)."""
        t = _utc(2024, 3, 15, 8)
        client = _mock_client([_make_candle("GBP_USD", "H1", t)])

        # Use exact bounds matching the single candle so no gap is detected.
        start = t
        end = t

        fetch_and_cache(client, mem_store, "GBP_USD", "H1", start, end,
                        write_parquet=False)
        df = fetch_and_cache(client, mem_store, "GBP_USD", "H1", start, end,
                             write_parquet=False)

        assert str(df["time"].dtype) == "datetime64[ns, UTC]"
        for ts in df["time"]:
            assert ts.tzinfo is not None


class TestPartialGapFill:
    """Only the missing sub-range should trigger a client call."""

    def test_trailing_gap_only_fetched(self, mem_store: Store) -> None:
        """When the leading portion is cached, only the trailing gap is fetched."""
        # Pre-populate the store with hours 0–3 of 2024-04-01.
        for h in range(4):
            mem_store.upsert([_make_candle("EUR_USD", "H1", _utc(2024, 4, 1, h))])

        # The full requested range is 0–5, but hours 0–3 are already in the store.
        # The client should be called for hours 4–5 only.
        hours_4_5 = [
            _make_candle("EUR_USD", "H1", _utc(2024, 4, 1, 4)),
            _make_candle("EUR_USD", "H1", _utc(2024, 4, 1, 5)),
        ]
        client = _mock_client(hours_4_5)

        df = fetch_and_cache(
            client, mem_store, "EUR_USD", "H1",
            start=_utc(2024, 4, 1, 0),
            end=_utc(2024, 4, 1, 5),
            write_parquet=False,
        )

        # Client was called exactly once (for the trailing gap).
        assert client.get_candles.call_count == 1

        # The from_time passed to get_candles should be > the last cached time.
        call_kwargs = client.get_candles.call_args
        from_time_arg = call_kwargs.kwargs.get(
            "from_time", call_kwargs.args[3] if len(call_kwargs.args) > 3 else None
        )
        # from_time must be UTC-aware and after the last pre-cached bar (hour 3).
        assert from_time_arg is not None
        assert from_time_arg > _utc(2024, 4, 1, 3), (
            "from_time should be set past the already-cached trailing edge"
        )

        # The result should contain all 6 hours (0–5).
        assert len(df) == 6

    def test_leading_gap_fetched(self, mem_store: Store) -> None:
        """When trailing portion is cached, only the leading gap is fetched."""
        # Pre-populate hours 4–5 of 2024-04-02.
        for h in [4, 5]:
            mem_store.upsert([_make_candle("EUR_USD", "H1", _utc(2024, 4, 2, h))])

        # The full requested range is 0–5, hours 4–5 are cached, 0–3 are missing.
        hours_0_3 = [
            _make_candle("EUR_USD", "H1", _utc(2024, 4, 2, h))
            for h in range(4)
        ]
        client = _mock_client(hours_0_3)

        df = fetch_and_cache(
            client, mem_store, "EUR_USD", "H1",
            start=_utc(2024, 4, 2, 0),
            end=_utc(2024, 4, 2, 5),
            write_parquet=False,
        )

        # Client was called exactly once (for the leading gap).
        assert client.get_candles.call_count == 1

        # The fetch must have been scoped to the missing leading sub-range,
        # not the whole requested range.  Inspect from_time: it must equal the
        # requested start (i.e. the gap begins at the start of the window).
        call_kwargs = client.get_candles.call_args
        from_time_arg = call_kwargs.kwargs.get(
            "from_time", call_kwargs.args[3] if len(call_kwargs.args) > 3 else None
        )
        assert from_time_arg is not None, "from_time must be passed to get_candles"
        assert from_time_arg == _utc(2024, 4, 2, 0), (
            f"from_time should be the requested start (leading gap begins there), "
            f"got {from_time_arg}"
        )
        # from_time must be strictly before the first cached bar (hour 4),
        # confirming the fetch was scoped to the missing sub-range.
        assert from_time_arg < _utc(2024, 4, 2, 4), (
            "from_time must be before the already-cached leading edge (hour 4) — "
            "fetch was not correctly scoped to the missing sub-range"
        )

        # Result contains all 6 rows.
        assert len(df) == 6

    def test_no_fetch_when_fully_cached(self, mem_store: Store) -> None:
        """If all rows are present, zero client calls are made."""
        times = [_utc(2024, 5, 10, h) for h in range(5)]
        for t in times:
            mem_store.upsert([_make_candle("USD_JPY", "H1", t)])

        client = _mock_client([])  # would raise if called unexpectedly

        df = fetch_and_cache(
            client, mem_store, "USD_JPY", "H1",
            start=_utc(2024, 5, 10, 0),
            end=_utc(2024, 5, 10, 4),
        )

        assert client.get_candles.call_count == 0
        assert len(df) == 5

    def test_fetch_and_cache_filters_incomplete(self, mem_store: Store) -> None:
        """complete=False candles from OANDA must not appear in the output."""
        complete_row = _make_candle("EUR_USD", "H1", _utc(2024, 6, 1, 0), complete=True)
        incomplete_row = _make_candle("EUR_USD", "H1", _utc(2024, 6, 1, 1), complete=False)
        client = _mock_client([complete_row, incomplete_row])

        df = fetch_and_cache(
            client, mem_store, "EUR_USD", "H1",
            start=_utc(2024, 6, 1),
            end=_utc(2024, 6, 2),
            write_parquet=False,
        )

        # Only the complete candle should appear.
        assert len(df) == 1
        ts = df["time"].iloc[0]
        assert ts == pd.Timestamp(_utc(2024, 6, 1, 0))


class TestFetchAndCacheValidation:
    """Edge-case validation for fetch_and_cache."""

    def test_naive_start_raises(self, mem_store: Store) -> None:
        client = _mock_client([])
        with pytest.raises(ValueError, match="UTC-aware"):
            fetch_and_cache(
                client, mem_store, "EUR_USD", "H1",
                start=datetime(2024, 1, 1),    # naive
                end=_utc(2024, 1, 2),
            )

    def test_naive_end_raises(self, mem_store: Store) -> None:
        client = _mock_client([])
        with pytest.raises(ValueError, match="UTC-aware"):
            fetch_and_cache(
                client, mem_store, "EUR_USD", "H1",
                start=_utc(2024, 1, 1),
                end=datetime(2024, 1, 2),       # naive
            )

    def test_returned_dataframe_columns(self, mem_store: Store) -> None:
        """Returned DataFrame has the AC-mandated column set."""
        t = _utc(2024, 7, 4, 12)
        client = _mock_client([_make_candle("GBP_USD", "H1", t)])

        df = fetch_and_cache(
            client, mem_store, "GBP_USD", "H1",
            start=_utc(2024, 7, 4),
            end=_utc(2024, 7, 5),
            write_parquet=False,
        )

        required = {
            "time",
            "open_bid", "high_bid", "low_bid", "close_bid",
            "open_ask", "high_ask", "low_ask", "close_ask",
            "volume",
        }
        assert required.issubset(set(df.columns)), (
            f"Missing columns: {required - set(df.columns)}"
        )
        assert str(df["time"].dtype) == "datetime64[ns, UTC]"
