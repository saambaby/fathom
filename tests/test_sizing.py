"""Tests for ``risk.sizing`` — the INV-05 0.25%-of-equity per-trade cap.

The headline guarantee is property-tested hard (hypothesis): realized risk
(``|units| × per_unit_risk``) never exceeds ``equity × risk_fraction``.  Worked
hand-computed fixtures pin the conversion maths for a quote==account pair
(EUR_USD) and a non-account-quote pair (USD_JPY).
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from data.oanda_client import InstrumentMeta
from risk.sizing import DEFAULT_RISK_FRACTION, size_position
from signals.ranker import Candidate


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _candidate(
    *,
    instrument: str = "EUR_USD",
    direction: str = "LONG",
    stop_distance: float = 0.0050,
    entry_ref: float = 1.1000,
    target_distance: float = 0.0100,
) -> Candidate:
    """Build a minimal valid Candidate; only direction + stop_distance matter."""
    return Candidate(
        instrument=instrument,
        timeframe="H1",
        strategy_name="macrossover_10_50",
        direction=direction,
        entry_ref=entry_ref,
        stop_distance=stop_distance,
        target_distance=target_distance,
        oos_sharpe_mean=1.0,
        quality_score=0.5,
        rank=1,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at="2026-05-29T00:00:00Z",
    )


def _meta(
    *,
    name: str = "EUR_USD",
    pip_location: int = -4,
    min_trade_size: float = 1.0,
) -> InstrumentMeta:
    """Build an InstrumentMeta; only min_trade_size is consulted by sizing."""
    return InstrumentMeta(
        name=name,
        pip_location=pip_location,
        min_trade_size=min_trade_size,
        margin_rate=0.02,
        display_precision=5 if pip_location == -4 else 3,
        long_rate=0.0,
        short_rate=0.0,
        financing_days_of_week=[2],
    )


# ---------------------------------------------------------------------------
# Worked hand-computed fixtures (AC: EUR_USD quote=USD, USD_JPY quote=JPY)
# ---------------------------------------------------------------------------


def test_eur_usd_worked_example_hits_cap_exactly() -> None:
    """EUR_USD, USD account (rate=1).

    equity=100_000, risk_fraction=0.0025 → budget=250 USD.
    stop_distance=0.0050 (50 pips) → per_unit_risk=0.0050 USD/unit.
    units = floor(250 / 0.0050) = 50_000 ; risk = 50_000 × 0.0050 = 250.0 = cap.
    """
    result = size_position(
        _candidate(instrument="EUR_USD", direction="LONG", stop_distance=0.0050),
        equity=100_000.0,
        instrument_meta=_meta(name="EUR_USD", pip_location=-4),
        rate=1.0,
    )
    assert result.units == 50_000  # LONG → positive
    assert result.risk_amount == pytest.approx(250.0, abs=1e-9)
    assert result.risk_amount <= 100_000.0 * DEFAULT_RISK_FRACTION + 1e-9
    assert result.reason is None


def test_usd_jpy_worked_example_conversion() -> None:
    """USD_JPY, USD account — quote is JPY, so a conversion rate is required.

    equity=100_000 USD, budget=250 USD.
    stop_distance=0.50 (50 pips, JPY price units).
    USD_JPY mid = 150.0 → rate = 1/150 USD per JPY.
    per_unit_risk = 0.50 × (1/150) = 0.0033333... USD/unit.
    units = floor(250 / 0.0033333...) = floor(250 × 150 / 0.50) = 75_000.
    SHORT → units = -75_000 ; risk = 75_000 × 0.0033333... = 250.0 = cap.
    """
    rate = 1.0 / 150.0
    result = size_position(
        _candidate(instrument="USD_JPY", direction="SHORT", stop_distance=0.50),
        equity=100_000.0,
        instrument_meta=_meta(name="USD_JPY", pip_location=-2),
        rate=rate,
    )
    assert result.units == -75_000  # SHORT → negative
    assert result.risk_amount == pytest.approx(250.0, abs=1e-6)
    assert result.risk_amount <= 100_000.0 * DEFAULT_RISK_FRACTION + 1e-6
    assert result.reason is None


def test_floor_rounds_down_below_cap_when_not_exact() -> None:
    """A budget that does not divide evenly floors down — strictly under cap."""
    # budget = 250 ; per_unit_risk = 0.0050 × 1 → raw = 50000 exact; perturb stop
    # so the division is not integral: stop=0.0051 → raw=49019.6 → floor 49019.
    result = size_position(
        _candidate(stop_distance=0.0051),
        equity=100_000.0,
        instrument_meta=_meta(),
        rate=1.0,
    )
    assert result.units == 49_019
    assert result.risk_amount < 250.0  # strictly under the cap
    assert result.risk_amount == pytest.approx(49_019 * 0.0051, abs=1e-9)


# ---------------------------------------------------------------------------
# Rejection paths (AC: stop<=0, budget < min_trade_size, bad inputs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_stop", [0.0, -0.0010, -1.0])
def test_non_positive_stop_rejected(bad_stop: float) -> None:
    """stop_distance <= 0 → reject (never sized naked, INV-04/11)."""
    # Candidate's own validator forbids stop<=0, so construct then mutate the
    # field to exercise sizing's own guard directly.
    cand = _candidate()
    object.__setattr__(cand, "stop_distance", bad_stop)
    result = size_position(
        cand, equity=100_000.0, instrument_meta=_meta(), rate=1.0
    )
    assert result.units == 0
    assert result.risk_amount == 0.0
    assert result.reason is not None
    assert "stop_distance" in result.reason


def test_budget_too_small_for_min_trade_size_rejected() -> None:
    """A budget that funds fewer than min_trade_size units → reject, not round up."""
    # equity tiny → budget tiny; min_trade_size large.
    result = size_position(
        _candidate(stop_distance=0.0050),
        equity=10.0,  # budget = 10 × 0.0025 = 0.025 USD
        instrument_meta=_meta(min_trade_size=1.0),
        rate=1.0,
    )
    # raw = 0.025 / 0.0050 = 5 → floor 5; but lift min_trade_size to force reject:
    assert result.units == 5  # this funds 5 units; min=1 so it is allowed
    # Now a genuinely-too-small budget:
    result2 = size_position(
        _candidate(stop_distance=0.0050),
        equity=1.0,  # budget = 0.0025 → raw = 0.5 → floor 0
        instrument_meta=_meta(min_trade_size=1.0),
        rate=1.0,
    )
    assert result2.units == 0
    assert result2.risk_amount == 0.0
    assert result2.reason is not None
    assert "minimum" in result2.reason


def test_min_trade_size_floor_never_rounds_up() -> None:
    """When the cap funds 100 units but min is 500, reject (never round up)."""
    result = size_position(
        _candidate(stop_distance=0.0050),
        equity=200.0,  # budget = 0.5 USD → raw = 100 units
        instrument_meta=_meta(min_trade_size=500.0),
        rate=1.0,
    )
    assert result.units == 0
    assert result.reason is not None


@pytest.mark.parametrize(
    "equity,rate,rf",
    [
        (0.0, 1.0, 0.0025),
        (-100.0, 1.0, 0.0025),
        (float("nan"), 1.0, 0.0025),
        (float("inf"), 1.0, 0.0025),
        (100_000.0, 0.0, 0.0025),
        (100_000.0, -1.0, 0.0025),
        (100_000.0, float("nan"), 0.0025),
        (100_000.0, 1.0, 0.0),
        (100_000.0, 1.0, -0.001),
        (100_000.0, 1.0, float("nan")),
    ],
)
def test_bad_scalar_inputs_rejected(equity: float, rate: float, rf: float) -> None:
    """Non-finite / non-positive equity, rate, or risk_fraction → reject."""
    result = size_position(
        _candidate(),
        equity=equity,
        instrument_meta=_meta(),
        rate=rate,
        risk_fraction=rf,
    )
    assert result.units == 0
    assert result.risk_amount == 0.0
    assert result.reason is not None


# ---------------------------------------------------------------------------
# Sign convention (AC: signed units by direction)
# ---------------------------------------------------------------------------


def test_long_units_positive_short_units_negative() -> None:
    long_res = size_position(
        _candidate(direction="LONG"),
        equity=100_000.0,
        instrument_meta=_meta(),
        rate=1.0,
    )
    short_res = size_position(
        _candidate(direction="SHORT"),
        equity=100_000.0,
        instrument_meta=_meta(),
        rate=1.0,
    )
    assert long_res.units > 0
    assert short_res.units < 0
    assert abs(long_res.units) == abs(short_res.units)  # magnitude direction-free


def test_flat_direction_rejected() -> None:
    cand = _candidate()
    object.__setattr__(cand, "direction", "FLAT")
    result = size_position(
        cand, equity=100_000.0, instrument_meta=_meta(), rate=1.0
    )
    assert result.units == 0
    assert result.reason is not None


def test_default_risk_fraction_is_the_cap() -> None:
    """The default risk_fraction is exactly the INV-05 0.25% cap."""
    assert DEFAULT_RISK_FRACTION == 0.0025


# ---------------------------------------------------------------------------
# INV-05 PROPERTY: realized risk never exceeds equity × risk_fraction
# ---------------------------------------------------------------------------


@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
@given(
    equity=st.floats(
        min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False
    ),
    stop_distance=st.floats(
        min_value=1e-6, max_value=10.0, allow_nan=False, allow_infinity=False
    ),
    rate=st.floats(
        min_value=1e-6, max_value=1e4, allow_nan=False, allow_infinity=False
    ),
    risk_fraction=st.floats(
        min_value=1e-6, max_value=0.0025, allow_nan=False, allow_infinity=False
    ),
    direction=st.sampled_from(["LONG", "SHORT"]),
    min_trade_size=st.floats(min_value=1.0, max_value=1000.0),
)
def test_inv05_realized_risk_never_exceeds_cap(
    equity: float,
    stop_distance: float,
    rate: float,
    risk_fraction: float,
    direction: str,
    min_trade_size: float,
) -> None:
    """INV-05: |units| × per_unit_risk ≤ equity × risk_fraction, ALWAYS.

    This is the headline invariant.  Across random equity/stop/rate/fraction
    combinations, the realized money at risk if the stop is hit must never
    exceed the cap.  The floor-only sizing makes this an identity, not a
    statistical near-miss.
    """
    cand = _candidate(direction=direction, stop_distance=stop_distance)
    result = size_position(
        cand,
        equity=equity,
        instrument_meta=_meta(min_trade_size=min_trade_size),
        rate=rate,
        risk_fraction=risk_fraction,
    )

    cap = equity * risk_fraction
    per_unit_risk = stop_distance * rate

    if result.units == 0:
        # Rejected: nothing is risked.
        assert result.risk_amount == 0.0
        return

    # Realized risk recomputed independently from the returned units.
    realized = abs(result.units) * per_unit_risk
    # Allow a tiny relative tolerance for float multiplication noise only.
    assert realized <= cap * (1 + 1e-9) + 1e-9, (
        f"INV-05 breached: realized={realized} > cap={cap} "
        f"(units={result.units}, per_unit_risk={per_unit_risk})"
    )
    # The reported risk_amount agrees with the recomputed realized risk.
    assert result.risk_amount == pytest.approx(realized, rel=1e-9, abs=1e-12)
    # And the reported risk_amount is itself within the cap.
    assert result.risk_amount <= cap * (1 + 1e-9) + 1e-9


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    equity=st.floats(
        min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False
    ),
    stop_distance=st.floats(
        min_value=1e-6, max_value=10.0, allow_nan=False, allow_infinity=False
    ),
    rate=st.floats(
        min_value=1e-6, max_value=1e4, allow_nan=False, allow_infinity=False
    ),
)
def test_inv05_cap_cannot_be_exceeded_by_one_more_unit(
    equity: float, stop_distance: float, rate: float
) -> None:
    """The size is the LARGEST cap-respecting size: one more unit breaches it.

    Proves there is no silent under-sizing and no rounding-up: ``units`` is the
    floor of the exact ratio, so ``(|units|+1) × per_unit_risk`` would exceed the
    cap whenever a real position was opened.
    """
    cand = _candidate(direction="LONG", stop_distance=stop_distance)
    result = size_position(
        cand,
        equity=equity,
        instrument_meta=_meta(min_trade_size=1.0),
        rate=rate,
        risk_fraction=DEFAULT_RISK_FRACTION,
    )
    if result.units == 0:
        return
    cap = equity * DEFAULT_RISK_FRACTION
    per_unit_risk = stop_distance * rate
    one_more = (abs(result.units) + 1) * per_unit_risk
    assert one_more > cap - 1e-9
    # And units equals the mathematical floor of the exact ratio.
    assert abs(result.units) == math.floor(cap / per_unit_risk)
