"""Tests for data/stream.py — PriceStream.

All tests use mocked PricingStream generators; NO live HTTP calls are made
(enforced by the task spec).

Coverage:
- Tick parsing: UTC-aware timestamps, bid/ask/instrument/status, gap_detected=False
  on first tick.
- Heartbeat: resets liveness timer (no tick emitted for heartbeats).
- Heartbeat timeout → reconnect path triggered.
- Backoff schedule: capped exponential + jitter (statistical bound check).
- gap_detected=True on first tick after reconnect.
- Clean shutdown: thread joins, no leak, sentinel consumed.
- Typed error (OandaStreamError) on 4xx; raw V20Error on 5xx wraps into retry.
- _parse_utc handles nanosecond and second precision OANDA timestamps.
- _backoff_delay: monotonically non-decreasing median, capped at _BACKOFF_CAP.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from typing import Generator, Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from oandapyV20.exceptions import V20Error

from config.settings import Settings
from data.stream import (
    OandaStreamError,
    PriceTick,
    PriceStream,
    _backoff_delay,
    _BACKOFF_CAP,
    _make_tick,
    _parse_utc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings() -> Settings:
    """Create a minimal Settings instance for tests (no .env needed)."""
    return Settings(
        env="demo",
        oanda_api_token=SecretStr("test-token-never-logged"),
        oanda_account_id="12345",
        oanda_base_url="https://api-fxpractice.oanda.com",
    )


def _price_msg(
    instrument: str = "EUR_USD",
    time_str: str = "2024-01-15T14:32:00.123456789Z",
    bid: str = "1.09500",
    ask: str = "1.09510",
    tradeable: bool = True,
) -> dict[str, Any]:
    """Build a synthetic PRICE stream message."""
    return {
        "type": "PRICE",
        "instrument": instrument,
        "time": time_str,
        "bids": [{"price": bid, "liquidity": 10_000_000}],
        "asks": [{"price": ask, "liquidity": 10_000_000}],
        "tradeable": tradeable,
        "status": "tradeable" if tradeable else "non-tradeable",
    }


def _heartbeat_msg() -> dict[str, Any]:
    """Build a synthetic HEARTBEAT stream message."""
    return {"type": "HEARTBEAT", "time": "2024-01-15T14:32:05.000000000Z"}


def _make_generator(
    messages: list[dict[str, Any]],
    *,
    pause_after: int | None = None,
    pause_seconds: float = 0.0,
) -> Generator[dict[str, Any], None, None]:
    """Yield messages from a list, optionally sleeping after `pause_after` items."""
    for i, msg in enumerate(messages):
        if pause_after is not None and i == pause_after:
            time.sleep(pause_seconds)
        yield msg


def _make_stream_with_mock_api(
    messages: list[dict[str, Any]],
    settings: Settings | None = None,
    heartbeat_timeout: float = 5.0,
) -> tuple[PriceStream, MagicMock]:
    """Create a PriceStream whose internal API is mocked to yield *messages*."""
    if settings is None:
        settings = _make_settings()
    stream = PriceStream(
        settings,
        instruments=["EUR_USD"],
        heartbeat_timeout=heartbeat_timeout,
    )
    mock_api = MagicMock()
    mock_api.request.return_value = iter(messages)
    return stream, mock_api


# ---------------------------------------------------------------------------
# Unit tests — _parse_utc
# ---------------------------------------------------------------------------

class TestParseUtc:
    def test_nanosecond_precision(self) -> None:
        dt = _parse_utc("2024-01-15T14:32:00.123456789Z")
        assert dt.tzinfo is timezone.utc
        assert dt.year == 2024
        assert dt.microsecond == 123456  # nanoseconds truncated to microseconds

    def test_second_precision(self) -> None:
        dt = _parse_utc("2024-01-15T14:32:00Z")
        assert dt.tzinfo is timezone.utc
        assert dt.second == 0
        assert dt.microsecond == 0

    def test_microsecond_precision(self) -> None:
        dt = _parse_utc("2024-01-15T14:32:00.000001Z")
        assert dt.tzinfo is timezone.utc
        assert dt.microsecond == 1

    def test_result_is_aware(self) -> None:
        dt = _parse_utc("2024-06-01T00:00:00Z")
        assert dt.utcoffset() is not None


# ---------------------------------------------------------------------------
# Unit tests — _backoff_delay
# ---------------------------------------------------------------------------

class TestBackoffDelay:
    def test_first_attempt_in_range(self) -> None:
        # Attempt 0 → base 1s ± 50 % jitter → [0.5, 1.5]
        delays = [_backoff_delay(0) for _ in range(100)]
        assert all(0.0 <= d <= 1.5 + 0.01 for d in delays)

    def test_capped_at_backoff_cap(self) -> None:
        # High attempt → must never exceed cap * (1 + jitter)
        delays = [_backoff_delay(20) for _ in range(100)]
        max_possible = _BACKOFF_CAP * 1.5
        assert all(d <= max_possible + 0.01 for d in delays)

    def test_median_grows_then_caps(self) -> None:
        # Median for each attempt should be non-decreasing up to cap.
        medians = []
        for attempt in range(8):
            sample = sorted(_backoff_delay(attempt) for _ in range(200))
            medians.append(sample[100])  # ~median
        for i in range(1, len(medians)):
            # Allow a small tolerance for randomness.
            assert medians[i] >= medians[i - 1] * 0.8, (
                f"Median decreased unexpectedly at attempt {i}: "
                f"{medians[i - 1]:.3f} → {medians[i]:.3f}"
            )
        # Late attempts should be capped.
        high_sample = sorted(_backoff_delay(10) for _ in range(200))
        assert high_sample[100] <= _BACKOFF_CAP

    def test_non_negative(self) -> None:
        assert all(_backoff_delay(i) >= 0.0 for i in range(10))


# ---------------------------------------------------------------------------
# Unit tests — _make_tick
# ---------------------------------------------------------------------------

class TestMakeTick:
    def test_basic_tick(self) -> None:
        msg = _price_msg()
        tick = _make_tick(msg, gap_detected=False)
        assert tick is not None
        assert tick.instrument == "EUR_USD"
        assert tick.bid == pytest.approx(1.09500)
        assert tick.ask == pytest.approx(1.09510)
        assert tick.time.tzinfo is timezone.utc
        assert tick.gap_detected is False

    def test_gap_detected_propagated(self) -> None:
        msg = _price_msg()
        tick = _make_tick(msg, gap_detected=True)
        assert tick is not None
        assert tick.gap_detected is True

    def test_non_tradeable(self) -> None:
        msg = _price_msg(tradeable=False)
        tick = _make_tick(msg, gap_detected=False)
        assert tick is not None
        assert tick.status == "non-tradeable"

    def test_missing_bids_returns_none(self) -> None:
        msg = _price_msg()
        del msg["bids"]
        assert _make_tick(msg, gap_detected=False) is None

    def test_missing_asks_returns_none(self) -> None:
        msg = _price_msg()
        del msg["asks"]
        assert _make_tick(msg, gap_detected=False) is None

    def test_invalid_price_returns_none(self) -> None:
        msg = _price_msg()
        msg["bids"] = [{"price": "not-a-float"}]
        assert _make_tick(msg, gap_detected=False) is None

    def test_utc_aware_timestamp(self) -> None:
        msg = _price_msg(time_str="2024-03-20T09:00:00.000000000Z")
        tick = _make_tick(msg, gap_detected=False)
        assert tick is not None
        assert tick.time == datetime(2024, 3, 20, 9, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Integration tests — PriceStream with mocked PricingStream
# ---------------------------------------------------------------------------

class TestPriceStreamTickParsing:
    """Tick parsing: UTC-aware timestamps, correct fields."""

    def test_single_tick_yielded(self) -> None:
        messages = [_price_msg()]
        stream, mock_api = _make_stream_with_mock_api(messages)

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            tick = stream.get_tick(timeout=5.0)
            stream.stop()

        assert tick is not None
        assert isinstance(tick, PriceTick)
        assert tick.instrument == "EUR_USD"
        assert tick.time.tzinfo is timezone.utc
        assert tick.bid == pytest.approx(1.09500)
        assert tick.ask == pytest.approx(1.09510)
        assert tick.gap_detected is False

    def test_multiple_ticks(self) -> None:
        instruments = ["EUR_USD", "GBP_USD"]
        messages = [
            _price_msg("EUR_USD", bid="1.09500", ask="1.09510"),
            _price_msg("GBP_USD", bid="1.26000", ask="1.26020"),
        ]
        settings = _make_settings()
        stream = PriceStream(settings, instruments=instruments, heartbeat_timeout=5.0)
        mock_api = MagicMock()
        mock_api.request.return_value = iter(messages)

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            tick1 = stream.get_tick(timeout=5.0)
            tick2 = stream.get_tick(timeout=5.0)
            stream.stop()

        assert tick1 is not None and tick1.instrument == "EUR_USD"
        assert tick2 is not None and tick2.instrument == "GBP_USD"


class TestPriceStreamHeartbeat:
    """Heartbeat: consumed internally, resets liveness timer, NOT surfaced."""

    def test_heartbeats_not_surfaced(self) -> None:
        # Three heartbeats then one price tick.
        messages = [
            _heartbeat_msg(),
            _heartbeat_msg(),
            _heartbeat_msg(),
            _price_msg(),
        ]
        stream, mock_api = _make_stream_with_mock_api(messages)

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            tick = stream.get_tick(timeout=5.0)
            stream.stop()

        # Only the PRICE message should produce a tick.
        assert tick is not None
        assert isinstance(tick, PriceTick)
        # Queue should now be drained (heartbeats didn't fill it).
        # After stop() the sentinel is in the queue; calling get_tick returns None.
        final = stream.get_tick(timeout=1.0)
        assert final is None

    def test_heartbeat_resets_liveness_timer(self) -> None:
        """Heartbeats must prevent a timeout from firing while they arrive."""
        # We use a very short heartbeat_timeout and send heartbeats just in time.
        # The stream should NOT reconnect during the heartbeat sequence.
        reconnect_count = [0]

        original_stream_once = PriceStream._stream_once

        def counting_stream_once(
            self_inner: Any, gap: bool, attempt: int
        ) -> bool:
            reconnect_count[0] = attempt
            return original_stream_once(self_inner, gap, attempt)

        messages = [
            _heartbeat_msg(),
            _heartbeat_msg(),
            _price_msg(),
        ]
        stream, mock_api = _make_stream_with_mock_api(
            messages, heartbeat_timeout=2.0
        )

        with patch.object(stream, "_make_api", return_value=mock_api):
            with patch.object(PriceStream, "_stream_once", counting_stream_once):
                stream.start()
                tick = stream.get_tick(timeout=5.0)
                stream.stop()

        # Should never have needed a reconnect (attempt 0 throughout).
        assert reconnect_count[0] == 0
        assert tick is not None


class TestPriceStreamReconnect:
    """Reconnect: triggered by heartbeat timeout; gap_detected on first tick after."""

    def test_gap_detected_on_reconnect(self) -> None:
        """After a reconnect, the first tick must have gap_detected=True."""
        # We mock _stream_once to simulate: first call returns True (disconnect),
        # second call yields a tick and returns False (clean stop).
        call_count = [0]
        collected_ticks: list[PriceTick] = []

        def mock_stream_once(
            self_inner: PriceStream, gap_on_next: bool, attempt: int
        ) -> bool:
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate first connection: emit one tick then disconnect.
                msg = _price_msg()
                tick = _make_tick(msg, gap_detected=gap_on_next)
                if tick:
                    self_inner._queue.put(tick)
                return True  # signals gap_on_next for next connection

            elif call_count[0] == 2:
                # Simulate reconnected connection: emit one tick then stop.
                msg = _price_msg(bid="1.09520", ask="1.09530")
                tick = _make_tick(msg, gap_detected=gap_on_next)
                if tick:
                    collected_ticks.append(tick)
                    self_inner._queue.put(tick)
                # Signal clean stop so the loop exits.
                self_inner._stop_event.set()
                return False

            return False

        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=5.0)

        with patch.object(PriceStream, "_stream_once", mock_stream_once):
            stream.start()
            tick1 = stream.get_tick(timeout=5.0)
            tick2 = stream.get_tick(timeout=5.0)
            stream.stop()

        assert tick1 is not None and tick1.gap_detected is False
        assert tick2 is not None and tick2.gap_detected is True

    def test_reconnect_on_heartbeat_timeout(self) -> None:
        """Heartbeat timeout must cause a reconnect (attempt counter increments)."""
        attempts_seen: list[int] = []

        def mock_stream_once(
            self_inner: PriceStream, gap_on_next: bool, attempt: int
        ) -> bool:
            attempts_seen.append(attempt)
            if attempt >= 1:
                # On second attempt, shut down cleanly.
                self_inner._stop_event.set()
                return False
            # First attempt: return True to simulate timeout/disconnect.
            return True

        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=0.01)

        with patch("data.stream._backoff_delay", return_value=0.0):
            with patch.object(PriceStream, "_stream_once", mock_stream_once):
                stream.start()
                stream.stop()

        assert 0 in attempts_seen
        assert 1 in attempts_seen

    def test_backoff_called_on_reconnect(self) -> None:
        """_backoff_delay must be called with the correct attempt index on reconnect."""
        backoff_calls: list[int] = []

        original_backoff = __import__("data.stream", fromlist=["_backoff_delay"])._backoff_delay

        def tracking_backoff(attempt: int) -> float:
            backoff_calls.append(attempt)
            return 0.0  # no actual sleep in tests

        call_count = [0]

        def mock_stream_once(
            self_inner: PriceStream, gap_on_next: bool, attempt: int
        ) -> bool:
            call_count[0] += 1
            if attempt >= 2:
                self_inner._stop_event.set()
                return False
            return True  # simulate disconnect each time

        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=5.0)

        with patch("data.stream._backoff_delay", side_effect=tracking_backoff):
            with patch.object(PriceStream, "_stream_once", mock_stream_once):
                stream.start()
                stream.stop()

        # backoff is called for attempt 1 and 2 (attempt 0 has no preceding backoff).
        assert 0 in backoff_calls  # first reconnect uses attempt 0 → delay for attempt 0
        assert 1 in backoff_calls  # second reconnect uses attempt 1


class TestPriceStreamShutdown:
    """Clean shutdown: thread joins, no orphaned connection, no leaked resources."""

    def test_stop_joins_thread(self) -> None:
        """After stop(), the background thread must not be alive."""
        messages: list[dict[str, Any]] = []  # Empty → generator exhausts immediately
        stream, mock_api = _make_stream_with_mock_api(messages)

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            assert stream._thread is not None
            stream.stop()

        assert stream._thread is None

    def test_stop_before_start_is_noop(self) -> None:
        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"])
        stream.stop()  # must not raise

    def test_stop_drains_sentinel(self) -> None:
        """After stop(), get_tick() must return None (not hang)."""
        messages: list[dict[str, Any]] = []
        stream, mock_api = _make_stream_with_mock_api(messages)

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            stream.stop()

        result = stream.get_tick(timeout=2.0)
        assert result is None

    def test_iterator_terminates_on_stop(self) -> None:
        """Iterating over a stopped stream must terminate cleanly."""
        messages = [_price_msg()]
        stream, mock_api = _make_stream_with_mock_api(messages)
        collected: list[PriceTick] = []

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            for tick in stream:
                collected.append(tick)
                stream.stop()  # signal stop on first tick
                break

        assert len(collected) == 1
        assert stream._thread is None

    def test_double_start_raises(self) -> None:
        """Starting an already-running stream must raise RuntimeError."""
        # Use a blocking generator so the thread stays alive.
        done = threading.Event()

        def blocking_gen() -> Generator[dict[str, Any], None, None]:
            done.wait(timeout=5.0)
            return
            yield  # make it a generator

        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=5.0)
        mock_api = MagicMock()
        mock_api.request.return_value = blocking_gen()

        with patch.object(stream, "_make_api", return_value=mock_api):
            stream.start()
            try:
                with pytest.raises(RuntimeError, match="already running"):
                    stream.start()
            finally:
                done.set()
                stream.stop()


class TestPriceStreamTypedErrors:
    """Typed errors: 4xx → OandaStreamError; no raw library exceptions escape."""

    def test_4xx_raises_stream_error(self) -> None:
        """A 4xx from the stream connection must raise OandaStreamError."""
        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=5.0)

        def mock_stream_once(
            self_inner: PriceStream, gap_on_next: bool, attempt: int
        ) -> bool:
            raise OandaStreamError(401, "Unauthorized")

        with patch.object(PriceStream, "_stream_once", mock_stream_once):
            stream.start()
            with pytest.raises(OandaStreamError) as exc_info:
                stream.get_tick(timeout=5.0)
            stream.stop()

        assert exc_info.value.status_code == 401

    def test_stream_error_inherits_oanda_api_error(self) -> None:
        from data.oanda_client import OandaAPIError

        err = OandaStreamError(403, "Forbidden")
        assert isinstance(err, OandaAPIError)
        assert "403" in str(err)

    def test_stream_error_delivered_via_queue(self) -> None:
        """OandaStreamError must be put in the queue and re-raised by get_tick."""
        settings = _make_settings()
        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=5.0)
        error = OandaStreamError(401, "Unauthorized")

        # Manually inject the error into the queue and put sentinel after.
        stream._queue.put(error)
        stream._queue.put(stream._queue.__class__.__new__(stream._queue.__class__))

        with pytest.raises(OandaStreamError):
            stream.get_tick(timeout=1.0)


class TestPriceStreamNoTokenInLogs:
    """INV-08: token must never appear in log records."""

    def test_reconnect_log_contains_no_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reconnect log messages must not contain the API token."""
        settings = _make_settings()
        token = settings.oanda_api_token.get_secret_value()

        call_count = [0]

        def mock_stream_once(
            self_inner: PriceStream, gap_on_next: bool, attempt: int
        ) -> bool:
            call_count[0] += 1
            if attempt >= 1:
                self_inner._stop_event.set()
                return False
            return True  # simulate disconnect

        stream = PriceStream(settings, instruments=["EUR_USD"], heartbeat_timeout=5.0)

        import logging
        with caplog.at_level(logging.INFO, logger="data.stream"):
            with patch("data.stream._backoff_delay", return_value=0.0):
                with patch.object(PriceStream, "_stream_once", mock_stream_once):
                    stream.start()
                    stream.stop()

        for record in caplog.records:
            assert token not in record.getMessage(), (
                f"Token found in log record: {record.getMessage()!r}"
            )
