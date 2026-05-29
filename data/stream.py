"""OANDA v20 live pricing stream — `PriceStream`.

Wraps `oandapyV20.endpoints.pricing.PricingStream` (chunked HTTP, NOT WebSocket)
to provide a background-thread producer that pushes `PriceTick` objects into a
thread-safe queue for consumption.

Design decisions (per spec and Phase 1B task 1B-T-01):
- Background thread + `queue.Queue` — simplest fit for the otherwise-synchronous
  codebase (open question in spec resolved as "lean: thread").
- Heartbeats consumed internally and used to reset a liveness timer; they are
  never surfaced to consumers.
- Heartbeat timeout (no heartbeat within `heartbeat_timeout` seconds) triggers
  a reconnect, exactly like a connection drop.
- Reconnect uses **capped exponential backoff + jitter** (base 1 s, cap 30 s,
  ×2 per attempt, ±50 % jitter) so reconnect storms cannot hammer the API.
- On each reconnect a `gap_detected` flag is set on the next `PriceTick` emitted,
  so downstream consumers know data continuity was broken.
- `OandaStreamError` is raised (not raw library exceptions) and also delivered
  through the queue so the consuming thread sees it cleanly.
- Token never logged (INV-08); env/endpoint from `settings.env` (INV-09);
  all timestamps UTC-aware from first touch (INV-03).

Usage::

    stream = PriceStream(settings, instruments=["EUR_USD", "GBP_USD"])
    stream.start()
    try:
        for tick in stream:          # blocks until a tick or timeout
            print(tick)
    finally:
        stream.stop()
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Generator, Iterator

import oandapyV20
import oandapyV20.endpoints.pricing as oanda_pricing
from oandapyV20.exceptions import StreamTerminated, V20Error
from pydantic import AwareDatetime, BaseModel

from config.settings import Settings
from data.oanda_client import OandaAPIError

logger = logging.getLogger(__name__)

# Map settings.env → oandapyV20 environment string (mirrors OandaClient, INV-09).
_ENV_MAP: dict[str, str] = {
    "demo": "practice",
    "live": "live",
}

# Sentinel placed in the queue when the stream shuts down cleanly.
_SENTINEL: object = object()

# Backoff parameters.
_BACKOFF_BASE: float = 1.0    # seconds
_BACKOFF_CAP: float = 30.0    # seconds (hard cap)
_BACKOFF_MULTIPLIER: float = 2.0
_BACKOFF_JITTER: float = 0.5  # ±50 % multiplicative jitter

# Default heartbeat timeout: OANDA sends a heartbeat every ~5 s; we allow 10 s.
_DEFAULT_HEARTBEAT_TIMEOUT: float = 10.0


class OandaStreamError(OandaAPIError):
    """Raised when a streaming connection fails unrecoverably.

    Inherits from `OandaAPIError` so callers that catch `OandaAPIError` also
    catch stream errors consistently with the rest of the data layer.
    """


class PriceTick(BaseModel):
    """A single live price tick from the OANDA pricing stream.

    Attributes
    ----------
    instrument : str
        OANDA instrument identifier, e.g. ``"EUR_USD"``.
    time : AwareDatetime
        UTC-aware timestamp of the tick (INV-03).
    bid : float
        Best bid price at tick time.
    ask : float
        Best ask price at tick time.
    status : str
        OANDA tradeable status, e.g. ``"tradeable"`` or ``"non-tradeable"``.
    gap_detected : bool
        ``True`` if this is the first tick after a reconnect — data may have
        been missed between the previous tick and this one.
    """

    instrument: str
    time: AwareDatetime   # UTC-aware (INV-03; pydantic rejects naive datetimes)
    bid: float
    ask: float
    status: str
    gap_detected: bool = False


def _parse_utc(iso_string: str) -> datetime:
    """Parse an ISO 8601 UTC string from the OANDA stream to UTC-aware datetime.

    OANDA stream timestamps look like ``"2024-01-15T14:32:00.123456789Z"``.
    Python's ``fromisoformat`` cannot handle nanosecond precision or a bare ``Z``
    on all versions, so we strip and truncate before parsing.

    Args:
        iso_string: ISO 8601 string from OANDA, assumed UTC.

    Returns:
        A UTC-aware ``datetime``.
    """
    s = iso_string.rstrip("Z")
    if "." in s:
        date_part, frac = s.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        s = f"{date_part}.{frac}"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc)


def _backoff_delay(attempt: int) -> float:
    """Compute a jittered capped exponential backoff delay.

    Args:
        attempt: Zero-based reconnect attempt count.

    Returns:
        Seconds to sleep before the next attempt.
    """
    base = _BACKOFF_BASE * (_BACKOFF_MULTIPLIER ** attempt)
    capped = min(base, _BACKOFF_CAP)
    jitter = capped * _BACKOFF_JITTER * (2 * random.random() - 1)  # ±50 %
    return max(0.0, capped + jitter)


def _make_tick(msg: dict[str, Any], gap_detected: bool) -> PriceTick | None:
    """Convert a PRICE stream message to a ``PriceTick``, or ``None`` if invalid.

    Args:
        msg: Raw dict decoded from the OANDA stream.
        gap_detected: Whether a reconnect happened before this tick.

    Returns:
        A ``PriceTick`` instance, or ``None`` if the message cannot be parsed.
    """
    try:
        # bids/asks are lists of dicts; use the first (best) entry.
        bids: list[dict[str, str]] = msg.get("bids", [])
        asks: list[dict[str, str]] = msg.get("asks", [])
        if not bids or not asks:
            return None
        return PriceTick(
            instrument=msg["instrument"],
            time=_parse_utc(msg["time"]),
            bid=float(bids[0]["price"]),
            ask=float(asks[0]["price"]),
            status=msg.get("tradeable", "tradeable")
            if isinstance(msg.get("tradeable"), str)
            else ("tradeable" if msg.get("tradeable", True) else "non-tradeable"),
            gap_detected=gap_detected,
        )
    except (KeyError, ValueError, IndexError):
        return None


class PriceStream:
    """Long-lived OANDA v20 live pricing stream with automatic reconnection.

    The stream runs on a background thread.  Parsed `PriceTick` objects are
    put into an internal queue.  The main thread consumes them by iterating
    over the `PriceStream` instance or calling `get_tick()`.

    Args:
        settings: Fully-initialised `Settings` instance.  Environment and token
            are derived from it exclusively (INV-08, INV-09).
        instruments: List of OANDA instrument identifiers to subscribe to,
            e.g. ``["EUR_USD", "GBP_USD"]``.
        heartbeat_timeout: Seconds to wait for a heartbeat before treating the
            connection as stale and reconnecting (default 10 s).
        queue_maxsize: Maximum number of `PriceTick` objects buffered in the
            internal queue before the producer blocks (0 = unbounded).
    """

    def __init__(
        self,
        settings: Settings,
        instruments: list[str],
        heartbeat_timeout: float = _DEFAULT_HEARTBEAT_TIMEOUT,
        queue_maxsize: int = 0,
    ) -> None:
        self._settings = settings
        self._instruments = instruments
        self._heartbeat_timeout = heartbeat_timeout
        self._queue: queue.Queue[PriceTick | OandaStreamError | object] = queue.Queue(
            maxsize=queue_maxsize
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background streaming thread.

        Safe to call only once.  Use `stop()` + `start()` to restart.

        Raises:
            RuntimeError: If the stream is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("PriceStream is already running")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="fathom-price-stream",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to shut down and wait for it to exit.

        No-op if the stream is not running.  After this returns the thread has
        joined and there is no orphaned connection (clean shutdown guarantee).
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def get_tick(self, timeout: float | None = None) -> PriceTick | None:
        """Retrieve the next tick from the queue.

        Args:
            timeout: Seconds to wait for a tick.  ``None`` = block forever.

        Returns:
            A `PriceTick`, or ``None`` if the stream has shut down.

        Raises:
            OandaStreamError: If the background thread encountered an
                unrecoverable stream error.
            queue.Empty: If ``timeout`` is given and no tick arrived in time.
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise
        if item is _SENTINEL:
            return None
        if isinstance(item, OandaStreamError):
            raise item
        return item  # type: ignore[return-value]

    def __iter__(self) -> Iterator[PriceTick]:
        """Iterate over ticks until the stream shuts down or raises an error."""
        while True:
            try:
                tick = self.get_tick()
            except queue.Empty:
                continue
            if tick is None:
                return
            yield tick

    # ------------------------------------------------------------------
    # Internal: background thread
    # ------------------------------------------------------------------

    def _make_api(self) -> oandapyV20.API:
        """Create an oandapyV20.API instance from settings (INV-08, INV-09)."""
        oanda_env = _ENV_MAP[self._settings.env]
        # Token accessed via get_secret_value(); never logged (INV-08).
        return oandapyV20.API(
            access_token=self._settings.oanda_api_token.get_secret_value(),
            environment=oanda_env,
        )

    def _run(self) -> None:
        """Main background loop: connect → stream → reconnect on failure."""
        attempt = 0
        gap_on_next = False  # True after the first reconnect

        while not self._stop_event.is_set():
            if attempt > 0:
                delay = _backoff_delay(attempt - 1)
                # Log at INFO; account_id is safe, token is NOT included (INV-08).
                logger.info(
                    "PriceStream reconnect attempt %d for account %s — "
                    "backoff %.2fs (instruments: %s)",
                    attempt,
                    self._settings.oanda_account_id,
                    delay,
                    ",".join(self._instruments),
                )
                # Sleep in short increments so stop_event is noticed quickly.
                deadline = time.monotonic() + delay
                while not self._stop_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.1, remaining))

            if self._stop_event.is_set():
                break

            try:
                gap_on_next = self._stream_once(gap_on_next, attempt)
            except OandaStreamError as exc:
                logger.warning(
                    "PriceStream unrecoverable error (attempt %d): %s",
                    attempt,
                    exc,
                )
                self._queue.put(exc)
                break
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    "PriceStream connection error (attempt %d): %s — will retry",
                    attempt,
                    exc,
                )
                gap_on_next = True

            attempt += 1

        self._queue.put(_SENTINEL)

    def _stream_once(self, gap_on_next: bool, attempt: int) -> bool:
        """Open one streaming connection and consume until it ends or times out.

        Args:
            gap_on_next: Whether to mark the next emitted tick as gap_detected.
            attempt: Current attempt count (for log context).

        Returns:
            ``True`` if the caller should set gap_on_next for the next
            connection (i.e. we finished this connection without a clean stop).

        Raises:
            OandaStreamError: On HTTP 4xx from OANDA (not worth retrying).
        """
        api = self._make_api()
        account_id = self._settings.oanda_account_id
        params = {"instruments": ",".join(self._instruments)}

        try:
            req = oanda_pricing.PricingStream(accountID=account_id, params=params)
            generator: Generator[dict[str, Any], None, None] = api.request(req)
        except V20Error as exc:
            if exc.code >= 400 and exc.code < 500:
                # 4xx: unrecoverable (bad credentials, bad instruments, etc.)
                raise OandaStreamError(exc.code, exc.msg) from exc
            # 5xx: transient; let the outer loop retry.
            raise

        first_tick_emitted = gap_on_next
        last_heartbeat = time.monotonic()

        while not self._stop_event.is_set():
            # Check heartbeat liveness on each iteration.
            elapsed = time.monotonic() - last_heartbeat
            if elapsed > self._heartbeat_timeout:
                logger.warning(
                    "PriceStream heartbeat timeout after %.1fs (attempt %d) — reconnecting",
                    elapsed,
                    attempt,
                )
                # Terminate the stream generator cleanly.
                try:
                    req.terminate("heartbeat timeout")
                except (StreamTerminated, ValueError):
                    pass
                return True  # signal: gap_on_next for next connection

            # Try to pull the next message.  We use a short-poll approach:
            # pull from the generator with a tight timeout so we can check
            # stop_event and liveness regularly.
            try:
                msg = self._next_with_timeout(generator, timeout=0.5)
            except StopIteration:
                # Generator exhausted — server closed the stream.
                return True
            except (StreamTerminated, RuntimeError):
                # Stream was terminated (either by us or by the library).
                if self._stop_event.is_set():
                    return False
                return True
            except V20Error as exc:
                if exc.code >= 400 and exc.code < 500:
                    raise OandaStreamError(exc.code, exc.msg) from exc
                raise
            except _TimeoutToken:
                # No message yet — loop back to check liveness and stop_event.
                continue

            # Process the message.
            msg_type = msg.get("type", "")

            if msg_type == "HEARTBEAT":
                last_heartbeat = time.monotonic()
                continue

            if msg_type == "PRICE":
                tick = _make_tick(msg, gap_detected=gap_on_next)
                if tick is not None:
                    gap_on_next = False
                    self._queue.put(tick)
                continue

            # Unknown message type — log and ignore.
            logger.debug("PriceStream ignored message type=%r", msg_type)

        # stop_event set: terminate the generator and exit cleanly.
        try:
            req.terminate("stop requested")
        except (StreamTerminated, ValueError):
            pass
        return False  # clean stop — no gap for next connection

    def _next_with_timeout(
        self,
        gen: Generator[dict[str, Any], None, None],
        timeout: float,
    ) -> dict[str, Any]:
        """Pull the next item from *gen*, raising _TimeoutToken if timeout elapses.

        Because the oandapyV20 generator is a synchronous blocking iterator we
        run it on a separate daemon thread so we can interrupt the wait.  The
        result (or exception) is communicated back via a small local queue.

        Args:
            gen: The oandapyV20 stream generator.
            timeout: Maximum seconds to wait.

        Returns:
            The next dict from the generator.

        Raises:
            _TimeoutToken: If no message arrived within *timeout* seconds.
            StopIteration: If the generator is exhausted.
            StreamTerminated: If the generator was terminated.
            V20Error: On API errors from the generator.
        """
        result_q: queue.Queue[Any] = queue.Queue(maxsize=1)

        def _pull() -> None:
            try:
                result_q.put(("ok", next(gen)))
            except StopIteration:
                result_q.put(("stop", None))
            except StreamTerminated as exc:
                result_q.put(("terminated", exc))
            except V20Error as exc:
                result_q.put(("v20error", exc))
            except Exception as exc:  # pylint: disable=broad-except
                result_q.put(("error", exc))

        t = threading.Thread(target=_pull, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            # Thread is blocked in the generator — signal a timeout.
            raise _TimeoutToken()

        kind, value = result_q.get_nowait()
        if kind == "ok":
            return value  # type: ignore[no-any-return]
        if kind == "stop":
            raise StopIteration
        if kind == "terminated":
            raise StreamTerminated("stream terminated")
        if kind == "v20error":
            raise value
        raise value  # kind == "error"


class _TimeoutToken(BaseException):
    """Internal sentinel raised when `_next_with_timeout` times out.

    Uses BaseException (not Exception) so it cannot be accidentally caught by
    broad ``except Exception`` handlers in the generator path.
    """
