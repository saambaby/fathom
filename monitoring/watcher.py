"""Deviation monitor — always-on watcher for open positions (P3-T-08).

Watches live ticks from the Phase 1B ``PriceStream`` against open positions
loaded from the store.  Evaluates four deviation rules per position on every
tick and emits a ``DeviationEvent`` to the injected ``alerter`` when a rule
fires.  Debounce per (position, rule) prevents alert storms.

Invariants
----------
* **INV-01** — the watcher never opens a position; auto-response is
  default-off and is delegated to a deterministic ``execution/`` function
  (close or modify of an *existing* position).  The watcher is not a Hermes
  tool and never calls the v20 order API inline.
* **INV-03** — all ``DeviationEvent.created_at`` timestamps are UTC-aware.
* **AMBIGUOUS-01 resolution** — ``alert_only`` (default) | ``auto_flatten`` |
  ``tighten_stop``, behind a default-off config flag.  On demo the default is
  alert-only.

Four deviation rules
--------------------
1. **adverse** — the current price has moved adversely past a configurable
   fraction of the stop distance from entry.
2. **slippage** — the fill slippage on record for this position exceeds the
   threshold.
3. **vol** — the recent price range (high-low over the last ``vol_lookback``
   ticks) exceeds ``vol_atr_multiplier`` times the ATR at open (derived from
   ``stop_distance`` as a proxy).
4. **feed_health** — no tick has arrived within ``heartbeat_timeout_seconds``
   (reuses the gap-detected signal from the Phase 1B stream, and checks
   elapsed wall-clock time).

Debounce
--------
Each (position, rule) key is debounced: once an event fires, it will not
re-fire until ``debounce_seconds`` has elapsed.  ``event_id`` is a 32-char
SHA-256 prefix over the combination of (broker_trade_id, rule, window-start)
for position rules, or (instrument, rule, window-start) for feed_health events
(which have no broker_trade_id).  Using the instrument in the feed_health key
ensures that each instrument debounces its own feed-health stream
independently — a stale feed on one instrument never suppresses the alert for
another.  Re-persistence is idempotent within a window.

Alerter protocol
----------------
The ``Alerter`` protocol is minimal — just ``send(event: DeviationEvent)``.
The concrete implementation (Discord / log) lives in monitor-alerts (T-09).

Auto-response (default-off, INV-01)
-----------------------------------
``WatcherConfig.severe_response`` controls the response to a ``severe``-
severity deviation:
* ``"alert_only"`` (default) — emit the event only; no execution call.
* ``"auto_flatten"`` — emit + call the delegated ``execution_responder``
  with ``("flatten", position)``.
* ``"tighten_stop"`` — emit + call with ``("tighten_stop", position)``.

The ``execution_responder`` is injected and defaults to a no-op stub so the
watcher is safe when auto-response is disabled (the default).
"""

from __future__ import annotations

import hashlib
import logging
import queue
import time
from datetime import datetime, timezone
from typing import Callable, Literal, Protocol, Sequence

import pandas as pd
from pydantic import AwareDatetime, BaseModel, field_validator

from data.stream import PriceTick

logger = logging.getLogger("fathom.monitoring.watcher")

# ---------------------------------------------------------------------------
# DeviationEvent — the producer (T-09 monitor-alerts consumes this shape)
# ---------------------------------------------------------------------------

DeviationType = Literal["adverse", "slippage", "vol", "feed_health"]
SeverityLevel = Literal["info", "warn", "severe"]
SevereResponse = Literal["alert_only", "auto_flatten", "tighten_stop"]


class DeviationEvent(BaseModel):
    """A single deviation event emitted by the watcher (INV-03, INV-14 analogue).

    This is the producer shape.  ``monitor-alerts`` (T-09) consumes it.  Field
    names, types, and flat shape are frozen once this ships (analogous to INV-13
    for Candidate and INV-14 for Order/Fill/Position).

    Fields
    ------
    event_id         : str — stable per (broker_trade_id, rule,
                       debounce-window-start) so re-persistence is idempotent.
                       ``sha256(broker_trade_id|deviation_type|window_ts)[:32]``.
    instrument       : str — OANDA instrument identifier.
    deviation_type   : ``"adverse"`` | ``"slippage"`` | ``"vol"`` |
                       ``"feed_health"``.
    detail           : str — short human-readable figure, e.g.
                       ``"adverse 0.00150 (threshold 0.00120)"``.
    broker_trade_id  : str | None — ``None`` for feed_health events (no
                       position context).
    severity         : ``"info"`` | ``"warn"`` | ``"severe"``.
    created_at       : UTC-aware datetime (INV-03).
    """

    event_id: str
    instrument: str
    deviation_type: DeviationType
    detail: str
    broker_trade_id: str | None = None
    severity: SeverityLevel
    created_at: AwareDatetime  # UTC-aware (INV-03; pydantic rejects naive)

    @field_validator("event_id", "instrument", "detail")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "created_at must be UTC-aware (INV-03). "
                "Use datetime.now(timezone.utc), never datetime.now()."
            )
        return v


# ---------------------------------------------------------------------------
# Alerter protocol — T-09 implements the concrete delivery; watcher uses this
# ---------------------------------------------------------------------------


class Alerter(Protocol):
    """Minimal alerter interface.  T-09 implements Discord/log delivery.

    The watcher injects any object that satisfies this protocol — duck-typed,
    not subclassed.  A no-op stub is provided for tests and as the default.
    """

    def send(self, event: DeviationEvent) -> None:
        """Deliver a deviation event to the downstream channel."""
        ...


class NoOpAlerter:
    """A silent alerter stub (default when no real alerter is injected)."""

    def send(self, event: DeviationEvent) -> None:  # noqa: D401
        """No-op: discard the event (use in tests or when alerter not wired)."""
        logger.debug("NoOpAlerter.send: %s", event.model_dump())


# ---------------------------------------------------------------------------
# Execution responder protocol — thin call-through for auto-response (INV-01)
# ---------------------------------------------------------------------------


class ExecutionResponder(Protocol):
    """Thin call-through to execution/ for auto-response (default-off, INV-01).

    The watcher never calls the v20 order API inline.  On ``auto_flatten`` or
    ``tighten_stop``, it calls this injected responder with
    ``(action, position)`` — the responder is the bridge to
    ``execution/orders.py`` close/modify functions (to be implemented when
    those functions ship).  Default is a no-op stub.
    """

    def respond(
        self,
        action: Literal["flatten", "tighten_stop"],
        broker_trade_id: str,
        instrument: str,
    ) -> None:
        """Delegate the auto-response to the execution layer (INV-01)."""
        ...


class NoOpExecutionResponder:
    """Default no-op responder (default-off; INV-01: never opens a position)."""

    def respond(
        self,
        action: Literal["flatten", "tighten_stop"],
        broker_trade_id: str,
        instrument: str,
    ) -> None:
        logger.debug(
            "NoOpExecutionResponder: action=%s trade=%s instrument=%s (no-op)",
            action,
            broker_trade_id,
            instrument,
        )


# ---------------------------------------------------------------------------
# WatcherConfig — thresholds, cadence, response policy
# ---------------------------------------------------------------------------


class WatcherConfig(BaseModel):
    """Configuration for the Watcher.

    All thresholds have sensible defaults for demo use.

    Fields
    ------
    adverse_fraction         : float — fraction of stop distance that
                               constitutes an adverse excursion alert.
                               Default 0.5 (halfway to stop → warn).
    slippage_threshold       : float — absolute slippage beyond which a
                               slippage event fires.  Default 0.0002 (2 pips
                               for 5-dp pairs like EUR_USD).
    vol_atr_multiplier       : float — range expansion factor above the
                               inferred ATR (stop_distance as proxy) that
                               triggers a vol spike event.  Default 2.0.
    vol_lookback             : int — number of recent ticks over which to
                               compute the high-low range for the vol rule.
                               Default 20.
    heartbeat_timeout_seconds: float — wall-clock seconds with no tick before
                               a feed_health event fires.  Default 15.0
                               (slightly above the Phase 1B 10 s stream
                               timeout so the watcher fires after the stream
                               has already tried to reconnect).
    reconcile_interval_seconds: float — how often to refresh open positions
                               from the store (not the broker — the store is
                               reconciled separately).  Default 60.0.
    debounce_seconds         : float — minimum seconds between repeated events
                               for the same (broker_trade_id, rule).  Default
                               300.0 (5 minutes).
    severe_response          : ``"alert_only"`` (default) | ``"auto_flatten"``
                               | ``"tighten_stop"``.  Controls what the watcher
                               does beyond emitting the event when severity is
                               ``"severe"``.  Default is alert-only (safe for
                               demo, INV-01).
    """

    adverse_fraction: float = 0.5
    slippage_threshold: float = 0.0002
    vol_atr_multiplier: float = 2.0
    vol_lookback: int = 20
    heartbeat_timeout_seconds: float = 15.0
    reconcile_interval_seconds: float = 60.0
    debounce_seconds: float = 300.0
    severe_response: SevereResponse = "alert_only"

    @field_validator("adverse_fraction", "slippage_threshold", "vol_atr_multiplier")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"threshold must be > 0, got {v}")
        return v

    @field_validator("vol_lookback")
    @classmethod
    def _lookback_positive(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"vol_lookback must be >= 2, got {v}")
        return v

    @field_validator("heartbeat_timeout_seconds", "reconcile_interval_seconds", "debounce_seconds")
    @classmethod
    def _seconds_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"timeout/interval must be > 0, got {v}")
        return v


# ---------------------------------------------------------------------------
# PositionSnapshot — lightweight copy of what the watcher tracks per position
# ---------------------------------------------------------------------------


class PositionSnapshot(BaseModel):
    """A position the watcher tracks per tick.

    Separate from ``execution.models.Position`` so the watcher can be tested
    without importing the full execution package and to allow independent
    evolution.  Fields mirror those of ``Position`` that the rules need.
    """

    broker_trade_id: str
    instrument: str
    units: int           # signed (long > 0, short < 0)
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    fill_slippage: float = 0.0  # from the fill record; 0 if not available


# ---------------------------------------------------------------------------
# Pure rule predicates
# ---------------------------------------------------------------------------


def _event_id(
    broker_trade_id: str | None,
    deviation_type: str,
    window_ts: str,
    instrument: str | None = None,
) -> str:
    """Compute a stable, idempotent event_id for a (position, rule, window).

    For feed_health events (broker_trade_id is None) the ``instrument`` is
    included so that each instrument produces an independent id and each
    instrument's own debounce stream is isolated from other instruments.
    """
    if broker_trade_id is not None:
        key = f"{broker_trade_id}|{deviation_type}|{window_ts}"
    else:
        # feed_health path: scope the key to the instrument
        key = f"feed_health|{instrument or ''}|{deviation_type}|{window_ts}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _debounce_window_ts(now: datetime, debounce_seconds: float) -> str:
    """Return a string representing the debounce window the timestamp falls in."""
    # Floor to the nearest debounce_seconds boundary from epoch.
    epoch_s = now.timestamp()
    window_start = int(epoch_s // debounce_seconds) * debounce_seconds
    return str(int(window_start))


def check_adverse(
    position: PositionSnapshot,
    current_price: float,
    config: WatcherConfig,
) -> DeviationEvent | None:
    """Fire if the current price has moved adversely past ``adverse_fraction``
    of the stop distance.

    For a LONG position: adverse = price below entry by >= fraction * stop_dist.
    For a SHORT position: adverse = price above entry by >= fraction * stop_dist.

    Returns a ``DeviationEvent`` (not yet emitted — caller handles debounce/emit),
    or ``None`` if the rule does not fire.
    """
    stop_dist = abs(position.entry_price - position.stop_loss_price)
    if stop_dist <= 0:
        return None  # degenerate position; cannot evaluate

    threshold = config.adverse_fraction * stop_dist
    is_long = position.units > 0

    if is_long:
        excursion = position.entry_price - current_price  # positive = down
    else:
        excursion = current_price - position.entry_price  # positive = up

    if excursion < threshold:
        return None

    # Severity: warn by default; severe if excursion >= stop distance itself.
    severity: SeverityLevel = "severe" if excursion >= stop_dist else "warn"

    now = datetime.now(timezone.utc)
    window_ts = _debounce_window_ts(now, config.debounce_seconds)
    return DeviationEvent(
        event_id=_event_id(position.broker_trade_id, "adverse", window_ts),
        instrument=position.instrument,
        deviation_type="adverse",
        detail=(
            f"adverse excursion {excursion:.5f} "
            f"(threshold {threshold:.5f}, stop_dist {stop_dist:.5f})"
        ),
        broker_trade_id=position.broker_trade_id,
        severity=severity,
        created_at=now,
    )


def check_slippage(
    position: PositionSnapshot,
    config: WatcherConfig,
) -> DeviationEvent | None:
    """Fire if the fill slippage recorded for this position exceeds the threshold.

    Slippage sign convention: positive = adverse (per INV-14 Fill model).
    """
    if position.fill_slippage <= config.slippage_threshold:
        return None

    severity: SeverityLevel = (
        "severe"
        if position.fill_slippage >= config.slippage_threshold * 3
        else "warn"
    )

    now = datetime.now(timezone.utc)
    window_ts = _debounce_window_ts(now, config.debounce_seconds)
    return DeviationEvent(
        event_id=_event_id(position.broker_trade_id, "slippage", window_ts),
        instrument=position.instrument,
        deviation_type="slippage",
        detail=(
            f"fill slippage {position.fill_slippage:.5f} "
            f"exceeds threshold {config.slippage_threshold:.5f}"
        ),
        broker_trade_id=position.broker_trade_id,
        severity=severity,
        created_at=now,
    )


def check_vol(
    position: PositionSnapshot,
    recent_prices: Sequence[float],
    config: WatcherConfig,
) -> DeviationEvent | None:
    """Fire if the recent price range exceeds ``vol_atr_multiplier`` times the
    ATR proxy (stop distance as inferred ATR).

    The ATR at open is proxied by ``stop_distance`` (the position was sized by
    an ATR-derived stop per INV-11).  Range = max(prices) - min(prices) over
    the last ``vol_lookback`` ticks.

    Returns ``None`` if fewer than 2 prices are available (not enough data).
    """
    if len(recent_prices) < 2:
        return None

    window = list(recent_prices)[-config.vol_lookback:]
    if len(window) < 2:
        return None

    price_range = max(window) - min(window)
    stop_dist = abs(position.entry_price - position.stop_loss_price)
    if stop_dist <= 0:
        return None  # degenerate; cannot compare

    threshold = config.vol_atr_multiplier * stop_dist
    if price_range < threshold:
        return None

    severity: SeverityLevel = (
        "severe"
        if price_range >= threshold * 2
        else "warn"
    )

    now = datetime.now(timezone.utc)
    window_ts = _debounce_window_ts(now, config.debounce_seconds)
    return DeviationEvent(
        event_id=_event_id(position.broker_trade_id, "vol", window_ts),
        instrument=position.instrument,
        deviation_type="vol",
        detail=(
            f"vol range {price_range:.5f} "
            f"exceeds {config.vol_atr_multiplier}× stop_dist "
            f"({threshold:.5f})"
        ),
        broker_trade_id=position.broker_trade_id,
        severity=severity,
        created_at=now,
    )


def check_feed_health(
    instrument: str,
    last_tick_time: float,
    config: WatcherConfig,
) -> DeviationEvent | None:
    """Fire if no tick has arrived within ``heartbeat_timeout_seconds``.

    ``last_tick_time`` is a ``time.monotonic()`` value set on every tick.
    Returns a feed_health event (no broker_trade_id) if stale.
    """
    elapsed = time.monotonic() - last_tick_time
    if elapsed < config.heartbeat_timeout_seconds:
        return None

    now = datetime.now(timezone.utc)
    window_ts = _debounce_window_ts(now, config.debounce_seconds)
    return DeviationEvent(
        event_id=_event_id(None, "feed_health", window_ts, instrument=instrument),
        instrument=instrument,
        deviation_type="feed_health",
        detail=f"no tick for {elapsed:.1f}s (timeout {config.heartbeat_timeout_seconds}s)",
        broker_trade_id=None,
        severity="severe",
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Watcher — the always-on loop
# ---------------------------------------------------------------------------


class Watcher:
    """Always-on deviation watcher.

    Consumes ticks from a queue (or any iterable of ``PriceTick``), refreshes
    open positions from the store periodically, evaluates the four deviation
    rules per position on every tick, debounces, and emits ``DeviationEvent``
    objects to the injected ``alerter``.

    Args:
        tick_source         : iterable of ``PriceTick`` (or a
                              ``queue.Queue[PriceTick | None]`` where ``None``
                              is the shutdown sentinel).  Typically the live
                              ``PriceStream`` but any iterable works for tests.
        store_loader        : callable ``() -> list[PositionSnapshot]``.
                              Called every ``config.reconcile_interval_seconds``
                              to refresh open positions.  Wraps
                              ``store.load_open_positions()`` in production.
        alerter             : any object with ``send(DeviationEvent) -> None``.
                              Default is ``NoOpAlerter``.
        config              : ``WatcherConfig`` (defaults are safe for demo).
        execution_responder : injected responder for auto-response actions
                              (default-off, INV-01).  Default is
                              ``NoOpExecutionResponder``.
        instruments         : list of instruments to track for feed-health.
                              Defaults to ``["EUR_USD"]``.
    """

    def __init__(
        self,
        *,
        tick_source: "queue.Queue[PriceTick | None] | None" = None,
        tick_iterable: "list[PriceTick] | None" = None,
        store_loader: Callable[[], list[PositionSnapshot]] | None = None,
        alerter: Alerter | None = None,
        config: WatcherConfig | None = None,
        execution_responder: ExecutionResponder | None = None,
        instruments: list[str] | None = None,
    ) -> None:
        self._tick_queue: "queue.Queue[PriceTick | None] | None" = tick_source
        self._tick_iterable: "list[PriceTick] | None" = tick_iterable
        self._store_loader = store_loader or (lambda: [])
        self._alerter: Alerter = alerter or NoOpAlerter()
        self._config = config or WatcherConfig()
        self._responder: ExecutionResponder = execution_responder or NoOpExecutionResponder()
        self._instruments = instruments or ["EUR_USD"]

        # Debounce state: maps (broker_trade_id_or_instrument, rule) → last event_id fired
        # For position rules: key is (broker_trade_id, deviation_type).
        # For feed_health (broker_trade_id is None): key is (instrument, "feed_health")
        # so each instrument maintains an independent debounce slot.
        self._debounce: dict[tuple[str | None, str], str] = {}

        # Per-instrument recent prices (for vol rule)
        self._recent_prices: dict[str, list[float]] = {}

        # Open positions (refreshed periodically)
        self._positions: list[PositionSnapshot] = []
        self._last_reconcile: float = 0.0

        # Feed-health: last tick arrival time per instrument
        self._last_tick_time: dict[str, float] = {i: time.monotonic() for i in self._instruments}

    def run(self) -> None:
        """Main loop: consume ticks, evaluate rules, emit events.

        Exits when the tick source signals done (``None`` sentinel from queue or
        iterable exhausted).  A feed-health check is also performed on each tick
        (and after each no-tick timeout).

        This method blocks until the source is exhausted or ``stop()`` is called.
        """
        self._last_reconcile = time.monotonic() - self._config.reconcile_interval_seconds
        self._positions = self._store_loader()

        if self._tick_iterable is not None:
            self._run_from_iterable()
        elif self._tick_queue is not None:
            self._run_from_queue()
        else:
            logger.warning("Watcher.run(): no tick source provided — exiting immediately")

    def _run_from_iterable(self) -> None:
        """Consume ticks from a pre-built iterable (tests / replay mode)."""
        assert self._tick_iterable is not None
        for tick in self._tick_iterable:
            self._on_tick(tick)
        # After iterable exhausted, do one final feed-health pass.
        self._check_all_feed_health()

    def _run_from_queue(self) -> None:
        """Consume ticks from a queue; ``None`` sentinel shuts down."""
        assert self._tick_queue is not None
        while True:
            try:
                item = self._tick_queue.get(
                    timeout=self._config.heartbeat_timeout_seconds
                )
            except queue.Empty:
                # Timeout — check feed health
                self._check_all_feed_health()
                continue

            if item is None:
                # Shutdown sentinel
                break

            self._on_tick(item)

    def _on_tick(self, tick: PriceTick) -> None:
        """Process one tick: update state, evaluate rules, emit events."""
        instrument = tick.instrument
        mid = (tick.bid + tick.ask) / 2.0

        # Update last-tick time for feed-health (using monotonic clock)
        self._last_tick_time[instrument] = time.monotonic()

        # Update recent prices for this instrument
        prices = self._recent_prices.setdefault(instrument, [])
        prices.append(mid)
        # Keep a bounded window: 2× vol_lookback to avoid unbounded memory
        max_prices = self._config.vol_lookback * 2
        if len(prices) > max_prices:
            self._recent_prices[instrument] = prices[-max_prices:]

        # Periodic position refresh
        now_mono = time.monotonic()
        if now_mono - self._last_reconcile >= self._config.reconcile_interval_seconds:
            self._positions = self._store_loader()
            self._last_reconcile = now_mono

        # Evaluate rules for each open position on this instrument
        for pos in self._positions:
            if pos.instrument != instrument:
                continue
            self._evaluate_position(pos, mid)

        # Feed-health check (gap_detected from stream means reconnect happened)
        if tick.gap_detected:
            self._emit_feed_health_event(instrument)

    def _evaluate_position(
        self,
        position: PositionSnapshot,
        current_price: float,
    ) -> None:
        """Evaluate all rules for one position at the current price."""
        recent = self._recent_prices.get(position.instrument, [])

        candidates = [
            check_adverse(position, current_price, self._config),
            check_slippage(position, self._config),
            check_vol(position, recent, self._config),
        ]

        for event in candidates:
            if event is not None:
                self._maybe_emit(event)

    def _check_all_feed_health(self) -> None:
        """Check feed health for all tracked instruments."""
        for instrument in self._instruments:
            last = self._last_tick_time.get(instrument, 0.0)
            event = check_feed_health(instrument, last, self._config)
            if event is not None:
                self._maybe_emit(event)

    def _emit_feed_health_event(self, instrument: str) -> None:
        """Emit a feed_health event for gap_detected ticks."""
        now = datetime.now(timezone.utc)
        window_ts = _debounce_window_ts(now, self._config.debounce_seconds)
        event = DeviationEvent(
            event_id=_event_id(None, "feed_health", window_ts, instrument=instrument),
            instrument=instrument,
            deviation_type="feed_health",
            detail="gap_detected: stream reconnected (data continuity broken)",
            broker_trade_id=None,
            severity="severe",
            created_at=now,
        )
        self._maybe_emit(event)

    def _maybe_emit(self, event: DeviationEvent) -> None:
        """Debounce and emit an event; trigger auto-response if configured.

        For feed_health events (broker_trade_id is None) the debounce key
        includes the instrument so that each instrument's feed-health stream
        is debounced independently.  Position-based rules (adverse, slippage,
        vol) continue to be keyed by broker_trade_id.
        """
        if event.broker_trade_id is None:
            # feed_health: one independent debounce slot per instrument
            key: tuple[str | None, str] = (event.instrument, event.deviation_type)
        else:
            key = (event.broker_trade_id, event.deviation_type)
        # Debounce: skip if the same event_id was already fired for this window
        if self._debounce.get(key) == event.event_id:
            logger.debug(
                "Watcher debounce: suppressed repeated %s event for trade=%s",
                event.deviation_type,
                event.broker_trade_id,
            )
            return

        self._debounce[key] = event.event_id
        logger.info(
            "Watcher emitting %s/%s event for %s trade=%s",
            event.deviation_type,
            event.severity,
            event.instrument,
            event.broker_trade_id,
        )
        self._alerter.send(event)

        # Auto-response (default-off, INV-01)
        if (
            event.severity == "severe"
            and self._config.severe_response != "alert_only"
            and event.broker_trade_id is not None
        ):
            action: Literal["flatten", "tighten_stop"] = (
                "flatten"
                if self._config.severe_response == "auto_flatten"
                else "tighten_stop"
            )
            logger.warning(
                "Watcher auto-response: %s for trade=%s instrument=%s",
                action,
                event.broker_trade_id,
                event.instrument,
            )
            self._responder.respond(
                action,
                broker_trade_id=event.broker_trade_id,
                instrument=event.instrument,
            )
