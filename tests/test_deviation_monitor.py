"""Tests for monitoring/watcher.py — deviation monitor (P3-T-08).

Coverage (per ACs):
1. Adverse excursion past the configured fraction → exactly one DeviationEvent
   (debounced; not re-fired every tick).
2. Fill slippage exceeds threshold → slippage DeviationEvent.
3. Volatility spike (range expansion past threshold) → vol DeviationEvent.
4. No tick within heartbeat window → feed_health DeviationEvent (stale feed).
5. Default config is alert_only: no position is flattened/modified unless the
   auto-response flag is explicitly enabled.
6. Feed-health resilience: a stream drop (gap_detected=True) triggers a
   feed-health event, not a crash.
7. Events carry UTC timestamps (INV-03).
8. The loop never opens a position (INV-01).
9. Debounce: same rule does not re-fire for the same debounce window.
10. Auto-response: responder is called when severe_response != alert_only AND
    severity == severe; responder is NOT called for alert_only.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal
from unittest.mock import MagicMock

import pytest

from data.stream import PriceTick
from monitoring.watcher import (
    Alerter,
    DeviationEvent,
    ExecutionResponder,
    NoOpAlerter,
    NoOpExecutionResponder,
    PositionSnapshot,
    Watcher,
    WatcherConfig,
    check_adverse,
    check_feed_health,
    check_slippage,
    check_vol,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=_UTC)


def _pos(
    *,
    broker_trade_id: str = "T1",
    instrument: str = "EUR_USD",
    units: int = 10_000,
    entry_price: float = 1.10000,
    stop_loss_price: float = 1.09500,   # stop dist = 0.00500
    take_profit_price: float = 1.10750,
    fill_slippage: float = 0.0,
) -> PositionSnapshot:
    return PositionSnapshot(
        broker_trade_id=broker_trade_id,
        instrument=instrument,
        units=units,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        fill_slippage=fill_slippage,
    )


def _tick(
    instrument: str = "EUR_USD",
    bid: float = 1.10000,
    ask: float = 1.10010,
    gap_detected: bool = False,
    time_utc: datetime | None = None,
) -> PriceTick:
    return PriceTick(
        instrument=instrument,
        time=time_utc or _NOW,
        bid=bid,
        ask=ask,
        status="tradeable",
        gap_detected=gap_detected,
    )


def _cfg(**overrides: object) -> WatcherConfig:
    defaults: dict[str, object] = {
        "adverse_fraction": 0.5,
        "slippage_threshold": 0.0002,
        "vol_atr_multiplier": 2.0,
        "vol_lookback": 5,
        "heartbeat_timeout_seconds": 15.0,
        "reconcile_interval_seconds": 60.0,
        "debounce_seconds": 300.0,
        "severe_response": "alert_only",
    }
    defaults.update(overrides)
    return WatcherConfig(**defaults)


def _watcher(
    ticks: list[PriceTick],
    positions: list[PositionSnapshot] | None = None,
    config: WatcherConfig | None = None,
    alerter: Alerter | None = None,
    responder: ExecutionResponder | None = None,
) -> tuple[Watcher, list[DeviationEvent]]:
    """Build a watcher over a fixed tick list and collect emitted events."""
    events: list[DeviationEvent] = []

    class _Collector:
        def send(self, event: DeviationEvent) -> None:
            events.append(event)

    watcher = Watcher(
        tick_iterable=ticks,
        store_loader=lambda: positions or [],
        alerter=_Collector(),
        config=config or _cfg(),
        execution_responder=responder or NoOpExecutionResponder(),
        instruments=["EUR_USD"],
    )
    watcher.run()
    return watcher, events


# ---------------------------------------------------------------------------
# DeviationEvent model — UTC and field contracts (INV-03)
# ---------------------------------------------------------------------------


def test_deviation_event_utc_required() -> None:
    """created_at must be UTC-aware (INV-03)."""
    with pytest.raises(Exception):
        DeviationEvent(
            event_id="abc",
            instrument="EUR_USD",
            deviation_type="adverse",
            detail="test",
            severity="warn",
            created_at=datetime(2026, 5, 29, 12, 0, 0),  # naive — should fail
        )


def test_deviation_event_utc_aware_accepted() -> None:
    """A UTC-aware created_at is accepted."""
    ev = DeviationEvent(
        event_id="abc123",
        instrument="EUR_USD",
        deviation_type="adverse",
        detail="test detail",
        severity="warn",
        created_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=_UTC),
    )
    assert ev.created_at.tzinfo is not None


def test_deviation_event_feed_health_no_trade_id() -> None:
    """Feed-health events have broker_trade_id=None."""
    ev = DeviationEvent(
        event_id="fh01",
        instrument="EUR_USD",
        deviation_type="feed_health",
        detail="stale feed",
        broker_trade_id=None,
        severity="severe",
        created_at=datetime.now(_UTC),
    )
    assert ev.broker_trade_id is None
    assert ev.deviation_type == "feed_health"


def test_deviation_event_non_empty_strings() -> None:
    """event_id, instrument, detail must be non-empty."""
    with pytest.raises(Exception):
        DeviationEvent(
            event_id="",
            instrument="EUR_USD",
            deviation_type="adverse",
            detail="detail",
            severity="warn",
            created_at=datetime.now(_UTC),
        )


# ---------------------------------------------------------------------------
# Pure rule: check_adverse
# ---------------------------------------------------------------------------


def test_check_adverse_long_below_threshold() -> None:
    """Long position — price not yet adversely past threshold → None."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # threshold = 0.5 * 0.005 = 0.00250; price only 0.001 below entry
    result = check_adverse(pos, 1.09900, _cfg())
    assert result is None


def test_check_adverse_long_fires_warn() -> None:
    """Long position — price adversely past threshold → warn event."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # threshold = 0.5 * 0.005 = 0.00250; price 0.003 below entry → warn
    result = check_adverse(pos, 1.09700, _cfg())
    assert result is not None
    assert result.deviation_type == "adverse"
    assert result.severity == "warn"
    assert result.broker_trade_id == "T1"
    assert result.instrument == "EUR_USD"
    assert result.created_at.tzinfo is not None  # INV-03


def test_check_adverse_long_fires_severe_at_stop() -> None:
    """Long position — price at or past stop → severe event."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)
    # stop_dist=0.00500; excursion=0.00510 >= stop_dist → severe
    result = check_adverse(pos, 1.09490, _cfg())
    assert result is not None
    assert result.severity == "severe"


def test_check_adverse_short_fires() -> None:
    """Short position — price moves adversely upward → event fires."""
    pos = _pos(units=-10_000, entry_price=1.10000, stop_loss_price=1.10500)
    # stop_dist=0.00500; threshold=0.00250; price 0.003 above entry → warn
    result = check_adverse(pos, 1.10300, _cfg())
    assert result is not None
    assert result.deviation_type == "adverse"


def test_check_adverse_short_below_threshold() -> None:
    """Short position — favourable price move → None."""
    pos = _pos(units=-10_000, entry_price=1.10000, stop_loss_price=1.10500)
    result = check_adverse(pos, 1.09800, _cfg())
    assert result is None


def test_check_adverse_degenerate_stop() -> None:
    """Zero stop distance → None (degenerate position)."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.10000)
    result = check_adverse(pos, 1.09000, _cfg())
    assert result is None


# ---------------------------------------------------------------------------
# Pure rule: check_slippage
# ---------------------------------------------------------------------------


def test_check_slippage_below_threshold() -> None:
    pos = _pos(fill_slippage=0.0001)
    result = check_slippage(pos, _cfg(slippage_threshold=0.0002))
    assert result is None


def test_check_slippage_fires_warn() -> None:
    pos = _pos(fill_slippage=0.0003)
    result = check_slippage(pos, _cfg(slippage_threshold=0.0002))
    assert result is not None
    assert result.deviation_type == "slippage"
    assert result.severity == "warn"
    assert result.broker_trade_id == "T1"
    assert result.created_at.tzinfo is not None  # INV-03


def test_check_slippage_fires_severe() -> None:
    """Slippage >= 3x threshold → severe."""
    pos = _pos(fill_slippage=0.0007)  # 0.0007 >= 3 * 0.0002 = 0.0006
    result = check_slippage(pos, _cfg(slippage_threshold=0.0002))
    assert result is not None
    assert result.severity == "severe"


def test_check_slippage_at_threshold_not_fired() -> None:
    """Slippage exactly at threshold (<=) → no event."""
    pos = _pos(fill_slippage=0.0002)
    result = check_slippage(pos, _cfg(slippage_threshold=0.0002))
    assert result is None


# ---------------------------------------------------------------------------
# Pure rule: check_vol
# ---------------------------------------------------------------------------


def test_check_vol_insufficient_data() -> None:
    """Fewer than 2 prices → None."""
    pos = _pos()
    result = check_vol(pos, [1.10000], _cfg())
    assert result is None


def test_check_vol_below_threshold() -> None:
    """Small range — below vol threshold → None."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # range = 0.00400; threshold = 2.0 * 0.00500 = 0.01000; 0.004 < 0.01 → no fire
    prices = [1.09800, 1.09900, 1.10000, 1.10100, 1.10200]
    result = check_vol(pos, prices, _cfg())
    assert result is None


def test_check_vol_fires_warn() -> None:
    """Range exceeds threshold → vol event (warn)."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # range = 0.01100; threshold = 2.0 * 0.00500 = 0.01000; fires warn
    prices = [1.09000, 1.09100, 1.09200, 1.09300, 1.10100]
    result = check_vol(pos, prices, _cfg())
    assert result is not None
    assert result.deviation_type == "vol"
    assert result.severity == "warn"
    assert result.created_at.tzinfo is not None  # INV-03


def test_check_vol_fires_severe() -> None:
    """Range >= 2x threshold → severe."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # range = 0.02100; threshold = 2.0 * 0.005 = 0.010; 2x threshold = 0.020;
    # 0.021 >= 0.020 → severe
    prices = [1.09000, 1.09100, 1.09200, 1.09300, 1.11100]
    result = check_vol(pos, prices, _cfg())
    assert result is not None
    assert result.severity == "severe"


def test_check_vol_degenerate_stop() -> None:
    """Zero stop distance → None."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.10000)
    prices = [1.09000, 1.11000]
    result = check_vol(pos, prices, _cfg())
    assert result is None


# ---------------------------------------------------------------------------
# Pure rule: check_feed_health
# ---------------------------------------------------------------------------


def test_check_feed_health_no_timeout() -> None:
    """Recent tick → None."""
    result = check_feed_health("EUR_USD", time.monotonic(), _cfg(heartbeat_timeout_seconds=15.0))
    assert result is None


def test_check_feed_health_fires() -> None:
    """Stale feed (last tick was long ago) → feed_health event."""
    stale = time.monotonic() - 20.0  # 20 seconds ago
    result = check_feed_health("EUR_USD", stale, _cfg(heartbeat_timeout_seconds=15.0))
    assert result is not None
    assert result.deviation_type == "feed_health"
    assert result.broker_trade_id is None  # no trade context for feed events
    assert result.severity == "severe"
    assert result.created_at.tzinfo is not None  # INV-03


# ---------------------------------------------------------------------------
# Watcher integration: adverse rule fires exactly once (debounced) — AC#1
# ---------------------------------------------------------------------------


def test_watcher_adverse_fires_once_debounced() -> None:
    """AC#1: adverse excursion fires exactly one DeviationEvent even with many ticks."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # Adverse price: 0.003 below entry, threshold=0.0025 → fires every tick without debounce
    adverse_price = 1.09700
    ticks = [_tick(bid=adverse_price - 0.0001, ask=adverse_price) for _ in range(10)]

    _, events = _watcher(ticks, positions=[pos])

    adverse_events = [e for e in events if e.deviation_type == "adverse"]
    # Must fire exactly once (debounced — same event_id suppresses repeats)
    assert len(adverse_events) == 1, (
        f"Expected exactly 1 adverse event, got {len(adverse_events)}"
    )
    assert adverse_events[0].broker_trade_id == "T1"
    assert adverse_events[0].created_at.tzinfo is not None  # INV-03


# ---------------------------------------------------------------------------
# Watcher integration: slippage rule — AC#2
# ---------------------------------------------------------------------------


def test_watcher_slippage_fires() -> None:
    """AC#2: fill slippage exceeds threshold → slippage DeviationEvent."""
    pos = _pos(fill_slippage=0.0005)  # exceeds 0.0002 threshold
    ticks = [_tick(bid=1.10000, ask=1.10010)]

    _, events = _watcher(ticks, positions=[pos])

    slippage_events = [e for e in events if e.deviation_type == "slippage"]
    assert len(slippage_events) >= 1
    assert slippage_events[0].deviation_type == "slippage"
    assert slippage_events[0].created_at.tzinfo is not None  # INV-03


# ---------------------------------------------------------------------------
# Watcher integration: vol rule — AC#3
# ---------------------------------------------------------------------------


def test_watcher_vol_fires() -> None:
    """AC#3: volatility spike (range expansion) → vol DeviationEvent."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)  # stop_dist=0.00500
    # Build ticks with large range to trigger vol: need 5 ticks (vol_lookback=5)
    # range will be 0.012 > 2.0 * 0.005 = 0.010 → fires
    prices = [
        (1.08950, 1.08960),
        (1.09200, 1.09210),
        (1.09500, 1.09510),
        (1.09800, 1.09810),
        (1.10150, 1.10160),  # range = 0.01210 → fires
    ]
    ticks = [_tick(bid=b, ask=a) for b, a in prices]

    _, events = _watcher(ticks, positions=[pos], config=_cfg(vol_lookback=5))

    vol_events = [e for e in events if e.deviation_type == "vol"]
    assert len(vol_events) >= 1
    assert vol_events[0].deviation_type == "vol"
    assert vol_events[0].created_at.tzinfo is not None  # INV-03


# ---------------------------------------------------------------------------
# Watcher integration: feed-health via gap_detected — AC#4, AC#6
# ---------------------------------------------------------------------------


def test_watcher_feed_health_on_gap_detected() -> None:
    """AC#4 / AC#6: gap_detected=True on a tick → feed_health event (not a crash)."""
    pos = _pos()
    # Normal tick then a gap-detected tick (stream reconnected)
    ticks = [
        _tick(bid=1.10000, ask=1.10010, gap_detected=False),
        _tick(bid=1.10000, ask=1.10010, gap_detected=True),
    ]
    _, events = _watcher(ticks, positions=[pos])

    fh_events = [e for e in events if e.deviation_type == "feed_health"]
    assert len(fh_events) >= 1
    assert fh_events[0].broker_trade_id is None  # no trade context
    assert fh_events[0].severity == "severe"
    assert fh_events[0].created_at.tzinfo is not None  # INV-03


# ---------------------------------------------------------------------------
# Watcher integration: default config is alert_only — AC#5
# ---------------------------------------------------------------------------


def test_watcher_default_alert_only_no_execution_call() -> None:
    """AC#5: default config is alert_only — responder.respond() never called."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)
    # Trigger a severe adverse event (price below stop)
    ticks = [_tick(bid=1.09490, ask=1.09500)]

    mock_responder = MagicMock()
    _, events = _watcher(ticks, positions=[pos], responder=mock_responder)

    adverse = [e for e in events if e.deviation_type == "adverse"]
    assert len(adverse) >= 1
    assert adverse[0].severity == "severe"
    # With default alert_only, the responder must NOT be called
    mock_responder.respond.assert_not_called()


def test_watcher_auto_flatten_calls_responder_on_severe() -> None:
    """auto_flatten: responder.respond() IS called when severity==severe."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)
    # Trigger severe adverse (past stop)
    ticks = [_tick(bid=1.09490, ask=1.09500)]

    mock_responder = MagicMock()
    cfg = _cfg(severe_response="auto_flatten")
    _, events = _watcher(ticks, positions=[pos], config=cfg, responder=mock_responder)

    adverse = [e for e in events if e.deviation_type == "adverse" and e.severity == "severe"]
    assert len(adverse) >= 1
    # Responder must be called with flatten
    mock_responder.respond.assert_called_once_with(
        "flatten",
        broker_trade_id="T1",
        instrument="EUR_USD",
    )


def test_watcher_tighten_stop_calls_responder() -> None:
    """tighten_stop: responder.respond() called with tighten_stop action."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)
    ticks = [_tick(bid=1.09490, ask=1.09500)]

    mock_responder = MagicMock()
    cfg = _cfg(severe_response="tighten_stop")
    _, events = _watcher(ticks, positions=[pos], config=cfg, responder=mock_responder)

    severe_events = [e for e in events if e.severity == "severe"]
    assert len(severe_events) >= 1
    mock_responder.respond.assert_called_once_with(
        "tighten_stop",
        broker_trade_id="T1",
        instrument="EUR_USD",
    )


# ---------------------------------------------------------------------------
# Watcher integration: debounce — same rule fires only once per window
# ---------------------------------------------------------------------------


def test_watcher_debounce_suppresses_repeat() -> None:
    """Same rule fires at most once per debounce window across many ticks."""
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)
    # All ticks adverse — should produce exactly 1 adverse event (debounced)
    price = 1.09700  # 0.003 below entry, threshold=0.0025 → fires
    ticks = [_tick(bid=price - 0.0001, ask=price) for _ in range(20)]

    _, events = _watcher(ticks, positions=[pos])

    adverse = [e for e in events if e.deviation_type == "adverse"]
    assert len(adverse) == 1, f"Expected 1 debounced event, got {len(adverse)}"


# ---------------------------------------------------------------------------
# Watcher integration: UTC timestamps (INV-03) — AC#7
# ---------------------------------------------------------------------------


def test_all_events_have_utc_timestamps() -> None:
    """AC#7: every emitted event has a UTC-aware timestamp (INV-03)."""
    pos = _pos(
        entry_price=1.10000,
        stop_loss_price=1.09500,
        fill_slippage=0.0005,
    )
    ticks = [
        _tick(bid=1.09700, ask=1.09710),  # adverse
        _tick(bid=1.09700, ask=1.09710, gap_detected=True),  # feed_health
    ]
    _, events = _watcher(ticks, positions=[pos])

    assert len(events) >= 1
    for ev in events:
        assert ev.created_at.tzinfo is not None, (
            f"Event {ev.deviation_type} has naive created_at (INV-03 violation)"
        )


# ---------------------------------------------------------------------------
# Watcher integration: never opens a position (INV-01) — AC#8
# ---------------------------------------------------------------------------


def test_watcher_never_opens_position() -> None:
    """AC#8 / INV-01: the watcher never calls a position-opening function.

    We verify this by:
    1. Confirming the responder's only allowed call targets are
       'flatten' and 'tighten_stop' — never an order-creation action.
    2. Verifying the watcher has no ``submit_order`` method (it is not the
       execution layer — it only delegates to the injected responder).
    """
    pos = _pos(entry_price=1.10000, stop_loss_price=1.09500)
    ticks = [_tick(bid=1.09490, ask=1.09500)]  # severe adverse

    mock_responder = MagicMock()
    cfg = _cfg(severe_response="auto_flatten")
    watcher, events = _watcher(ticks, positions=[pos], config=cfg, responder=mock_responder)

    for call in mock_responder.respond.call_args_list:
        action = call.args[0] if call.args else call.kwargs.get("action")
        assert action in ("flatten", "tighten_stop"), (
            f"Watcher called responder with disallowed action: {action} (INV-01)"
        )
    # The Watcher class itself must not expose a submit_order method (INV-01)
    assert not hasattr(watcher, "submit_order"), (
        "Watcher must not have a submit_order method (INV-01)"
    )
    assert not hasattr(watcher, "open_position"), (
        "Watcher must not have an open_position method (INV-01)"
    )


# ---------------------------------------------------------------------------
# Watcher integration: multiple positions on different instruments
# ---------------------------------------------------------------------------


def test_watcher_only_evaluates_matching_instrument() -> None:
    """Rules are only evaluated for positions whose instrument matches the tick."""
    pos_eur = _pos(broker_trade_id="T1", instrument="EUR_USD", entry_price=1.10000, stop_loss_price=1.09500)
    pos_gbp = _pos(broker_trade_id="T2", instrument="GBP_USD", entry_price=1.27000, stop_loss_price=1.26500)

    # Only EUR_USD ticks; price is adverse for EUR_USD position
    ticks = [_tick(instrument="EUR_USD", bid=1.09700, ask=1.09710)]

    _, events = _watcher(ticks, positions=[pos_eur, pos_gbp])

    trade_ids = {e.broker_trade_id for e in events if e.deviation_type == "adverse"}
    # Only T1 (EUR_USD) should fire; T2 (GBP_USD) should not
    assert "T1" in trade_ids or len(trade_ids) == 0  # T1 may fire
    assert "T2" not in trade_ids  # T2 must never fire from EUR_USD ticks


# ---------------------------------------------------------------------------
# Watcher integration: no tick source resilience
# ---------------------------------------------------------------------------


def test_watcher_empty_tick_source_no_crash() -> None:
    """Watcher with empty tick iterable exits cleanly (no crash)."""
    pos = _pos()
    _, events = _watcher([], positions=[pos])
    # No events expected (no ticks to evaluate), but no exception either
    assert isinstance(events, list)


# ---------------------------------------------------------------------------
# WatcherConfig validation
# ---------------------------------------------------------------------------


def test_watcher_config_defaults_are_alert_only() -> None:
    """Default WatcherConfig has severe_response='alert_only' (demo safety)."""
    cfg = WatcherConfig()
    assert cfg.severe_response == "alert_only"


def test_watcher_config_negative_threshold_rejected() -> None:
    """Non-positive threshold is rejected by validator."""
    with pytest.raises(Exception):
        WatcherConfig(adverse_fraction=-0.1)


# ---------------------------------------------------------------------------
# NoOpAlerter / NoOpExecutionResponder — smoke tests
# ---------------------------------------------------------------------------


def test_noop_alerter_does_not_crash() -> None:
    alerter = NoOpAlerter()
    ev = DeviationEvent(
        event_id="test",
        instrument="EUR_USD",
        deviation_type="adverse",
        detail="test",
        severity="info",
        created_at=datetime.now(_UTC),
    )
    alerter.send(ev)  # must not raise


def test_noop_execution_responder_does_not_crash() -> None:
    responder = NoOpExecutionResponder()
    responder.respond("flatten", broker_trade_id="T1", instrument="EUR_USD")


# ---------------------------------------------------------------------------
# PositionSnapshot validation
# ---------------------------------------------------------------------------


def test_position_snapshot_construction() -> None:
    pos = _pos()
    assert pos.broker_trade_id == "T1"
    assert pos.instrument == "EUR_USD"
    assert pos.units == 10_000
