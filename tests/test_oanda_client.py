"""Unit tests for data.oanda_client.

Mocks the OANDA v20 candle endpoint using the ``responses`` library.
No live HTTP calls are made.

Covers:
- Single-page success (count <= 500)
- Multi-page pagination (count > 500)
- 401 Unauthorised error -> OandaAPIError
- 400 Bad instrument error -> OandaAPIError
- UTC-aware timestamps on all returned CandleRow instances
- price="BAM" is sent in every request
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import responses as resp_lib
from pydantic import SecretStr
from responses import matchers

import pydantic

from config.settings import Settings
from data.oanda_client import CandleRow, OandaAPIError, OandaClient, _parse_utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PRACTICE_BASE = "https://api-fxpractice.oanda.com"
CANDLES_URL = f"{PRACTICE_BASE}/v3/instruments/EUR_USD/candles"


def _make_candle(
    time: str = "2024-01-15T14:00:00.000000000Z",
    volume: int = 100,
    complete: bool = True,
    o: str = "1.08500",
    h: str = "1.08600",
    lo: str = "1.08400",
    c: str = "1.08550",
) -> dict[str, Any]:
    """Build a single OANDA candle dict with bid, ask, and mid prices."""
    return {
        "time": time,
        "volume": volume,
        "complete": complete,
        "bid": {"o": o, "h": h, "l": lo, "c": c},
        "ask": {"o": str(float(o) + 0.0001), "h": str(float(h) + 0.0001),
                "l": str(float(lo) + 0.0001), "c": str(float(c) + 0.0001)},
        "mid": {"o": str(float(o) + 0.00005), "h": str(float(h) + 0.00005),
                "l": str(float(lo) + 0.00005), "c": str(float(c) + 0.00005)},
    }


def _make_candles_response(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap candles in an OANDA-style top-level response dict."""
    return {
        "instrument": "EUR_USD",
        "granularity": "H1",
        "candles": candles,
    }


def _make_settings(env: str = "demo") -> Settings:
    """Construct a Settings instance with dummy credentials."""
    return Settings(
        env=env,  # type: ignore[arg-type]
        oanda_api_token=SecretStr("dummy-token-never-used-in-tests"),
        oanda_account_id="001-001-1234567-001",
    )


# ---------------------------------------------------------------------------
# _parse_utc helper tests
# ---------------------------------------------------------------------------

class TestParseUtc:
    def test_nanosecond_precision_string(self) -> None:
        dt = _parse_utc("2024-01-15T14:00:00.000000000Z")
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 14

    def test_no_fractional_seconds(self) -> None:
        dt = _parse_utc("2024-06-30T00:00:00Z")
        assert dt.tzinfo == timezone.utc
        assert dt.second == 0

    def test_microsecond_precision(self) -> None:
        dt = _parse_utc("2024-01-15T14:00:00.123456Z")
        assert dt.microsecond == 123456
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# INV-03: CandleRow rejects naive datetime (AwareDatetime enforcement)
# ---------------------------------------------------------------------------

class TestCandleRowInv03:
    def test_naive_time_raises_validation_error(self) -> None:
        """CandleRow must reject a naive (tz-unaware) datetime for time (INV-03)."""
        with pytest.raises(pydantic.ValidationError):
            CandleRow(
                instrument="EUR_USD",
                granularity="H1",
                time=datetime(2024, 1, 1),  # naive — no tzinfo
                open_bid=1.0, high_bid=1.0, low_bid=1.0, close_bid=1.0,
                open_ask=1.0, high_ask=1.0, low_ask=1.0, close_ask=1.0,
                open_mid=1.0, high_mid=1.0, low_mid=1.0, close_mid=1.0,
                volume=100,
                complete=True,
            )

    def test_utc_aware_time_accepted(self) -> None:
        """CandleRow must accept a UTC-aware datetime for time (INV-03)."""
        row = CandleRow(
            instrument="EUR_USD",
            granularity="H1",
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            open_bid=1.0, high_bid=1.0, low_bid=1.0, close_bid=1.0,
            open_ask=1.0, high_ask=1.0, low_ask=1.0, close_ask=1.0,
            open_mid=1.0, high_mid=1.0, low_mid=1.0, close_mid=1.0,
            volume=100,
            complete=True,
        )
        assert row.time.tzinfo is not None


# ---------------------------------------------------------------------------
# Single-page success
# ---------------------------------------------------------------------------

class TestSinglePage:
    @resp_lib.activate
    def test_returns_correct_number_of_rows(self) -> None:
        candles = [_make_candle(f"2024-01-15T{14 + i:02d}:00:00.000000000Z") for i in range(3)]
        resp_lib.add(
            resp_lib.GET,
            CANDLES_URL,
            json=_make_candles_response(candles),
            status=200,
        )
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 3)
        assert len(rows) == 3

    @resp_lib.activate
    def test_returns_candle_row_instances(self) -> None:
        candles = [_make_candle()]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert isinstance(rows[0], CandleRow)

    @resp_lib.activate
    def test_timestamps_are_utc_aware(self) -> None:
        candles = [_make_candle("2024-03-20T10:00:00.000000000Z")]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert rows[0].time.tzinfo is not None
        assert rows[0].time.tzinfo == timezone.utc
        assert rows[0].time == datetime(2024, 3, 20, 10, 0, 0, tzinfo=timezone.utc)

    @resp_lib.activate
    def test_price_BAM_sent_in_request(self) -> None:
        """Verify that price=BAM is included in the query string."""
        candles = [_make_candle()]
        resp_lib.add(
            resp_lib.GET,
            CANDLES_URL,
            match=[matchers.query_param_matcher(
                {"granularity": "H1", "count": "1", "price": "BAM"},
                strict_match=False,
            )],
            json=_make_candles_response(candles),
            status=200,
        )
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert len(rows) == 1

    @resp_lib.activate
    def test_bid_ask_mid_prices_parsed(self) -> None:
        # _make_candle adds 0.0001 for ask and 0.00005 for mid relative to bid.
        candle = _make_candle(o="1.08500", h="1.08600", lo="1.08400", c="1.08550")
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response([candle]), status=200)
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        row = rows[0]
        assert row.open_bid == pytest.approx(1.08500)
        # ask high = 1.08600 + 0.0001 = 1.0861
        assert row.high_ask == pytest.approx(1.08600 + 0.0001)
        # mid open = 1.08500 + 0.00005 = 1.08505
        assert row.open_mid == pytest.approx(1.08500 + 0.00005)

    @resp_lib.activate
    def test_instrument_and_granularity_set_on_row(self) -> None:
        candles = [_make_candle()]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert rows[0].instrument == "EUR_USD"
        assert rows[0].granularity == "H1"

    @resp_lib.activate
    def test_volume_and_complete_flags(self) -> None:
        candles = [_make_candle(volume=250, complete=True)]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert rows[0].volume == 250
        assert rows[0].complete is True

    @resp_lib.activate
    def test_from_time_included_in_request(self) -> None:
        from_dt = datetime(2024, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        candles = [_make_candle()]
        resp_lib.add(
            resp_lib.GET,
            CANDLES_URL,
            match=[matchers.query_param_matcher(
                {"granularity": "H1", "count": "1", "price": "BAM",
                 "from": "2024-01-10T00:00:00.000000000Z"},
                strict_match=True,
            )],
            json=_make_candles_response(candles),
            status=200,
        )
        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1, from_time=from_dt)
        assert len(rows) == 1

    def test_naive_from_time_raises_value_error(self) -> None:
        settings = _make_settings()
        client = OandaClient(settings)
        naive_dt = datetime(2024, 1, 1, 0, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="UTC-aware"):
            client.get_candles("EUR_USD", "H1", 10, from_time=naive_dt)


# ---------------------------------------------------------------------------
# Multi-page pagination
# ---------------------------------------------------------------------------

class TestPagination:
    @resp_lib.activate
    def test_issues_two_requests_when_count_exceeds_500(self) -> None:
        """count=600 should trigger exactly two HTTP requests."""
        def _ts1(idx: int) -> str:
            day = 1 + idx // 24
            hour = idx % 24
            return f"2024-01-{day:02d}T{hour:02d}:00:00.000000000Z"

        # First page: 500 candles
        page1_candles = [_make_candle(_ts1(i)) for i in range(500)]

        # Second page: 101 candles (first is duplicate of last in page1, so 100 net)
        page2_candles = [
            _make_candle(f"2024-02-{1 + j // 24:02d}T{j % 24:02d}:00:00.000000000Z")
            for j in range(101)
        ]
        # Match all GET requests to the candles URL, return pages in order
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(page1_candles), status=200)
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(page2_candles), status=200)

        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 600)

        # Two requests made
        assert len(resp_lib.calls) == 2
        # Total rows should be <= 600
        assert len(rows) <= 600

    @resp_lib.activate
    def test_pagination_concatenates_results(self) -> None:
        """Rows from page1 and page2 are all present in output."""
        # Build 500 candles with valid timestamps using day+hour combinations.
        # 500 hours = ~20 days * 24 + 20 hours; iterate over day 1-21.
        def _ts(idx: int) -> str:
            day = 1 + idx // 24
            hour = idx % 24
            return f"2024-01-{day:02d}T{hour:02d}:00:00.000000000Z"

        page1 = [_make_candle(_ts(i)) for i in range(500)]
        # Page2: first candle is duplicate of page1's last, followed by 100 new
        last_of_page1 = page1[-1]
        new_candles = [_make_candle(f"2024-02-{1 + j // 24:02d}T{j % 24:02d}:00:00.000000000Z")
                       for j in range(100)]
        page2 = [last_of_page1] + new_candles

        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(page1), status=200)
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(page2), status=200)

        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 600)

        # Should have combined 500 + 100 = 600 rows (duplicate dropped)
        assert len(rows) == 600

    @resp_lib.activate
    def test_stops_when_oanda_returns_less_than_requested(self) -> None:
        """If OANDA returns fewer candles than asked, do not issue more requests."""
        candles = [_make_candle(f"2024-01-01T{i:02d}:00:00.000000000Z") for i in range(10)]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)

        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 500)

        assert len(resp_lib.calls) == 1
        assert len(rows) == 10

    @resp_lib.activate
    def test_returns_at_most_count_rows(self) -> None:
        """Even if OANDA returns more, output is capped at count."""
        candles = [_make_candle(f"2024-01-01T{i:02d}:00:00.000000000Z") for i in range(20)]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)

        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 5)
        assert len(rows) == 5

    @resp_lib.activate
    def test_first_page_returns_499_of_500_requested_issues_only_one_request(self) -> None:
        """Boundary: count=500, OANDA returns 499 on first page → no second request.

        Regression test for the off-by-one where first_page was cleared before
        the usable_slots early-exit check, causing a spurious extra request when
        OANDA returns exactly request_count - 1 candles on the first page.
        """
        candles = [
            _make_candle(f"2024-01-{1 + i // 24:02d}T{i % 24:02d}:00:00.000000000Z")
            for i in range(499)
        ]
        resp_lib.add(resp_lib.GET, CANDLES_URL, json=_make_candles_response(candles), status=200)

        settings = _make_settings()
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 500)

        assert len(resp_lib.calls) == 1, (
            "Expected exactly one HTTP request when OANDA returns 499 of 500 candles"
        )
        assert len(rows) == 499


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @resp_lib.activate
    def test_401_raises_oanda_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            CANDLES_URL,
            json={"errorMessage": "Unauthorised"},
            status=401,
        )
        settings = _make_settings()
        client = OandaClient(settings)
        with pytest.raises(OandaAPIError) as exc_info:
            client.get_candles("EUR_USD", "H1", 10)
        assert exc_info.value.status_code == 401

    @resp_lib.activate
    def test_400_bad_instrument_raises_oanda_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            CANDLES_URL,
            json={"errorMessage": "Invalid instrument: EUR_USD_BAD"},
            status=400,
        )
        settings = _make_settings()
        client = OandaClient(settings)
        with pytest.raises(OandaAPIError) as exc_info:
            client.get_candles("EUR_USD", "H1", 10)
        assert exc_info.value.status_code == 400

    @resp_lib.activate
    def test_oanda_api_error_is_not_v20error(self) -> None:
        """The raised error must be OandaAPIError, not the raw V20Error."""
        from oandapyV20.exceptions import V20Error
        resp_lib.add(resp_lib.GET, CANDLES_URL, json={"errorMessage": "Server error"}, status=500)
        settings = _make_settings()
        client = OandaClient(settings)
        with pytest.raises(OandaAPIError):
            client.get_candles("EUR_USD", "H1", 10)

    @resp_lib.activate
    def test_oanda_api_error_message_accessible(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            CANDLES_URL,
            json={"errorMessage": "Account not found"},
            status=403,
        )
        settings = _make_settings()
        client = OandaClient(settings)
        with pytest.raises(OandaAPIError) as exc_info:
            client.get_candles("EUR_USD", "H1", 10)
        assert exc_info.value.status_code == 403
        assert exc_info.value.message  # non-empty message


# ---------------------------------------------------------------------------
# Environment / settings wiring
# ---------------------------------------------------------------------------

class TestEnvironmentWiring:
    @resp_lib.activate
    def test_demo_env_uses_practice_url(self) -> None:
        """demo settings -> requests go to api-fxpractice.oanda.com."""
        candles = [_make_candle()]
        resp_lib.add(
            resp_lib.GET,
            f"{PRACTICE_BASE}/v3/instruments/EUR_USD/candles",
            json=_make_candles_response(candles),
            status=200,
        )
        settings = _make_settings(env="demo")
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert len(rows) == 1
        # The call must have hit the practice base URL.
        called_url = resp_lib.calls[0].request.url
        assert called_url is not None
        assert PRACTICE_BASE in called_url

    @resp_lib.activate
    def test_live_env_uses_live_url(self) -> None:
        """live settings -> requests go to api-fxtrade.oanda.com."""
        live_url = "https://api-fxtrade.oanda.com/v3/instruments/EUR_USD/candles"
        candles = [_make_candle()]
        resp_lib.add(resp_lib.GET, live_url, json=_make_candles_response(candles), status=200)
        settings = _make_settings(env="live")
        client = OandaClient(settings)
        rows = client.get_candles("EUR_USD", "H1", 1)
        assert len(rows) == 1
        called_url = resp_lib.calls[0].request.url
        assert called_url is not None
        assert "api-fxtrade.oanda.com" in called_url
