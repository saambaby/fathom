"""OANDA v20 REST client — candle endpoint only.

Scope: PoC (POC-T-02). No streaming, no order methods.

INV-03 compliance: all timestamps are UTC-aware datetime from first touch.
INV-08 compliance: token is read via SecretStr.get_secret_value(); never logged.
INV-09 compliance: environment (practice/live) is derived exclusively from
    settings.env — no stray env-var override.

D-01: uses oandapyV20 as the HTTP transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import oandapyV20
import oandapyV20.endpoints.instruments as oanda_instruments
from oandapyV20.exceptions import V20Error
from pydantic import AwareDatetime, BaseModel

from config.settings import Settings

# OANDA hard cap: maximum candles returned per single request.
_OANDA_MAX_PER_REQUEST: int = 500

# Map settings.env values to oandapyV20 environment identifiers (INV-09).
# oandapyV20 recognises "practice" and "live" — not "fxtrade".
_ENV_MAP: dict[str, str] = {
    "demo": "practice",
    "live": "live",
}


class OandaAPIError(Exception):
    """Raised when the OANDA v20 API returns an HTTP 4xx or 5xx response.

    Attributes
    ----------
    status_code : int
        The HTTP status code returned by OANDA.
    message : str
        The error message / body returned by OANDA.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"OANDA API error {status_code}: {message}")


class CandleRow(BaseModel):
    """A single OANDA candle bar with bid, ask, and mid prices.

    All price fields are floats. ``time`` is always UTC-aware (INV-03).
    """

    instrument: str
    granularity: str
    time: AwareDatetime     # UTC-aware (INV-03: naive datetime rejected by pydantic)
    # bid prices
    open_bid: float
    high_bid: float
    low_bid: float
    close_bid: float
    # ask prices
    open_ask: float
    high_ask: float
    low_ask: float
    close_ask: float
    # mid prices
    open_mid: float
    high_mid: float
    low_mid: float
    close_mid: float
    # metadata
    volume: int
    complete: bool


def _parse_utc(iso_string: str) -> datetime:
    """Parse an ISO 8601 UTC string from OANDA into a UTC-aware datetime.

    OANDA returns strings like ``"2024-01-15T14:00:00.000000000Z"``.
    Python's ``fromisoformat`` does not handle nanosecond precision or
    the trailing ``Z`` on all Python versions, so sub-second precision
    is truncated to microseconds before parsing, then UTC is attached
    explicitly.

    Args:
        iso_string: ISO 8601 UTC timestamp string from OANDA.

    Returns:
        A UTC-aware ``datetime`` object.
    """
    # Strip trailing "Z" and trim fractional seconds to at most 6 digits.
    # e.g. "2024-01-15T14:00:00.000000000Z" -> "2024-01-15T14:00:00.000000"
    # e.g. "2024-01-15T14:00:00Z"           -> "2024-01-15T14:00:00"
    s = iso_string.rstrip("Z")
    if "." in s:
        date_part, frac = s.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        s = f"{date_part}.{frac}"

    dt = datetime.fromisoformat(s)
    # OANDA always returns UTC; the string carries no offset, so attach it.
    return dt.replace(tzinfo=timezone.utc)


def _candle_to_row(
    instrument: str,
    granularity: str,
    candle: dict[str, Any],
) -> CandleRow:
    """Convert a single raw OANDA candle dict to a ``CandleRow``.

    Requires the candle to carry bid, ask, and mid sub-dicts (i.e. the
    request was issued with ``price="BAM"``).

    Args:
        instrument: The OANDA instrument identifier (e.g. ``"EUR_USD"``).
        granularity: The candle granularity (e.g. ``"H1"``).
        candle: A single candle dict from the OANDA response.

    Returns:
        A validated ``CandleRow`` instance with UTC-aware timestamp.
    """
    bid = candle["bid"]
    ask = candle["ask"]
    mid = candle["mid"]

    return CandleRow(
        instrument=instrument,
        granularity=granularity,
        time=_parse_utc(candle["time"]),
        open_bid=float(bid["o"]),
        high_bid=float(bid["h"]),
        low_bid=float(bid["l"]),
        close_bid=float(bid["c"]),
        open_ask=float(ask["o"]),
        high_ask=float(ask["h"]),
        low_ask=float(ask["l"]),
        close_ask=float(ask["c"]),
        open_mid=float(mid["o"]),
        high_mid=float(mid["h"]),
        low_mid=float(mid["l"]),
        close_mid=float(mid["c"]),
        volume=int(candle["volume"]),
        complete=bool(candle["complete"]),
    )


def _request_page(
    api: oandapyV20.API,
    instrument: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Issue a single ``InstrumentsCandles`` request and return the raw response.

    Args:
        api: An initialised ``oandapyV20.API`` instance.
        instrument: OANDA instrument identifier.
        params: Query parameters dict for ``InstrumentsCandles``.

    Returns:
        The raw response dict from OANDA.

    Raises:
        OandaAPIError: If OANDA returns HTTP 4xx or 5xx.
    """
    try:
        req = oanda_instruments.InstrumentsCandles(instrument, params=params)
        return api.request(req)  # type: ignore[no-any-return]
    except V20Error as exc:
        raise OandaAPIError(exc.code, exc.msg) from exc


class OandaClient:
    """OANDA v20 REST client — candle data only.

    Wraps ``oandapyV20`` to provide a typed, paginated interface.
    Authentication and environment selection are taken exclusively from
    ``settings`` (INV-09); the token is never logged (INV-08).

    Args:
        settings: A fully-initialised ``Settings`` instance.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        oanda_env = _ENV_MAP[settings.env]  # "practice" or "live" (INV-09)
        self._api = oandapyV20.API(
            access_token=settings.oanda_api_token.get_secret_value(),
            environment=oanda_env,
        )

    def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int,
        from_time: datetime | None = None,
    ) -> list[CandleRow]:
        """Fetch candle bars for an instrument, auto-paginating if needed.

        OANDA caps responses at 500 candles per request.  When ``count``
        exceeds 500 this method issues multiple sequential requests and
        concatenates the results.  Pagination advances via the ``from``
        query parameter: each subsequent page starts from the timestamp
        immediately after the last received candle, achieved by requesting
        ``page_size + 1`` candles anchored at the last candle's time and
        dropping the first (duplicate) result.

        Args:
            instrument: OANDA instrument identifier, e.g. ``"EUR_USD"``.
            granularity: OANDA granularity string, e.g. ``"H1"`` or ``"D"``.
            count: Total number of candles to retrieve (>= 1).
            from_time: Optional UTC-aware datetime for the start of the
                range.  If ``None``, OANDA returns the most recent candles.

        Returns:
            A list of ``CandleRow`` instances in ascending chronological
            order.  May be shorter than ``count`` if OANDA has exhausted
            the available data.

        Raises:
            OandaAPIError: If OANDA returns an HTTP 4xx or 5xx status.
            ValueError: If ``from_time`` is provided but is not UTC-aware.
        """
        if from_time is not None and from_time.tzinfo is None:
            raise ValueError(
                "from_time must be a UTC-aware datetime (INV-03). "
                "Use datetime(..., tzinfo=timezone.utc)."
            )

        rows: list[CandleRow] = []
        current_from = from_time
        first_page = True

        while len(rows) < count:
            needed = count - len(rows)

            # On pages after the first, we request one extra candle anchored
            # at the last received time.  OANDA "from" is inclusive, so the
            # first returned candle is the duplicate of the previous page's
            # last entry — we skip it below.
            request_count = min(
                needed if first_page else needed + 1,
                _OANDA_MAX_PER_REQUEST,
            )

            params: dict[str, Any] = {
                "granularity": granularity,
                "count": request_count,
                "price": "BAM",  # bid + ask + mid in a single call (D-01)
            }
            if current_from is not None:
                params["from"] = current_from.strftime(
                    "%Y-%m-%dT%H:%M:%S.000000000Z"
                )

            response = _request_page(self._api, instrument, params)
            raw_candles: list[dict[str, Any]] = response.get("candles", [])

            if not raw_candles:
                break  # OANDA returned nothing; no more data available.

            # On pages 2+: drop the first candle (duplicate of previous last).
            if not first_page:
                raw_candles = raw_candles[1:]

            if not raw_candles:
                # All we got back was the duplicate anchor candle — exhausted.
                break

            page_rows = [
                _candle_to_row(instrument, granularity, c)
                for c in raw_candles
            ]
            rows.extend(page_rows)

            # If OANDA returned fewer candles than the usable page slots, there
            # is no more data to fetch.  Evaluate this BEFORE clearing
            # first_page so that the first-page slot count (no anchor deduction)
            # is used correctly.  "usable_slots" on the first page equals
            # request_count; on subsequent pages it is request_count - 1
            # (the +1 anchor candle is not a net-new slot).
            usable_slots = request_count if first_page else request_count - 1
            first_page = False
            if len(raw_candles) < usable_slots:
                break

            # Advance the anchor to the last received candle's time.
            current_from = rows[-1].time

        return rows[:count]
