"""Tests for the Phase 2 signal-ranker (P2-T-01).

Everything is mocked — NO live HTTP, no real OANDA/calendar fetch.  Fakes:
- ``FakeStore`` — returns a canned approved-set + per-combo candle frames.
- ``FakeCalendar`` — returns canned ``CalendarEvent``-shaped objects per window.
- ``StubStrategy`` — emits a single canned ``Signal`` (via an injected builder).

Coverage maps to the AC:
- empty approved-set → ``rank()`` == ``[]`` (INV-10), logged.
- only approved (strategy, pair, tf) combos emit; gate join uses ``granularity``.
- high-impact news in-window → dropped; medium → ``news_flag=True``; low/none → False.
- spread/session fail → dropped.
- same-(instrument, tf) opposite-direction → both suppressed.
- rank order: ``oos_sharpe_mean`` primary, ``quality_score`` tie-break, deterministic.
- ``Candidate`` serialisation round-trip pins the INV-13 field shape.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd
import pytest

from data.calendar import CalendarEvent, Impact
from signals.ranker import (
    NEWS_WINDOW_HIGH,
    NEWS_WINDOW_MEDIUM,
    Candidate,
    Ranker,
    SessionCheck,
    SpreadCheck,
    _default_session_ok,
    _default_spread_ok,
)
from strategies.base import Direction, Signal, Strategy

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _row(
    strategy_name: str,
    instrument: str,
    granularity: str,
    oos_sharpe_mean: float,
) -> dict[str, object]:
    """An approved-set row in the shipped ``load_approved_set`` shape."""
    return {
        "run_timestamp": "2026-05-28T00:00:00Z",
        "strategy_name": strategy_name,
        "instrument": instrument,
        "granularity": granularity,
        "oos_sharpe_mean": oos_sharpe_mean,
        "oos_trade_count_total": 42,
        "swap_modelled": True,
    }


def _candles(n: int = 5) -> pd.DataFrame:
    """A minimal non-empty candle frame (content is irrelevant — strategy stubbed)."""
    # Last bar closes exactly at NOW; earlier bars step back hourly.
    times = pd.to_datetime(
        [NOW - timedelta(hours=n - 1 - i) for i in range(n)], utc=True
    )
    return pd.DataFrame(
        {
            "time": times,
            "open_bid": [1.0] * n,
            "high_bid": [1.0] * n,
            "low_bid": [1.0] * n,
            "close_bid": [1.0] * n,
            "open_ask": [1.0] * n,
            "high_ask": [1.0] * n,
            "low_ask": [1.0] * n,
            "close_ask": [1.0] * n,
            "volume": [100] * n,
        }
    )


class FakeStore:
    """Mock Store: canned approved-set + always-non-empty candle frames."""

    def __init__(self, approved: list[dict[str, object]], empty_candles: bool = False):
        self._approved = approved
        self._empty_candles = empty_candles

    def load_approved_set(
        self, run_timestamp: Optional[datetime] = None
    ) -> list[dict[str, object]]:
        return self._approved

    def load_candles(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        if self._empty_candles:
            return _candles(0).iloc[0:0]
        return _candles()


class FakeCalendar:
    """Mock calendar: maps each leg-currency to a fixed impact level.

    ``upcoming_events`` honours the window: an event is only returned if its
    impact's natural window covers the requested ``window`` (high events appear
    in any window; medium events only in the medium window or wider).
    """

    def __init__(self, by_currency: dict[str, Impact]):
        self._by_currency = by_currency

    def upcoming_events(
        self, currencies: list[str], window: timedelta
    ) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        for ccy in currencies:
            impact = self._by_currency.get(ccy)
            if impact is None:
                continue
            out.append(
                CalendarEvent(
                    currency=ccy,
                    event_name=f"{ccy} event",
                    time=NOW + timedelta(minutes=30),
                    impact=impact,
                )
            )
        return out


class StubStrategy(Strategy):
    """A strategy that always emits one canned Signal for the latest bar."""

    def __init__(
        self,
        *,
        strategy_name: str,
        instrument: str,
        timeframe: str,
        direction: Direction,
        quality_score: float,
    ):
        self._name = strategy_name
        self._instrument = instrument
        self._timeframe = timeframe
        self._direction = direction
        self._quality_score = quality_score

    @property
    def name(self) -> str:
        return self._name

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        bar_time = df["time"].iloc[-1].to_pydatetime()
        return [
            Signal(
                instrument=self._instrument,
                direction=self._direction,
                entry_ref=1.2345,
                stop_distance=0.0010,
                target_distance=0.0015,
                strategy_name=self._name,
                timeframe=self._timeframe,
                quality_score=self._quality_score,
                generated_at=bar_time,
            )
        ]


def make_builder(
    spec: dict[tuple[str, str, str], tuple[Direction, float]],
) -> Callable[[str, str, str], Strategy]:
    """Builder factory: maps (strategy_name, instrument, timeframe) → StubStrategy.

    ``spec`` value is ``(direction, quality_score)``.  A combo absent from the
    spec yields a FLAT signal (no candidate) — lets tests assert that only the
    intended combos emit.
    """

    def _builder(strategy_name: str, instrument: str, timeframe: str) -> Strategy:
        direction, quality = spec.get(
            (strategy_name, instrument, timeframe), (Direction.FLAT, 0.0)
        )
        return StubStrategy(
            strategy_name=strategy_name,
            instrument=instrument,
            timeframe=timeframe,
            direction=direction,
            quality_score=quality,
        )

    return _builder


def make_ranker(
    approved: list[dict[str, object]],
    builder_spec: dict[tuple[str, str, str], tuple[Direction, float]],
    *,
    calendar: Optional[FakeCalendar] = None,
    spread_ok: Optional[SpreadCheck] = None,
    session_ok: Optional[SessionCheck] = None,
    empty_candles: bool = False,
) -> Ranker:
    return Ranker(
        store=FakeStore(approved, empty_candles=empty_candles),
        calendar=calendar or FakeCalendar({}),  # type: ignore[arg-type]
        strategy_builder=make_builder(builder_spec),
        spread_ok=spread_ok if spread_ok is not None else _default_spread_ok,
        session_ok=session_ok if session_ok is not None else _default_session_ok,
    )


# ---------------------------------------------------------------------------
# INV-10 gate
# ---------------------------------------------------------------------------


def test_empty_approved_set_returns_empty_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ranker = make_ranker([], {})
    with caplog.at_level(logging.INFO, logger="signals.ranker"):
        result = ranker.rank(NOW)
    assert result == []
    assert any("Approved-set is empty" in r.message for r in caplog.records)


def test_rank_requires_utc_aware_now() -> None:
    ranker = make_ranker([], {})
    with pytest.raises(ValueError, match="UTC-aware"):
        ranker.rank(datetime(2026, 5, 28, 12, 0, 0))  # naive


# ---------------------------------------------------------------------------
# Only approved combos emit + gate join uses granularity
# ---------------------------------------------------------------------------


def test_only_approved_combos_emit() -> None:
    # Approved: macrossover EUR_USD H1. Builder is asked only for approved
    # combos; an un-approved combo is never built (so never emits).
    approved = [_row("macrossover_eur_usd_h1", "EUR_USD", "H1", 1.0)]
    spec = {("macrossover_eur_usd_h1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    ranker = make_ranker(approved, spec)
    result = ranker.rank(NOW)
    assert len(result) == 1
    assert result[0].instrument == "EUR_USD"
    assert result[0].timeframe == "H1"
    assert result[0].strategy_name == "macrossover_eur_usd_h1"


def test_gate_join_uses_granularity_dimension() -> None:
    """The DB row's ``granularity`` is matched to ``Signal.timeframe``.

    The approved row uses ``granularity='H4'``; the stub emits a signal whose
    ``timeframe`` equals the granularity it was built with.  The candidate's
    ``timeframe`` must surface that same dimension.
    """
    approved = [_row("donchian_x", "GBP_USD", "H4", 0.8)]
    spec = {("donchian_x", "GBP_USD", "H4"): (Direction.SHORT, 0.6)}
    ranker = make_ranker(approved, spec)
    result = ranker.rank(NOW)
    assert len(result) == 1
    # Candidate exposes the dimension as ``timeframe`` (== row's granularity).
    assert result[0].timeframe == "H4"
    assert result[0].direction == "SHORT"


def test_flat_signal_produces_no_candidate() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.FLAT, 0.5)}
    ranker = make_ranker(approved, spec)
    assert ranker.rank(NOW) == []


def test_empty_candles_skips_combo() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    ranker = make_ranker(approved, spec, empty_candles=True)
    assert ranker.rank(NOW) == []


# ---------------------------------------------------------------------------
# News gate
# ---------------------------------------------------------------------------


def test_high_impact_news_drops_candidate() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    cal = FakeCalendar({"USD": Impact.high})  # quote leg has high-impact event
    ranker = make_ranker(approved, spec, calendar=cal)
    assert ranker.rank(NOW) == []


def test_medium_impact_news_sets_flag_but_keeps() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    cal = FakeCalendar({"EUR": Impact.medium})
    ranker = make_ranker(approved, spec, calendar=cal)
    result = ranker.rank(NOW)
    assert len(result) == 1
    assert result[0].news_flag is True


def test_low_or_no_news_clears_flag() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    cal_low = FakeCalendar({"EUR": Impact.low})
    assert make_ranker(approved, spec, calendar=cal_low).rank(NOW)[0].news_flag is False
    cal_none = FakeCalendar({})
    assert make_ranker(approved, spec, calendar=cal_none).rank(NOW)[0].news_flag is False


def test_news_windows_are_the_documented_lengths() -> None:
    assert NEWS_WINDOW_HIGH == timedelta(hours=4)
    assert NEWS_WINDOW_MEDIUM == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Spread / session filters
# ---------------------------------------------------------------------------


def test_spread_fail_drops_candidate() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    ranker = make_ranker(approved, spec, spread_ok=lambda i, t, n: False)
    assert ranker.rank(NOW) == []


def test_session_fail_drops_candidate() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    ranker = make_ranker(approved, spec, session_ok=lambda i, t, n: False)
    assert ranker.rank(NOW) == []


def test_passing_filters_set_flags_true() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    c = make_ranker(approved, spec).rank(NOW)[0]
    assert c.spread_ok is True and c.session_ok is True


# ---------------------------------------------------------------------------
# Conflict (D-P2-1)
# ---------------------------------------------------------------------------


def test_opposite_direction_same_instrument_timeframe_suppresses_both() -> None:
    approved = [
        _row("long_strat", "EUR_USD", "H1", 1.0),
        _row("short_strat", "EUR_USD", "H1", 2.0),
    ]
    spec = {
        ("long_strat", "EUR_USD", "H1"): (Direction.LONG, 0.5),
        ("short_strat", "EUR_USD", "H1"): (Direction.SHORT, 0.9),
    }
    ranker = make_ranker(approved, spec)
    assert ranker.rank(NOW) == []


def test_same_direction_same_group_not_suppressed() -> None:
    approved = [
        _row("a", "EUR_USD", "H1", 1.0),
        _row("b", "EUR_USD", "H1", 2.0),
    ]
    spec = {
        ("a", "EUR_USD", "H1"): (Direction.LONG, 0.5),
        ("b", "EUR_USD", "H1"): (Direction.LONG, 0.9),
    }
    result = make_ranker(approved, spec).rank(NOW)
    assert len(result) == 2


def test_opposite_direction_different_timeframe_ranked_independently() -> None:
    approved = [
        _row("a", "EUR_USD", "H1", 1.0),
        _row("b", "EUR_USD", "H4", 2.0),
    ]
    spec = {
        ("a", "EUR_USD", "H1"): (Direction.LONG, 0.5),
        ("b", "EUR_USD", "H4"): (Direction.SHORT, 0.9),
    }
    result = make_ranker(approved, spec).rank(NOW)
    # Different timeframes — no conflict; both survive.
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Rank order
# ---------------------------------------------------------------------------


def test_rank_primary_by_oos_sharpe_descending() -> None:
    approved = [
        _row("low", "EUR_USD", "H1", 0.5),
        _row("high", "GBP_USD", "H1", 2.0),
        _row("mid", "AUD_USD", "H1", 1.0),
    ]
    spec = {
        ("low", "EUR_USD", "H1"): (Direction.LONG, 0.9),
        ("high", "GBP_USD", "H1"): (Direction.LONG, 0.1),
        ("mid", "AUD_USD", "H1"): (Direction.LONG, 0.5),
    }
    result = make_ranker(approved, spec).rank(NOW)
    assert [c.oos_sharpe_mean for c in result] == [2.0, 1.0, 0.5]
    assert [c.rank for c in result] == [1, 2, 3]


def test_quality_score_breaks_sharpe_ties() -> None:
    approved = [
        _row("a", "EUR_USD", "H1", 1.0),
        _row("b", "GBP_USD", "H1", 1.0),
    ]
    spec = {
        ("a", "EUR_USD", "H1"): (Direction.LONG, 0.3),
        ("b", "GBP_USD", "H1"): (Direction.LONG, 0.8),
    }
    result = make_ranker(approved, spec).rank(NOW)
    # Same sharpe → higher quality_score ranks first.
    assert result[0].instrument == "GBP_USD"
    assert result[0].quality_score == 0.8
    assert [c.rank for c in result] == [1, 2]


def test_final_tiebreak_is_stable_and_deterministic() -> None:
    # Identical sharpe AND quality → deterministic by (instrument, strategy_name).
    approved = [
        _row("z_strat", "GBP_USD", "H1", 1.0),
        _row("a_strat", "EUR_USD", "H1", 1.0),
    ]
    spec = {
        ("z_strat", "GBP_USD", "H1"): (Direction.LONG, 0.5),
        ("a_strat", "EUR_USD", "H1"): (Direction.LONG, 0.5),
    }
    result = make_ranker(approved, spec).rank(NOW)
    # EUR_USD sorts before GBP_USD.
    assert [c.instrument for c in result] == ["EUR_USD", "GBP_USD"]


# ---------------------------------------------------------------------------
# INV-13 serialisation round-trip (pins the contract)
# ---------------------------------------------------------------------------


EXPECTED_FIELDS = [
    "instrument",
    "timeframe",
    "strategy_name",
    "direction",
    "entry_ref",
    "stop_distance",
    "target_distance",
    "oos_sharpe_mean",
    "quality_score",
    "rank",
    "spread_ok",
    "session_ok",
    "news_flag",
    "generated_at",
]


def test_candidate_field_names_and_order_match_inv13() -> None:
    assert list(Candidate.model_fields.keys()) == EXPECTED_FIELDS


def test_candidate_field_types_match_inv13() -> None:
    ann = {name: f.annotation for name, f in Candidate.model_fields.items()}
    assert ann["instrument"] is str
    assert ann["timeframe"] is str
    assert ann["strategy_name"] is str
    assert ann["direction"] is str
    assert ann["entry_ref"] is float
    assert ann["stop_distance"] is float
    assert ann["target_distance"] is float
    assert ann["oos_sharpe_mean"] is float
    assert ann["quality_score"] is float
    assert ann["rank"] is int
    assert ann["spread_ok"] is bool
    assert ann["session_ok"] is bool
    assert ann["news_flag"] is bool
    assert ann["generated_at"] is str


def test_candidate_serialisation_round_trip() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.25)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.7)}
    candidate = make_ranker(approved, spec).rank(NOW)[0]

    payload = candidate.model_dump()
    assert set(payload.keys()) == set(EXPECTED_FIELDS)

    # Flat shape — no nested objects/dicts/lists leak through.
    for value in payload.values():
        assert not isinstance(value, (dict, list))

    # JSON round-trip is loss-free and reconstructs an equal Candidate.
    as_json = candidate.model_dump_json()
    restored = Candidate.model_validate(json.loads(as_json))
    assert restored == candidate

    # generated_at is a UTC RFC-3339 ...Z string (INV-03).
    assert payload["generated_at"].endswith("Z")
    assert payload["direction"] in ("LONG", "SHORT")


def test_generated_at_carries_signal_bar_close_time() -> None:
    approved = [_row("s1", "EUR_USD", "H1", 1.0)]
    spec = {("s1", "EUR_USD", "H1"): (Direction.LONG, 0.5)}
    c = make_ranker(approved, spec).rank(NOW)[0]
    # The stub uses the latest candle's time (NOW), formatted RFC-3339 Z.
    assert c.generated_at == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
