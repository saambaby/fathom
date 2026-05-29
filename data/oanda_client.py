"""OANDA v20 REST client — candle and instrument-metadata endpoints.

Scope: Phase 1 (P1A-T-01 data-layer-expansion). No streaming, no order methods.

INV-03 compliance: all timestamps are UTC-aware datetime from first touch.
INV-08 compliance: token is read via SecretStr.get_secret_value(); never logged.
INV-09 compliance: environment (practice/live) is derived exclusively from
    settings.env — no stray env-var override.

D-01: uses oandapyV20 as the HTTP transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List

import oandapyV20
import oandapyV20.endpoints.accounts as oanda_accounts
import oandapyV20.endpoints.instruments as oanda_instruments
import oandapyV20.endpoints.orders as oanda_orders
import oandapyV20.endpoints.trades as oanda_trades
from oandapyV20.exceptions import V20Error
from pydantic import AwareDatetime, BaseModel, field_validator

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


class InstrumentMeta(BaseModel):
    """Instrument metadata from the OANDA accounts/instruments endpoint.

    Canonical financing field names are ``long_rate`` / ``short_rate``
    (the swap-cost model maps these to ``CostParams.swap_long_rate`` /
    ``swap_short_rate`` at the engine boundary — see AMBIGUOUS-01 audit).

    Wire-format coercion happens here at the boundary:
    - ``pipLocation`` (int from OANDA) → ``pip_location``.
    - Financing rates arrive as decimal strings (e.g. ``"-0.0002"``);
      they are coerced to ``float`` by the field validators below.
    - ``financing_days_of_week`` is a list of weekday-number ints (0=Mon).

    INV-09: the instrument list is account-scoped (fetched via the account
    endpoint, not the public instruments list).
    """

    name: str
    """OANDA instrument identifier, e.g. ``"EUR_USD"``."""

    pip_location: int
    """Pip exponent.  −4 for most majors; −2 for JPY pairs."""

    min_trade_size: float
    """Minimum trade size in units (coerced from OANDA string)."""

    margin_rate: float
    """Required margin rate (coerced from OANDA decimal string)."""

    display_precision: int
    """Number of decimal places for display."""

    long_rate: float
    """Daily financing rate for long positions (canonical name; coerced from
    OANDA's ``longRate`` decimal string)."""

    short_rate: float
    """Daily financing rate for short positions (canonical name; coerced from
    OANDA's ``shortRate`` decimal string)."""

    financing_days_of_week: List[int]
    """Weekday numbers (0=Mon … 6=Sun) on which financing is charged.
    Most FX instruments charge 3× on Wednesday."""

    @field_validator("min_trade_size", "margin_rate", mode="before")
    @classmethod
    def _coerce_float_str(cls, v: object) -> float:
        """Coerce OANDA string fields to float at the boundary."""
        return float(v)  # type: ignore[arg-type]

    @field_validator("long_rate", "short_rate", mode="before")
    @classmethod
    def _coerce_rate_str(cls, v: object) -> float:
        """Coerce OANDA financing rate strings to float."""
        return float(v)  # type: ignore[arg-type]


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


def _instrument_from_raw(raw: dict[str, Any]) -> InstrumentMeta:
    """Convert a single raw OANDA instrument dict to an ``InstrumentMeta``.

    Handles the OANDA wire-format field names (camelCase) and coerces
    string rates to float at the boundary.

    Args:
        raw: A single instrument dict from ``AccountInstruments`` response.

    Returns:
        A validated ``InstrumentMeta`` instance.
    """
    financing = raw.get("financing", {})
    # financing_days_of_week is a list of dicts: [{"dayOfWeek": "MONDAY", ...}]
    # or a list of ints in some API versions. We normalise to int (0=Mon…6=Sun).
    days_raw = financing.get("financingDaysOfWeek", [])
    _DAY_MAP: dict[str, int] = {
        "MONDAY": 0,
        "TUESDAY": 1,
        "WEDNESDAY": 2,
        "THURSDAY": 3,
        "FRIDAY": 4,
        "SATURDAY": 5,
        "SUNDAY": 6,
    }
    days: list[int] = []
    for entry in days_raw:
        if isinstance(entry, dict):
            day_str = entry.get("dayOfWeek", "")
            if day_str in _DAY_MAP:
                # We store the unique weekday number once; daysCharged=3 means
                # 3 days of financing are charged on that single day — the cost
                # model handles the multiplier, so we do not capture it here.
                days.append(_DAY_MAP[day_str])
        elif isinstance(entry, int):
            days.append(entry)

    return InstrumentMeta(
        name=raw["name"],
        pip_location=int(raw["pipLocation"]),
        min_trade_size=raw["minimumTradeSize"],
        margin_rate=raw["marginRate"],
        display_precision=int(raw["displayPrecision"]),
        long_rate=financing.get("longRate", "0"),
        short_rate=financing.get("shortRate", "0"),
        financing_days_of_week=days,
    )


class OandaClient:
    """OANDA v20 REST client — candle data and instrument metadata.

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

    def list_instruments(self) -> list[InstrumentMeta]:
        """Fetch the full tradeable FX instrument list for this account.

        Calls ``GET /v3/accounts/{accountID}/instruments`` via
        ``oandapyV20.endpoints.accounts.AccountInstruments``.  Only FX
        instruments (``type == "CURRENCY"``) are returned; other asset
        classes (CFDs, metals, crypto) are filtered out.

        Results should be cached by the caller (e.g. via
        ``Store.upsert_instruments`` / ``Store.load_instruments``) to
        avoid re-fetching on every run.

        Returns:
            A list of ``InstrumentMeta`` instances, one per tradeable FX
            instrument.  Order is unspecified (matches OANDA response order).

        Raises:
            OandaAPIError: If OANDA returns HTTP 4xx or 5xx.
        """
        account_id = self._settings.oanda_account_id
        try:
            req = oanda_accounts.AccountInstruments(accountID=account_id)
            response = self._api.request(req)
        except V20Error as exc:
            raise OandaAPIError(exc.code, exc.msg) from exc

        raw_instruments: list[dict[str, Any]] = response.get("instruments", [])
        return [
            _instrument_from_raw(r)
            for r in raw_instruments
            if r.get("type") == "CURRENCY"
        ]

    # ------------------------------------------------------------------
    # Order placement + account summary (Phase 3 — execution path)
    # ------------------------------------------------------------------

    def create_order(self, order_body: dict[str, Any]) -> dict[str, Any]:
        """Submit one v20 order-create request and return the raw response.

        This is the single broker-write entry point used by
        ``execution/orders.py``.  The ``order_body`` is the fully-formed v20
        ``OrderCreate`` payload — a market order **with** its
        ``stopLossOnFill`` and ``takeProfitOnFill`` brackets and a
        ``clientExtensions.id`` idempotency key, assembled by the caller in a
        single dict so the bracket is atomic (INV-04): there is no separate
        bracket call this client could skip.

        INV-07/INV-09: the practice-vs-live endpoint is selected once, here,
        from ``settings.env`` in ``__init__``; ``orders.py`` never reads ``env``
        or the token.

        Transient/network failures (``requests.RequestException``) are **not**
        swallowed — they propagate so the caller's retry-with-backoff layer can
        reuse the same ``clientExtensions.id`` (a retry of a landed order
        de-dupes; INV-15).  HTTP 4xx/5xx surface as ``OandaAPIError`` exactly
        like the candle path, carrying the broker status code so the caller can
        distinguish a retryable 5xx from a terminal 4xx rejection.

        Args:
            order_body: the v20 ``OrderCreate`` request body
                (``{"order": {...}}``), already carrying brackets + the
                ``clientExtensions`` idempotency id.

        Returns:
            The raw OANDA response dict (``orderFillTransaction`` /
            ``orderCancelTransaction`` / ``orderRejectTransaction`` etc.).

        Raises:
            OandaAPIError: on any HTTP 4xx/5xx from OANDA.
            requests.RequestException: on a network-level transport failure
                (propagated for the retry layer).
        """
        account_id = self._settings.oanda_account_id
        try:
            req = oanda_orders.OrderCreate(accountID=account_id, data=order_body)
            return self._api.request(req)  # type: ignore[no-any-return]
        except V20Error as exc:
            raise OandaAPIError(exc.code, exc.msg) from exc

    def account_summary(self) -> dict[str, Any]:
        """Fetch the OANDA account summary (equity, balance, open P&L).

        Calls ``GET /v3/accounts/{accountID}/summary``.  Used by the execution
        path / kill switch (INV-16: the broker is the source of truth for
        equity and realised day P&L).  Practice/live is selected once from
        ``settings.env`` (INV-09).

        Returns:
            The raw OANDA account-summary response dict.

        Raises:
            OandaAPIError: on any HTTP 4xx/5xx from OANDA.
        """
        account_id = self._settings.oanda_account_id
        try:
            req = oanda_accounts.AccountSummary(accountID=account_id)
            return self._api.request(req)  # type: ignore[no-any-return]
        except V20Error as exc:
            raise OandaAPIError(exc.code, exc.msg) from exc

    def open_trades(self) -> list[dict[str, Any]]:
        """Fetch the broker's currently-open trades (INV-16 source of truth).

        Calls ``GET /v3/accounts/{accountID}/openTrades`` via
        ``oandapyV20.endpoints.trades.OpenTrades``.  Used by
        ``execution/reconcile.py`` to diff the broker's view of open positions
        against the store ``positions`` table — the broker wins on any
        disagreement (INV-16).  Practice/live is selected once from
        ``settings.env`` (INV-09); the token is never logged (INV-08).

        Returns:
            The raw list of v20 trade dicts (the ``trades`` array of the
            response).  Each dict carries ``id`` (broker trade id),
            ``instrument``, ``currentUnits``, ``price`` (entry), ``unrealizedPL``,
            ``realizedPL``, ``openTime`` and (when bracketed) ``stopLossOrder`` /
            ``takeProfitOrder`` sub-objects.  An account with no open trades
            yields an empty list.

        Raises:
            OandaAPIError: on any HTTP 4xx/5xx from OANDA.
        """
        account_id = self._settings.oanda_account_id
        try:
            req = oanda_trades.OpenTrades(accountID=account_id)
            response = self._api.request(req)
        except V20Error as exc:
            raise OandaAPIError(exc.code, exc.msg) from exc
        trades: list[dict[str, Any]] = response.get("trades", [])
        return trades

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
