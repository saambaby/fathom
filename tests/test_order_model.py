"""Tests for the frozen execution contract (INV-14) and ``build_bracket``.

Covers every Acceptance criterion of ``order-model-and-brackets``:

* SL + TP always present, both directions, no naked path (INV-04).
* Direction-correct bracket maths against worked examples.
* Precision rounding pinned for 5-dp (EUR_USD) and 3-dp (USD_JPY) fixtures.
* Signed units; zero rejected.
* UTC validators reject naive datetimes (INV-03).
* JSON round-trip pins the frozen shape (INV-14).
* ``client_order_id`` determinism (INV-15).
* Non-positive stop raises.
* hypothesis property: stop/target straddle entry on the correct sides
  after rounding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from execution.models import (
    EntryType,
    Fill,
    FillStatus,
    Order,
    Position,
    build_bracket,
)
from signals.ranker import Candidate
from strategies.base import Direction

UTC = timezone.utc
EXEC_DATE = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _candidate(
    *,
    instrument: str = "EUR_USD",
    timeframe: str = "H1",
    strategy_name: str = "macrossover_10_50_eur_usd_h1",
    direction: str = "LONG",
    entry_ref: float = 1.10000,
    stop_distance: float = 0.00200,
    target_distance: float = 0.00300,
    generated_at: str = "2026-05-29T11:00:00Z",
) -> Candidate:
    return Candidate(
        instrument=instrument,
        timeframe=timeframe,
        strategy_name=strategy_name,
        direction=direction,
        entry_ref=entry_ref,
        stop_distance=stop_distance,
        target_distance=target_distance,
        oos_sharpe_mean=1.2,
        quality_score=0.7,
        rank=1,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# AC: SL + TP always present, both directions (INV-04)
# ---------------------------------------------------------------------------


def test_long_bracket_has_stop_and_target() -> None:
    order = build_bracket(
        _candidate(direction="LONG"), 1000, execution_date=EXEC_DATE, precision=5
    )
    assert order.stop_loss_price > 0
    assert order.take_profit_price > 0
    assert order.entry_type is EntryType.MARKET


def test_short_bracket_has_stop_and_target() -> None:
    order = build_bracket(
        _candidate(direction="SHORT"), -1000, execution_date=EXEC_DATE, precision=5
    )
    assert order.stop_loss_price > 0
    assert order.take_profit_price > 0


def test_non_positive_stop_distance_raises() -> None:
    # Candidate validator forbids stop_distance <= 0, so build a candidate that
    # bypasses validation (model_construct) to prove build_bracket also guards.
    bad = _candidate().model_copy(update={"stop_distance": 0.0})
    with pytest.raises(ValueError, match="stop_distance must be > 0"):
        build_bracket(bad, 1000, execution_date=EXEC_DATE, precision=5)


def test_non_positive_target_distance_raises() -> None:
    bad = _candidate().model_copy(update={"target_distance": -0.001})
    with pytest.raises(ValueError, match="target_distance must be > 0"):
        build_bracket(bad, 1000, execution_date=EXEC_DATE, precision=5)


# ---------------------------------------------------------------------------
# AC: direction-correct bracket maths (worked examples)
# ---------------------------------------------------------------------------


def test_long_worked_example() -> None:
    c = _candidate(
        direction="LONG", entry_ref=1.10000, stop_distance=0.00200, target_distance=0.00300
    )
    order = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    assert order.stop_loss_price == pytest.approx(1.09800)
    assert order.take_profit_price == pytest.approx(1.10300)
    assert order.direction is Direction.LONG


def test_short_worked_example() -> None:
    c = _candidate(
        direction="SHORT", entry_ref=1.10000, stop_distance=0.00200, target_distance=0.00300
    )
    order = build_bracket(c, -1000, execution_date=EXEC_DATE, precision=5)
    assert order.stop_loss_price == pytest.approx(1.10200)
    assert order.take_profit_price == pytest.approx(1.09700)
    assert order.direction is Direction.SHORT


# ---------------------------------------------------------------------------
# AC: precision rounding — 5dp (EUR_USD) and 3dp (USD_JPY)
# ---------------------------------------------------------------------------


def test_precision_rounding_eur_usd_5dp() -> None:
    c = _candidate(
        instrument="EUR_USD",
        entry_ref=1.105555,
        stop_distance=0.001234,
        target_distance=0.002345,
    )
    order = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    assert order.stop_loss_price == round(1.105555 - 0.001234, 5)
    assert order.take_profit_price == round(1.105555 + 0.002345, 5)
    # Pinned literal values.
    assert order.stop_loss_price == 1.10432
    assert order.take_profit_price == 1.1079


def test_precision_rounding_usd_jpy_3dp() -> None:
    c = _candidate(
        instrument="USD_JPY",
        entry_ref=151.2378,
        stop_distance=0.2512,
        target_distance=0.3766,
        direction="SHORT",
    )
    order = build_bracket(c, -1000, execution_date=EXEC_DATE, precision=3)
    assert order.stop_loss_price == round(151.2378 + 0.2512, 3)
    assert order.take_profit_price == round(151.2378 - 0.3766, 3)
    assert order.stop_loss_price == 151.489
    assert order.take_profit_price == 150.861


# ---------------------------------------------------------------------------
# AC: signed units; zero rejected; sign must match direction
# ---------------------------------------------------------------------------


def test_zero_units_rejected_by_order_validator() -> None:
    with pytest.raises(ValidationError):
        Order(
            client_order_id="x" * 32,
            instrument="EUR_USD",
            direction=Direction.LONG,
            units=0,
            entry_type=EntryType.MARKET,
            stop_loss_price=1.0,
            take_profit_price=1.1,
            candidate_ref="EUR_USD:H1:macrossover",
            created_at=EXEC_DATE,
        )


def test_long_with_negative_units_rejected() -> None:
    with pytest.raises(ValueError, match="LONG candidate requires units > 0"):
        build_bracket(_candidate(direction="LONG"), -1000, execution_date=EXEC_DATE, precision=5)


def test_short_with_positive_units_rejected() -> None:
    with pytest.raises(ValueError, match="SHORT candidate requires units < 0"):
        build_bracket(_candidate(direction="SHORT"), 1000, execution_date=EXEC_DATE, precision=5)


def test_zero_units_to_build_bracket_rejected() -> None:
    with pytest.raises(ValueError):
        build_bracket(_candidate(direction="LONG"), 0, execution_date=EXEC_DATE, precision=5)


# ---------------------------------------------------------------------------
# AC: UTC validators reject naive datetimes (INV-03)
# ---------------------------------------------------------------------------


def test_order_naive_created_at_rejected() -> None:
    with pytest.raises(ValidationError):
        Order(
            client_order_id="x" * 32,
            instrument="EUR_USD",
            direction=Direction.LONG,
            units=1000,
            entry_type=EntryType.MARKET,
            stop_loss_price=1.0,
            take_profit_price=1.1,
            candidate_ref="EUR_USD:H1:macrossover",
            created_at=datetime(2026, 5, 29, 12, 0, 0),  # naive
        )


def test_fill_naive_filled_at_rejected() -> None:
    with pytest.raises(ValidationError):
        Fill(
            client_order_id="x" * 32,
            broker_trade_id="t1",
            fill_price=1.10,
            units_filled=1000,
            slippage=0.0001,
            filled_at=datetime(2026, 5, 29, 12, 0, 0),  # naive
            status=FillStatus.FILLED,
        )


def test_position_naive_opened_at_rejected() -> None:
    with pytest.raises(ValidationError):
        Position(
            broker_trade_id="t1",
            instrument="EUR_USD",
            units=1000,
            entry_price=1.10,
            stop_loss_price=1.098,
            take_profit_price=1.103,
            opened_at=datetime(2026, 5, 29, 12, 0, 0),  # naive
            unrealized_pl=0.0,
            candidate_ref="EUR_USD:H1:macrossover",
        )


def test_position_naive_closed_at_rejected() -> None:
    with pytest.raises(ValidationError):
        Position(
            broker_trade_id="t1",
            instrument="EUR_USD",
            units=1000,
            entry_price=1.10,
            stop_loss_price=1.098,
            take_profit_price=1.103,
            opened_at=EXEC_DATE,
            unrealized_pl=0.0,
            closed_at=datetime(2026, 5, 29, 14, 0, 0),  # naive
            candidate_ref="EUR_USD:H1:macrossover",
        )


def test_build_bracket_naive_execution_date_rejected() -> None:
    with pytest.raises(ValueError, match="execution_date must be UTC-aware"):
        build_bracket(
            _candidate(),
            1000,
            execution_date=datetime(2026, 5, 29, 12, 0, 0),
            precision=5,
        )


# ---------------------------------------------------------------------------
# AC: positive-price validators
# ---------------------------------------------------------------------------


def test_negative_stop_price_rejected() -> None:
    with pytest.raises(ValidationError):
        Order(
            client_order_id="x" * 32,
            instrument="EUR_USD",
            direction=Direction.LONG,
            units=1000,
            entry_type=EntryType.MARKET,
            stop_loss_price=-1.0,
            take_profit_price=1.1,
            candidate_ref="EUR_USD:H1:macrossover",
            created_at=EXEC_DATE,
        )


def test_empty_client_order_id_rejected() -> None:
    with pytest.raises(ValidationError):
        Order(
            client_order_id="",
            instrument="EUR_USD",
            direction=Direction.LONG,
            units=1000,
            entry_type=EntryType.MARKET,
            stop_loss_price=1.0,
            take_profit_price=1.1,
            candidate_ref="EUR_USD:H1:macrossover",
            created_at=EXEC_DATE,
        )


# ---------------------------------------------------------------------------
# AC: JSON round-trip pins the frozen shape (INV-14)
# ---------------------------------------------------------------------------


def test_order_json_roundtrip_pins_shape() -> None:
    order = build_bracket(_candidate(), 1000, execution_date=EXEC_DATE, precision=5)
    dumped = order.model_dump_json()
    restored = Order.model_validate_json(dumped)
    assert restored == order
    keys = set(order.model_dump().keys())
    assert keys == {
        "client_order_id",
        "instrument",
        "direction",
        "units",
        "entry_type",
        "stop_loss_price",
        "take_profit_price",
        "candidate_ref",
        "created_at",
    }


def test_fill_json_roundtrip_pins_shape() -> None:
    fill = Fill(
        client_order_id="x" * 32,
        broker_trade_id="t1",
        fill_price=1.10,
        units_filled=1000,
        slippage=0.00012,
        filled_at=EXEC_DATE,
        status=FillStatus.FILLED,
    )
    restored = Fill.model_validate_json(fill.model_dump_json())
    assert restored == fill
    assert set(fill.model_dump().keys()) == {
        "client_order_id",
        "broker_trade_id",
        "fill_price",
        "units_filled",
        "slippage",
        "filled_at",
        "status",
    }


def test_position_json_roundtrip_pins_shape() -> None:
    pos = Position(
        broker_trade_id="t1",
        instrument="EUR_USD",
        units=1000,
        entry_price=1.10,
        stop_loss_price=1.098,
        take_profit_price=1.103,
        opened_at=EXEC_DATE,
        unrealized_pl=0.0,
        closed_at=None,
        realized_pl=None,
        candidate_ref="EUR_USD:H1:macrossover",
    )
    restored = Position.model_validate_json(pos.model_dump_json())
    assert restored == pos
    assert set(pos.model_dump().keys()) == {
        "broker_trade_id",
        "instrument",
        "units",
        "entry_price",
        "stop_loss_price",
        "take_profit_price",
        "opened_at",
        "unrealized_pl",
        "closed_at",
        "realized_pl",
        "candidate_ref",
    }


def test_candidate_ref_format() -> None:
    order = build_bracket(
        _candidate(instrument="EUR_USD", timeframe="H1", strategy_name="donchian_20"),
        1000,
        execution_date=EXEC_DATE,
        precision=5,
    )
    assert order.candidate_ref == "EUR_USD:H1:donchian_20"


# ---------------------------------------------------------------------------
# AC: client_order_id determinism (INV-15)
# ---------------------------------------------------------------------------


def test_client_order_id_deterministic() -> None:
    c = _candidate()
    a = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    b = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    assert a.client_order_id == b.client_order_id
    assert len(a.client_order_id) == 32
    assert a.client_order_id != ""


def test_client_order_id_changes_with_execution_date() -> None:
    c = _candidate()
    a = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    b = build_bracket(c, 1000, execution_date=EXEC_DATE + timedelta(days=1), precision=5)
    assert a.client_order_id != b.client_order_id


def test_client_order_id_changes_with_candidate_identity() -> None:
    a = build_bracket(_candidate(instrument="EUR_USD"), 1000, execution_date=EXEC_DATE, precision=5)
    b = build_bracket(_candidate(instrument="GBP_USD"), 1000, execution_date=EXEC_DATE, precision=5)
    assert a.client_order_id != b.client_order_id


def test_client_order_id_matches_formula() -> None:
    import hashlib

    c = _candidate()
    payload = (
        f"{c.instrument}:{c.strategy_name}:{c.timeframe}:"
        f"{c.generated_at}:{EXEC_DATE}"
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    order = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    assert order.client_order_id == expected


# ---------------------------------------------------------------------------
# build_bracket does not mutate the input Candidate (INV-13 read-only)
# ---------------------------------------------------------------------------


def test_build_bracket_does_not_mutate_candidate() -> None:
    c = _candidate()
    before = c.model_dump()
    build_bracket(c, 1000, execution_date=EXEC_DATE, precision=5)
    assert c.model_dump() == before


# ---------------------------------------------------------------------------
# Property: stop/target straddle entry on the correct sides after rounding
# ---------------------------------------------------------------------------

_PRECISION = st.integers(min_value=2, max_value=5)


@st.composite
def _entry_and_distances(draw: st.DrawFn) -> tuple[float, float, float]:
    """Entry + (stop, target) distances that keep both bracket prices positive.

    Distances stay comfortably above the coarsest rounding granularity (1e-2)
    and strictly below entry, so the property exercises *straddling*, not the
    separately-tested price-positivity validator (a SHORT target or a LONG stop
    at/below zero is impossible and is rejected by the model — see
    ``test_negative_stop_price_rejected``).
    """
    entry = draw(st.floats(min_value=1.0, max_value=200.0, allow_nan=False, allow_infinity=False))
    # Both distances < entry so LONG stop (entry−stop) and SHORT target
    # (entry−target) remain strictly positive.
    upper = min(entry * 0.5, 5.0)
    stop_d = draw(st.floats(min_value=0.05, max_value=upper, allow_nan=False, allow_infinity=False))
    target_d = draw(st.floats(min_value=0.05, max_value=upper, allow_nan=False, allow_infinity=False))
    return entry, stop_d, target_d


@given(ed=_entry_and_distances(), precision=_PRECISION)
def test_property_long_brackets_straddle_entry(
    ed: tuple[float, float, float], precision: int
) -> None:
    entry, stop_d, target_d = ed
    c = _candidate(direction="LONG", entry_ref=entry, stop_distance=stop_d, target_distance=target_d)
    order = build_bracket(c, 1000, execution_date=EXEC_DATE, precision=precision)
    entry_rounded = round(entry, precision)
    assert order.stop_loss_price < entry_rounded < order.take_profit_price


@given(ed=_entry_and_distances(), precision=_PRECISION)
def test_property_short_brackets_straddle_entry(
    ed: tuple[float, float, float], precision: int
) -> None:
    entry, stop_d, target_d = ed
    c = _candidate(direction="SHORT", entry_ref=entry, stop_distance=stop_d, target_distance=target_d)
    order = build_bracket(c, -1000, execution_date=EXEC_DATE, precision=precision)
    entry_rounded = round(entry, precision)
    assert order.take_profit_price < entry_rounded < order.stop_loss_price


@given(
    instrument=st.sampled_from(["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD"]),
    timeframe=st.sampled_from(["H1", "H4", "D"]),
    strategy=st.sampled_from(["macrossover_10_50", "donchian_20", "rsi_14"]),
)
def test_property_client_order_id_is_32_hex(
    instrument: str, timeframe: str, strategy: str
) -> None:
    direction = "SHORT" if instrument == "USD_JPY" else "LONG"
    units = -1000 if direction == "SHORT" else 1000
    entry = 150.0 if instrument == "USD_JPY" else 1.1
    precision = 3 if instrument == "USD_JPY" else 5
    stop_d = 0.5 if instrument == "USD_JPY" else 0.002
    target_d = 0.75 if instrument == "USD_JPY" else 0.003
    c = _candidate(
        instrument=instrument,
        timeframe=timeframe,
        strategy_name=strategy,
        direction=direction,
        entry_ref=entry,
        stop_distance=stop_d,
        target_distance=target_d,
    )
    order = build_bracket(c, units, execution_date=EXEC_DATE, precision=precision)
    assert len(order.client_order_id) == 32
    assert all(ch in "0123456789abcdef" for ch in order.client_order_id)
