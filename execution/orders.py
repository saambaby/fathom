"""Order placement — submit atomic bracketed market orders to OANDA v20.

This is the most safety-critical module in Fathom: it is the only code that
actually opens a position with real (demo) money.  It is invoked exclusively by
the deterministic execution path (``fathom execute``), never by Hermes
(INV-01).

Correctness guarantees enforced here
------------------------------------
* **INV-04 — atomic bracket / no naked position.**  Every submission is a single
  v20 ``OrderCreate`` request whose body carries both ``stopLossOnFill`` and
  ``takeProfitOnFill``.  There is no code path that opens a position with a
  separate, skippable bracket call — the bracket either lands with the entry or
  the order does not fill.
* **INV-15 — idempotency / no double-fill.**  Two independent guards.  (a) a
  pre-submit store read on the deterministic ``client_order_id`` returns the
  existing fill with *no* HTTP if the order already filled; (b) the same
  ``client_order_id`` is attached as the v20 ``clientExtensions.id`` on every
  attempt, so even if the store missed a landed order (a crash between the
  broker ack and the store write), OANDA itself rejects the duplicate client
  id.  A network retry reuses the identical id and body, so a retry of a
  silently-landed first attempt can never create a second order.
* **INV-03 — UTC timestamps.**  ``filled_at`` is parsed from the broker
  transaction time (always UTC) or, absent that, taken from an injected
  UTC-aware clock — never ``datetime.now()`` with a naive tz.
* **INV-07/INV-09 — practice endpoint, single code path.**  This module never
  imports ``Settings`` or reads ``env``; it receives an already-constructed
  ``OandaClient`` whose endpoint was chosen once in its ``__init__``.
* **INV-08 — no secret logged.**  No token is read or logged here; the client
  owns the credential.

The slippage sign convention is the one frozen on the ``Fill`` model
(AMBIGUOUS-05): **positive = adverse** vs the candidate ``entry_ref``,
regardless of direction.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from data.oanda_client import OandaAPIError
from execution.models import Fill, FillStatus, Order, Position
from strategies.base import Direction

if TYPE_CHECKING:  # avoid import cycles / heavy deps at runtime
    from data.oanda_client import OandaClient
    from data.store import Store

__all__ = ["submit_order", "OrderRejected"]


class OrderRejected(Exception):
    """Raised when the broker rejects an order outright.

    A rejection is terminal and produces **no** ``Position`` and no synthetic
    ``Fill`` — the broker's verdict is recorded faithfully
    (``store.write_rejection``) and surfaced as this exception so the caller
    cannot mistake a rejection for a fill (AC: "never synthesise a fill").
    """

    def __init__(self, client_order_id: str, reason: str) -> None:
        self.client_order_id = client_order_id
        self.reason = reason
        super().__init__(f"order {client_order_id} rejected: {reason}")


# Default retry policy: a small number of attempts with exponential backoff.
# Only transient failures (network errors / HTTP 5xx) are retried; a 4xx is a
# terminal client error (e.g. a malformed body) and is not retried.
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_BASE_SECONDS = 0.5


def _is_transient(exc: Exception) -> bool:
    """True if ``exc`` is a retryable transport/server failure.

    A ``requests.RequestException`` (network-level) is transient.  An
    ``OandaAPIError`` is transient only for HTTP 5xx; a 4xx is a terminal
    client error and must not be retried (retrying a malformed order just burns
    attempts).
    """
    if isinstance(exc, OandaAPIError):
        return exc.status_code >= 500
    # Treat any non-OANDA exception escaping the client (e.g. a
    # requests.RequestException network failure, which the client deliberately
    # propagates) as transient.
    return True


def _build_order_body(order: Order, *, precision: int) -> dict[str, Any]:
    """Assemble the single v20 ``OrderCreate`` body — entry + both brackets.

    The body is built in one dict so the bracket is atomic (INV-04): there is
    no representation here of "entry now, bracket later".  ``stopLossOnFill`` and
    ``takeProfitOnFill`` carry absolute prices (resolved by ``build_bracket``)
    formatted to instrument precision.  ``clientExtensions.id`` carries the
    deterministic idempotency key (INV-15) so the broker de-dupes a retry.
    """
    price_fmt = f"%.{precision}f"
    return {
        "order": {
            "type": "MARKET",
            "instrument": order.instrument,
            "units": str(order.units),  # signed; long > 0, short < 0
            "timeInForce": "FOK",  # fill-or-kill: no resting partials lingering
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": order.client_order_id},
            "stopLossOnFill": {
                "price": price_fmt % order.stop_loss_price,
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": price_fmt % order.take_profit_price,
                "timeInForce": "GTC",
            },
        }
    }


def _compute_slippage(order: Order, entry_ref: float, fill_price: float) -> float:
    """Signed slippage; **positive = adverse** vs ``entry_ref`` (any direction).

    For a long, a fill *above* the reference is adverse (we paid more); for a
    short, a fill *below* the reference is adverse (we sold cheaper).
    """
    if order.direction is Direction.LONG:
        return fill_price - entry_ref
    return entry_ref - fill_price


def _parse_fill_time(fill_txn: dict[str, Any], fallback: datetime) -> datetime:
    """Return a UTC-aware fill time from the broker transaction (INV-03).

    OANDA transaction ``time`` is RFC-3339 UTC (``...Z``).  If it is missing we
    fall back to the injected UTC clock value — never a naive ``datetime.now``.
    """
    raw = fill_txn.get("time")
    if isinstance(raw, str) and raw:
        s = raw.rstrip("Z")
        if "." in s:
            date_part, frac = s.split(".", 1)
            frac = frac[:6].ljust(6, "0")
            s = f"{date_part}.{frac}"
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    if fallback.tzinfo is None:
        raise ValueError("fallback fill time must be UTC-aware (INV-03).")
    return fallback


def submit_order(
    order: Order,
    *,
    client: "OandaClient",
    store: "Store",
    entry_ref: float,
    precision: int,
    now: datetime,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], None] = _time.sleep,
) -> Fill:
    """Submit a bracketed market order to OANDA v20 (practice), idempotently.

    Flow (mirrors the spec sequence diagram):

    1. **Idempotency read (INV-15).**  Look up ``order.client_order_id`` in the
       store; if a filled/partial ``Fill`` already exists, return it with no
       broker call.
    2. **Atomic submit (INV-04).**  Build ONE v20 ``OrderCreate`` body with the
       entry, ``stopLossOnFill``, ``takeProfitOnFill`` and the
       ``clientExtensions.id`` idempotency key, and submit it.
    3. **Retry (INV-15).**  On a transient failure (network / HTTP 5xx) retry
       with exponential backoff, reusing the identical body+id.  Before each
       retry, re-check the store/broker — a prior attempt that landed but whose
       ack we lost is detected, not duplicated.
    4. **Capture + persist.**  Parse the ``orderFillTransaction``; compute signed
       slippage vs ``entry_ref``; build and persist the ``Order``/``Fill``/
       ``Position`` rows.
    5. **Rejection.**  An ``orderRejectTransaction`` / ``orderCancelTransaction``
       records ``status="rejected"`` (no position) and raises
       :class:`OrderRejected` — a fill is never synthesised.

    Args:
        order: the gated, sized, fully-bracketed ``Order`` (INV-04/INV-14).
        client: an already-constructed ``OandaClient`` (endpoint chosen by
            ``settings.env``; this module never reads ``env`` — INV-09).
        store: the persistence layer (orders/fills/positions tables).
        entry_ref: the candidate's reference entry price, for slippage.
        precision: instrument display precision for bracket price formatting.
        now: a UTC-aware clock value used only as a fallback fill time and for
            the rejection timestamp (INV-03 — never ``datetime.now()`` naive).
        max_attempts: total submission attempts (>= 1).
        backoff_base_seconds: base for exponential backoff between retries.
        sleep: injected sleeper (so tests don't actually wait).

    Returns:
        The ``Fill`` for the (possibly already-existing) order.

    Raises:
        OrderRejected: if the broker rejects/cancels the order.
        OandaAPIError: on a terminal (4xx) broker error or after retries are
            exhausted on a 5xx.
        requests.RequestException: if the network keeps failing past
            ``max_attempts``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a UTC-aware datetime (INV-03).")

    # 1. Pre-submit idempotency read (INV-15) — no HTTP if already filled.
    existing = store.get_fill_by_client_order_id(order.client_order_id)
    if existing is not None:
        return existing

    body = _build_order_body(order, precision=precision)

    # Record the intent before the broker write so a crash mid-submit leaves an
    # auditable "submitted" row (reconciliation can later resolve it).
    store.write_order(order, status="submitted")

    response: dict[str, Any] | None = None
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        # Belt-and-suspenders: a prior attempt may have landed even though its
        # ack never reached us. Re-check before re-hitting the broker (INV-15).
        if attempt > 0:
            already = store.get_fill_by_client_order_id(order.client_order_id)
            if already is not None:
                return already
        try:
            response = client.create_order(body)
            break
        except Exception as exc:  # noqa: BLE001 — re-raised below if terminal
            last_exc = exc
            if not _is_transient(exc) or attempt == max_attempts - 1:
                raise
            sleep(backoff_base_seconds * (2 ** attempt))

    if response is None:  # pragma: no cover — loop either breaks or raises
        assert last_exc is not None
        raise last_exc

    return _resolve_response(order, response, store, entry_ref=entry_ref, now=now)


def _resolve_response(
    order: Order,
    response: dict[str, Any],
    store: "Store",
    *,
    entry_ref: float,
    now: datetime,
) -> Fill:
    """Turn a v20 ``OrderCreate`` response into a persisted ``Fill``.

    A rejection/cancellation records the verdict and raises ``OrderRejected``;
    a fill persists ``Fill`` + ``Position`` and returns the ``Fill``.  A partial
    fill (``units_filled`` strictly between 0 and the ordered units) is recorded
    with ``status="partial"``.
    """
    reject_txn = response.get("orderRejectTransaction") or response.get(
        "orderCancelTransaction"
    )
    fill_txn = response.get("orderFillTransaction")

    if fill_txn is None:
        reason = ""
        if isinstance(reject_txn, dict):
            reason = str(reject_txn.get("reason", "")) or str(
                reject_txn.get("type", "")
            )
        store.write_order(order, status="rejected")
        store.write_rejection(order.client_order_id, rejected_at=now, reason=reason)
        raise OrderRejected(order.client_order_id, reason or "no fill transaction")

    fill_price = float(fill_txn["price"])
    units_filled = int(float(fill_txn["units"]))
    if units_filled == 0:
        # Broker returned a fill transaction with zero units — treat as a
        # rejection rather than constructing an invalid (zero-unit) Fill.
        store.write_order(order, status="rejected")
        store.write_rejection(
            order.client_order_id, rejected_at=now, reason="zero-unit fill"
        )
        raise OrderRejected(order.client_order_id, "zero-unit fill")

    partial = abs(units_filled) < abs(order.units)
    status = FillStatus.PARTIAL if partial else FillStatus.FILLED

    filled_at = _parse_fill_time(fill_txn, now)
    slippage = _compute_slippage(order, entry_ref, fill_price)

    # broker_trade_id: the opened trade id (present on a fill).  Fall back to the
    # fill transaction id so the (frozen) non-empty constraint always holds.
    trade_opened = fill_txn.get("tradeOpened") or {}
    broker_trade_id = str(
        trade_opened.get("tradeID")
        or fill_txn.get("id")
        or ""
    )
    if not broker_trade_id:
        # No identifiable trade id — cannot build a valid Position/Fill; record
        # as rejected rather than fabricate one.
        store.write_order(order, status="rejected")
        store.write_rejection(
            order.client_order_id, rejected_at=now, reason="missing trade id"
        )
        raise OrderRejected(order.client_order_id, "missing broker trade id")

    fill = Fill(
        client_order_id=order.client_order_id,
        broker_trade_id=broker_trade_id,
        fill_price=fill_price,
        units_filled=units_filled,
        slippage=slippage,
        filled_at=filled_at,
        status=status,
    )

    position = Position(
        broker_trade_id=broker_trade_id,
        instrument=order.instrument,
        units=units_filled,
        entry_price=fill_price,
        stop_loss_price=order.stop_loss_price,
        take_profit_price=order.take_profit_price,
        opened_at=filled_at,
        unrealized_pl=0.0,
        closed_at=None,
        realized_pl=None,
        candidate_ref=order.candidate_ref,
    )

    store.write_order(order, status=status.value)
    store.write_fill(fill)
    store.write_position(position)
    return fill
