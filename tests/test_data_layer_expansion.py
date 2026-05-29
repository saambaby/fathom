"""Tests for P1A-T-01 data-layer-expansion.

AC coverage:
- ``list_instruments()`` returns validated ``InstrumentMeta`` (mocked OANDA).
- ``pip_location`` correct for JPY pairs (−2) vs majors (−4).
- Parquet round-trip preserves ``datetime64[ns, UTC]`` + float64/int64.
- ``upsert_instruments`` + ``load_instruments`` round-trips metadata via SQLite.
- ``fetch_and_cache`` with ``write_parquet=True`` writes to Parquet archive.
- Gap-aware multi-pair fetch makes no redundant HTTP calls (cache-hit).
- ``OandaAPIError`` is raised on 4xx/5xx from ``list_instruments``.
- No OANDA token appears in any raised exception message (INV-08).
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from pydantic import SecretStr

from config.settings import Settings
from data.candles import fetch_and_cache
from data.oanda_client import (
    InstrumentMeta,
    OandaAPIError,
    OandaClient,
    _instrument_from_raw,
)
from data.store import Store


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _make_settings() -> Settings:
    return Settings(
        env="demo",
        oanda_api_token=SecretStr("test-token-never-logged"),
        oanda_account_id="101-001-12345678-001",
    )


def _raw_instrument(
    name: str = "EUR_USD",
    pip_location: int = -4,
    margin_rate: str = "0.02",
    display_precision: int = 5,
    min_trade_size: str = "1",
    long_rate: str = "-0.0002",
    short_rate: str = "0.0001",
    days_of_week: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal OANDA instruments response entry."""
    if days_of_week is None:
        days_of_week = [
            {"dayOfWeek": "WEDNESDAY", "daysCharged": 3},
            {"dayOfWeek": "FRIDAY", "daysCharged": 1},
        ]
    return {
        "name": name,
        "type": "CURRENCY",
        "pipLocation": pip_location,
        "marginRate": margin_rate,
        "displayPrecision": display_precision,
        "minimumTradeSize": min_trade_size,
        "financing": {
            "longRate": long_rate,
            "shortRate": short_rate,
            "financingDaysOfWeek": days_of_week,
        },
    }


def _make_candle_row(
    instrument: str,
    granularity: str,
    t: datetime,
    bid: float = 1.1000,
    ask: float = 1.1002,
    volume: int = 100,
) -> Any:
    from data.oanda_client import CandleRow

    return CandleRow(
        instrument=instrument,
        granularity=granularity,
        time=t,
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
        complete=True,
    )


# ---------------------------------------------------------------------------
# InstrumentMeta model tests
# ---------------------------------------------------------------------------


class TestInstrumentMeta:
    def test_basic_construction(self) -> None:
        m = InstrumentMeta(
            name="EUR_USD",
            pip_location=-4,
            min_trade_size=1.0,
            margin_rate=0.02,
            display_precision=5,
            long_rate=-0.0002,
            short_rate=0.0001,
            financing_days_of_week=[2, 4],
        )
        assert m.name == "EUR_USD"
        assert m.pip_location == -4
        assert m.long_rate == pytest.approx(-0.0002)
        assert m.short_rate == pytest.approx(0.0001)
        assert m.financing_days_of_week == [2, 4]

    def test_string_coercion_for_rates(self) -> None:
        """OANDA returns rates as decimal strings; they must be coerced."""
        m = InstrumentMeta(
            name="USD_JPY",
            pip_location=-2,
            min_trade_size="1",
            margin_rate="0.02",
            display_precision=3,
            long_rate="-0.00015",
            short_rate="0.00010",
            financing_days_of_week=[2],
        )
        assert isinstance(m.long_rate, float)
        assert isinstance(m.short_rate, float)
        assert isinstance(m.min_trade_size, float)
        assert isinstance(m.margin_rate, float)

    def test_pip_location_jpy(self) -> None:
        """JPY pairs must have pip_location == −2."""
        raw = _raw_instrument(name="USD_JPY", pip_location=-2)
        m = _instrument_from_raw(raw)
        assert m.pip_location == -2

    def test_pip_location_major(self) -> None:
        """EUR_USD must have pip_location == −4."""
        raw = _raw_instrument(name="EUR_USD", pip_location=-4)
        m = _instrument_from_raw(raw)
        assert m.pip_location == -4

    def test_financing_days_parsed(self) -> None:
        """financingDaysOfWeek dicts must map to int weekday numbers."""
        raw = _raw_instrument(
            days_of_week=[
                {"dayOfWeek": "WEDNESDAY", "daysCharged": 3},
                {"dayOfWeek": "FRIDAY", "daysCharged": 1},
            ]
        )
        m = _instrument_from_raw(raw)
        # WEDNESDAY = 2, FRIDAY = 4
        assert 2 in m.financing_days_of_week
        assert 4 in m.financing_days_of_week

    def test_unknown_day_ignored(self) -> None:
        """Unknown day strings should be silently dropped."""
        raw = _raw_instrument(
            days_of_week=[{"dayOfWeek": "HOLIDAY", "daysCharged": 1}]
        )
        m = _instrument_from_raw(raw)
        assert m.financing_days_of_week == []


# ---------------------------------------------------------------------------
# OandaClient.list_instruments tests
# ---------------------------------------------------------------------------


class TestListInstruments:
    def _mock_client_with_response(
        self,
        instruments: list[dict[str, Any]],
    ) -> OandaClient:
        """Build an OandaClient whose _api.request returns the given payload."""
        settings = _make_settings()
        client = OandaClient.__new__(OandaClient)
        client._settings = settings

        mock_api = MagicMock()
        mock_api.request.return_value = {"instruments": instruments}
        client._api = mock_api
        return client

    def test_returns_list_of_instrument_meta(self) -> None:
        instruments = [
            _raw_instrument("EUR_USD", pip_location=-4),
            _raw_instrument("USD_JPY", pip_location=-2),
        ]
        client = self._mock_client_with_response(instruments)
        result = client.list_instruments()
        assert len(result) == 2
        assert all(isinstance(m, InstrumentMeta) for m in result)

    def test_filters_non_currency_types(self) -> None:
        """Non-CURRENCY instruments (CFDs, metals) must be excluded."""
        instruments = [
            _raw_instrument("EUR_USD"),
            {**_raw_instrument("BCO_USD"), "type": "CFD"},
            {**_raw_instrument("XAU_USD"), "type": "METAL"},
        ]
        client = self._mock_client_with_response(instruments)
        result = client.list_instruments()
        assert len(result) == 1
        assert result[0].name == "EUR_USD"

    def test_pip_location_jpy_vs_major(self) -> None:
        instruments = [
            _raw_instrument("EUR_USD", pip_location=-4),
            _raw_instrument("USD_JPY", pip_location=-2),
        ]
        client = self._mock_client_with_response(instruments)
        result = client.list_instruments()
        by_name = {m.name: m for m in result}
        assert by_name["EUR_USD"].pip_location == -4
        assert by_name["USD_JPY"].pip_location == -2

    def test_raises_oanda_api_error_on_4xx(self) -> None:
        from oandapyV20.exceptions import V20Error

        settings = _make_settings()
        client = OandaClient.__new__(OandaClient)
        client._settings = settings

        mock_api = MagicMock()
        mock_api.request.side_effect = V20Error(401, "Unauthorised")
        client._api = mock_api

        with pytest.raises(OandaAPIError) as exc_info:
            client.list_instruments()
        assert exc_info.value.status_code == 401

    def test_token_not_in_exception_message(self) -> None:
        """INV-08: the API token must never appear in exception messages."""
        from oandapyV20.exceptions import V20Error

        token = "secret-token-12345"
        settings = Settings(
            env="demo",
            oanda_api_token=SecretStr(token),
            oanda_account_id="101-001-99999-001",
        )
        client = OandaClient.__new__(OandaClient)
        client._settings = settings

        mock_api = MagicMock()
        mock_api.request.side_effect = V20Error(403, "Forbidden")
        client._api = mock_api

        with pytest.raises(OandaAPIError) as exc_info:
            client.list_instruments()
        assert token not in str(exc_info.value)

    def test_empty_response(self) -> None:
        """Empty instruments list is valid (no instruments on account)."""
        client = self._mock_client_with_response([])
        result = client.list_instruments()
        assert result == []


# ---------------------------------------------------------------------------
# Store instrument metadata tests
# ---------------------------------------------------------------------------


class TestStoreInstruments:
    def _store(self) -> Store:
        return Store(":memory:")

    def test_upsert_and_load_roundtrip(self) -> None:
        store = self._store()
        instruments = [
            InstrumentMeta(
                name="EUR_USD",
                pip_location=-4,
                min_trade_size=1.0,
                margin_rate=0.02,
                display_precision=5,
                long_rate=-0.0002,
                short_rate=0.0001,
                financing_days_of_week=[2, 4],
            ),
            InstrumentMeta(
                name="USD_JPY",
                pip_location=-2,
                min_trade_size=1.0,
                margin_rate=0.02,
                display_precision=3,
                long_rate=-0.00015,
                short_rate=0.0001,
                financing_days_of_week=[2],
            ),
        ]
        store.upsert_instruments(instruments)
        loaded = store.load_instruments()
        assert len(loaded) == 2
        by_name = {m.name: m for m in loaded}
        assert by_name["EUR_USD"].pip_location == -4
        assert by_name["USD_JPY"].pip_location == -2
        assert by_name["EUR_USD"].financing_days_of_week == [2, 4]

    def test_upsert_is_idempotent(self) -> None:
        store = self._store()
        instr = InstrumentMeta(
            name="GBP_USD",
            pip_location=-4,
            min_trade_size=1.0,
            margin_rate=0.02,
            display_precision=5,
            long_rate=-0.0003,
            short_rate=0.0002,
            financing_days_of_week=[2],
        )
        store.upsert_instruments([instr])
        # Upsert again with updated rate.
        instr2 = instr.model_copy(update={"long_rate": -0.0004})
        store.upsert_instruments([instr2])
        loaded = store.load_instruments()
        assert len(loaded) == 1
        assert loaded[0].long_rate == pytest.approx(-0.0004)

    def test_empty_store_returns_empty_list(self) -> None:
        store = self._store()
        assert store.load_instruments() == []

    def test_float_types_preserved(self) -> None:
        store = self._store()
        instr = InstrumentMeta(
            name="EUR_USD",
            pip_location=-4,
            min_trade_size=1.0,
            margin_rate=0.02,
            display_precision=5,
            long_rate=-0.0002,
            short_rate=0.0001,
            financing_days_of_week=[2],
        )
        store.upsert_instruments([instr])
        loaded = store.load_instruments()[0]
        assert isinstance(loaded.long_rate, float)
        assert isinstance(loaded.short_rate, float)
        assert isinstance(loaded.margin_rate, float)
        assert isinstance(loaded.min_trade_size, float)


# ---------------------------------------------------------------------------
# Parquet round-trip tests (INV-03 dtype contract)
# ---------------------------------------------------------------------------


class TestParquetRoundTrip:
    def test_datetime_tz_preserved(self, tmp_path: Path) -> None:
        """Parquet round-trip must preserve datetime64[ns, UTC] (INV-03)."""
        store = Store(":memory:", archive_dir=tmp_path)
        times = pd.to_datetime(
            [
                "2024-01-15T14:00:00Z",
                "2024-01-15T15:00:00Z",
                "2024-01-15T16:00:00Z",
            ],
            utc=True,
        ).astype("datetime64[ns, UTC]")

        df = pd.DataFrame(
            {
                "time": times,
                "open_bid": [1.08500, 1.08510, 1.08520],
                "high_bid": [1.08600, 1.08610, 1.08620],
                "low_bid": [1.08400, 1.08410, 1.08420],
                "close_bid": [1.08550, 1.08560, 1.08570],
                "open_ask": [1.08502, 1.08512, 1.08522],
                "high_ask": [1.08602, 1.08612, 1.08622],
                "low_ask": [1.08402, 1.08412, 1.08422],
                "close_ask": [1.08552, 1.08562, 1.08572],
                "volume": [100, 200, 150],
            }
        )

        store.write_parquet("EUR_USD", "H1", df)
        loaded = store.load_parquet(
            "EUR_USD",
            "H1",
            start=_utc(2024, 1, 15, 14),
            end=_utc(2024, 1, 15, 16),
        )

        assert str(loaded["time"].dtype) == "datetime64[ns, UTC]"
        assert loaded["time"].dt.tz is not None
        # Confirm the timezone is UTC
        assert str(loaded["time"].dt.tz) == "UTC"

    def test_float64_and_int64_dtypes_preserved(self, tmp_path: Path) -> None:
        """Float columns must be float64; volume must be int64."""
        store = Store(":memory:", archive_dir=tmp_path)
        times = pd.to_datetime(["2024-02-01T00:00:00Z"], utc=True).astype(
            "datetime64[ns, UTC]"
        )
        df = pd.DataFrame(
            {
                "time": times,
                "open_bid": [1.1000],
                "high_bid": [1.1010],
                "low_bid": [1.0990],
                "close_bid": [1.1005],
                "open_ask": [1.1002],
                "high_ask": [1.1012],
                "low_ask": [1.0992],
                "close_ask": [1.1007],
                "volume": [500],
            }
        )
        df["volume"] = df["volume"].astype("int64")

        store.write_parquet("GBP_USD", "H4", df)
        loaded = store.load_parquet(
            "GBP_USD",
            "H4",
            start=_utc(2024, 2, 1),
            end=_utc(2024, 2, 1, 23),
        )

        for col in ["open_bid", "high_bid", "low_bid", "close_bid",
                    "open_ask", "high_ask", "low_ask", "close_ask"]:
            assert loaded[col].dtype == "float64", f"Expected float64 for {col}"
        assert loaded["volume"].dtype == "int64"

    def test_values_round_trip_correctly(self, tmp_path: Path) -> None:
        """Write then read must produce identical values."""
        store = Store(":memory:", archive_dir=tmp_path)
        times = pd.to_datetime(
            ["2024-03-10T10:00:00Z", "2024-03-10T11:00:00Z"],
            utc=True,
        ).astype("datetime64[ns, UTC]")
        df = pd.DataFrame(
            {
                "time": times,
                "open_bid": [1.28100, 1.28200],
                "high_bid": [1.28150, 1.28250],
                "low_bid": [1.28050, 1.28150],
                "close_bid": [1.28120, 1.28220],
                "open_ask": [1.28102, 1.28202],
                "high_ask": [1.28152, 1.28252],
                "low_ask": [1.28052, 1.28152],
                "close_ask": [1.28122, 1.28222],
                "volume": [300, 400],
            }
        )

        store.write_parquet("GBP_USD", "H1", df)
        loaded = store.load_parquet(
            "GBP_USD",
            "H1",
            start=_utc(2024, 3, 10, 10),
            end=_utc(2024, 3, 10, 11),
        )

        assert len(loaded) == 2
        assert list(loaded["open_bid"]) == pytest.approx([1.28100, 1.28200])
        assert list(loaded["volume"]) == [300, 400]

    def test_multi_date_partitioning(self, tmp_path: Path) -> None:
        """Data spanning multiple dates must be split into per-date Parquet files."""
        store = Store(":memory:", archive_dir=tmp_path)
        times = pd.to_datetime(
            [
                "2024-04-01T22:00:00Z",  # April 1
                "2024-04-02T00:00:00Z",  # April 2
                "2024-04-02T01:00:00Z",  # April 2
            ],
            utc=True,
        ).astype("datetime64[ns, UTC]")
        df = pd.DataFrame(
            {
                "time": times,
                "open_bid": [1.0, 2.0, 3.0],
                "high_bid": [1.1, 2.1, 3.1],
                "low_bid": [0.9, 1.9, 2.9],
                "close_bid": [1.05, 2.05, 3.05],
                "open_ask": [1.01, 2.01, 3.01],
                "high_ask": [1.11, 2.11, 3.11],
                "low_ask": [0.91, 1.91, 2.91],
                "close_ask": [1.06, 2.06, 3.06],
                "volume": [100, 200, 300],
            }
        )

        store.write_parquet("EUR_USD", "H1", df)

        # Check files exist for both dates under the granularity sub-directory.
        assert (tmp_path / "EUR_USD" / "H1" / "2024-04-01.parquet").exists()
        assert (tmp_path / "EUR_USD" / "H1" / "2024-04-02.parquet").exists()

    def test_load_parquet_empty_range(self, tmp_path: Path) -> None:
        """load_parquet on a range with no Parquet files returns empty DataFrame."""
        store = Store(":memory:", archive_dir=tmp_path)
        loaded = store.load_parquet(
            "EUR_USD", "H1", start=_utc(2024, 1, 1), end=_utc(2024, 1, 2)
        )
        assert loaded.empty
        assert str(loaded["time"].dtype) == "datetime64[ns, UTC]"

    def test_no_archive_dir_raises(self) -> None:
        """RuntimeError if archive_dir not set and Parquet methods called."""
        store = Store(":memory:")  # no archive_dir
        df = pd.DataFrame(columns=["time"])
        df["time"] = pd.to_datetime(df["time"], utc=True).astype("datetime64[ns, UTC]")
        with pytest.raises(RuntimeError, match="archive_dir"):
            store.write_parquet("EUR_USD", "H1", df)
        with pytest.raises(RuntimeError, match="archive_dir"):
            store.load_parquet("EUR_USD", "H1", _utc(2024, 1, 1), _utc(2024, 1, 2))


# ---------------------------------------------------------------------------
# fetch_and_cache dual-write tests
# ---------------------------------------------------------------------------


class TestFetchAndCacheDualWrite:
    def test_writes_to_parquet_on_new_fetch(self, tmp_path: Path) -> None:
        """When new rows are fetched, they must also be written to Parquet."""
        db_path = tmp_path / "test.db"
        store = Store(db_path, archive_dir=tmp_path / "archive")

        t1 = _utc(2024, 5, 1, 10)
        t2 = _utc(2024, 5, 1, 11)

        mock_client = MagicMock()
        mock_client.get_candles.return_value = [
            _make_candle_row("EUR_USD", "H1", t1),
            _make_candle_row("EUR_USD", "H1", t2),
        ]

        fetch_and_cache(
            mock_client, store, "EUR_USD", "H1", t1, t2, write_parquet=True
        )

        parquet_file = tmp_path / "archive" / "EUR_USD" / "H1" / "2024-05-01.parquet"
        assert parquet_file.exists(), "Parquet file must be written on first fetch"

    def test_no_parquet_write_when_flag_false(self, tmp_path: Path) -> None:
        """``write_parquet=False`` must skip the Parquet write entirely."""
        db_path = tmp_path / "test.db"
        archive_dir = tmp_path / "archive"
        store = Store(db_path, archive_dir=archive_dir)

        t1 = _utc(2024, 5, 2, 10)
        t2 = _utc(2024, 5, 2, 11)

        mock_client = MagicMock()
        mock_client.get_candles.return_value = [
            _make_candle_row("EUR_USD", "H1", t1),
            _make_candle_row("EUR_USD", "H1", t2),
        ]

        fetch_and_cache(
            mock_client, store, "EUR_USD", "H1", t1, t2, write_parquet=False
        )

        parquet_file = archive_dir / "EUR_USD" / "2024-05-02.parquet"
        assert not parquet_file.exists(), "No Parquet file when write_parquet=False"

    def test_cache_hit_makes_no_http_call(self, tmp_path: Path) -> None:
        """Second fetch_and_cache with same range must not call OANDA."""
        db_path = tmp_path / "test.db"
        store = Store(db_path, archive_dir=tmp_path / "archive")

        t1 = _utc(2024, 6, 1, 10)
        t2 = _utc(2024, 6, 1, 11)

        mock_client = MagicMock()
        mock_client.get_candles.return_value = [
            _make_candle_row("EUR_USD", "H1", t1),
            _make_candle_row("EUR_USD", "H1", t2),
        ]

        # First call — fetches from OANDA.
        fetch_and_cache(mock_client, store, "EUR_USD", "H1", t1, t2)
        assert mock_client.get_candles.call_count == 1

        # Second call — must be served entirely from SQLite cache.
        fetch_and_cache(mock_client, store, "EUR_USD", "H1", t1, t2)
        assert mock_client.get_candles.call_count == 1, (
            "get_candles must not be called again for a fully-cached range"
        )


# ---------------------------------------------------------------------------
# Parquet granularity isolation test
# ---------------------------------------------------------------------------


class TestParquetGranularityIsolation:
    def test_h1_and_h4_do_not_bleed_into_each_other(self, tmp_path: Path) -> None:
        """Different granularities for the same instrument must be isolated."""
        store = Store(":memory:", archive_dir=tmp_path)

        base_time = _utc(2024, 7, 15, 0)
        times_h1 = pd.to_datetime(
            [base_time + pd.Timedelta(hours=i) for i in range(3)], utc=True
        ).astype("datetime64[ns, UTC]")
        times_h4 = pd.to_datetime(
            [base_time + pd.Timedelta(hours=i * 4) for i in range(2)], utc=True
        ).astype("datetime64[ns, UTC]")

        def _df(times: pd.DatetimeTZDtype) -> pd.DataFrame:
            n = len(times)
            return pd.DataFrame(
                {
                    "time": times,
                    "open_bid": [1.0] * n,
                    "high_bid": [1.1] * n,
                    "low_bid": [0.9] * n,
                    "close_bid": [1.05] * n,
                    "open_ask": [1.01] * n,
                    "high_ask": [1.11] * n,
                    "low_ask": [0.91] * n,
                    "close_ask": [1.06] * n,
                    "volume": [100] * n,
                }
            )

        store.write_parquet("EUR_USD", "H1", _df(times_h1))
        store.write_parquet("EUR_USD", "H4", _df(times_h4))

        loaded_h1 = store.load_parquet(
            "EUR_USD", "H1", start=base_time, end=base_time + pd.Timedelta(hours=3)
        )
        loaded_h4 = store.load_parquet(
            "EUR_USD", "H4", start=base_time, end=base_time + pd.Timedelta(hours=9)
        )

        # H1 should have 3 rows, H4 should have 2 rows.
        assert len(loaded_h1) == 3
        assert len(loaded_h4) == 2
