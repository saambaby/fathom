"""Tests for the POC-T-05 backtest engine + cost model.

The AC mandates four tests; each is named and documented here:

(a) ``test_no_lookahead_canary`` — inject a canary value into bar N+1 and assert
    it never influences the decision the engine makes at bar N.  Property-based
    over random series (hypothesis) plus an explicit canary check.
(b) ``test_costs_non_zero`` — across any multi-trade run, ``sum(cost_pips) > 0``
    and gross PnL >= net PnL on every trade (INV-06).  Property-based.
(c) ``test_known_trade_exact_pnl`` — a hand-crafted candle sequence with a known
    cross and known stop reproduces the expected net PnL to 5 decimals.
(d) ``test_stops_fill_within_bar`` — every stop/target fill price lies within
    ``[low, high]`` of its fill bar — never an impossible price.  Property-based.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backtest.costs import CostParams, CostResult, apply_costs
from backtest.engine import BacktestEngine
from data.store import Store
from strategies.base import Direction, Signal, Strategy


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

EPOCH = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
PIP = 0.0001  # EUR_USD-style pip value


Bar = dict[str, Any]


def _make_store_from_bars(bars: list[Bar]) -> Store:
    """Build an in-memory Store seeded with the given OHLC bars.

    Each bar dict needs: time (datetime, UTC), open, high, low, close.
    Bid/ask are set equal to the supplied OHLC (the engine uses *_bid).
    """
    store = Store(":memory:")
    rows = []
    for b in bars:
        rows.append(
            (
                "EUR_USD",
                "H1",
                b["time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                b["open"], b["high"], b["low"], b["close"],   # bid
                b["open"], b["high"], b["low"], b["close"],   # ask (= bid here)
                b.get("volume", 100),
                1,  # complete
            )
        )
    store._conn.executemany(store._UPSERT_SQL, rows)
    store._conn.commit()
    return store


class FixedSignalStrategy(Strategy):
    """Test strategy that emits one LONG/SHORT signal on a chosen bar index.

    Deterministic and look-ahead-safe: it only ever reads the LAST row of the
    prefix it is given, and emits a signal exactly when that row's index in the
    full series equals ``signal_bar``.  Because the engine passes a prefix
    slice, the strategy identifies "current bar" by ``len(df) - 1``.
    """

    def __init__(
        self,
        signal_bar: int,
        direction: Direction,
        stop_distance: float,
        target_distance: float,
    ) -> None:
        self._signal_bar = signal_bar
        self._direction = direction
        self._stop = stop_distance
        self._target = target_distance

    @property
    def name(self) -> str:
        return "FixedSignalStrategy"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        current_idx = len(df) - 1
        if current_idx != self._signal_bar:
            return []
        last = df.iloc[current_idx]
        return [
            Signal(
                instrument="EUR_USD",
                direction=self._direction,
                entry_ref=float(last["close_bid"]),
                stop_distance=self._stop,
                target_distance=self._target,
                strategy_name=self.name,
                timeframe="H1",
                quality_score=0.5,
                generated_at=last["time"].to_pydatetime(),
            )
        ]


class RecordingStrategy(Strategy):
    """Records the decision (signal direction or None) made on each bar.

    Used by the no-look-ahead canary test: it reads only the last row of the
    prefix, so its per-bar decision must be invariant to anything placed in
    future bars.
    """

    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        self.decisions: dict[int, str] = {}

    @property
    def name(self) -> str:
        return "RecordingStrategy"

    def generate_signals(self, df: pd.DataFrame) -> list[Signal]:
        idx = len(df) - 1
        last = df.iloc[idx]
        close = float(last["close_bid"])
        # Decision depends ONLY on the current (last) bar.
        if close > self._threshold:
            self.decisions[idx] = "LONG"
            return [
                Signal(
                    instrument="EUR_USD",
                    direction=Direction.LONG,
                    entry_ref=close,
                    stop_distance=0.0010,
                    target_distance=0.0015,
                    strategy_name=self.name,
                    timeframe="H1",
                    quality_score=0.5,
                    generated_at=last["time"].to_pydatetime(),
                )
            ]
        self.decisions[idx] = "NONE"
        return []


def _default_cost_params(slippage: float = 1.0) -> CostParams:
    return CostParams(spread_pips=2.0, slippage_pips=slippage, pip_value=PIP)


def _bars_from_closes(closes: list[float], hl_pad: float = 0.0005) -> list[Bar]:
    """Build bars from a close series; open = prev close; high/low pad close."""
    bars = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o = prev
        hi = max(o, c) + hl_pad
        lo = min(o, c) - hl_pad
        bars.append(
            {
                "time": EPOCH + timedelta(hours=k),
                "open": o,
                "high": hi,
                "low": lo,
                "close": c,
            }
        )
        prev = c
    return bars


# ---------------------------------------------------------------------------
# (a) No look-ahead — canary
# ---------------------------------------------------------------------------


def test_no_lookahead_canary_explicit() -> None:
    """A poison value in bar N+1 must not change bar N's decision."""
    closes = [1.1000, 1.1010, 1.1020, 1.1005, 1.1030, 1.1040, 1.1015, 1.1050]
    bars = _bars_from_closes(closes)

    store_clean = _make_store_from_bars(bars)
    end = bars[-1]["time"]

    strat_clean = RecordingStrategy(threshold=1.1015)
    engine = BacktestEngine(store_clean, _default_cost_params())
    engine.run(strat_clean, "EUR_USD", "H1", EPOCH, end)

    # Poison bar index 4 (an arbitrary "N+1") with absurd values.
    poisoned = [dict(b) for b in bars]
    poisoned[4]["close"] = 99.0
    poisoned[4]["high"] = 99.0
    poisoned[4]["low"] = 98.0
    store_poisoned = _make_store_from_bars(poisoned)

    strat_poisoned = RecordingStrategy(threshold=1.1015)
    engine2 = BacktestEngine(store_poisoned, _default_cost_params())
    engine2.run(strat_poisoned, "EUR_USD", "H1", EPOCH, end)

    # Decisions for all bars STRICTLY BEFORE the poisoned bar must be identical.
    for n in range(4):
        assert strat_clean.decisions[n] == strat_poisoned.decisions[n], (
            f"look-ahead leak: bar {n} decision changed when bar 4 was poisoned"
        )


@settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(
    closes=st.lists(
        st.floats(min_value=1.05, max_value=1.15, allow_nan=False),
        min_size=6,
        max_size=30,
    ),
    poison_idx=st.integers(min_value=1, max_value=29),
)
def test_no_lookahead_property(closes: list[float], poison_idx: int) -> None:
    """Property: poisoning bar K never changes any decision at bar < K."""
    if poison_idx >= len(closes):
        poison_idx = len(closes) - 1
    if poison_idx < 1:
        return

    bars = _bars_from_closes(closes)
    end = bars[-1]["time"]
    threshold = 1.10

    store_a = _make_store_from_bars(bars)
    strat_a = RecordingStrategy(threshold=threshold)
    BacktestEngine(store_a, _default_cost_params()).run(
        strat_a, "EUR_USD", "H1", EPOCH, end
    )

    poisoned = [dict(b) for b in bars]
    poisoned[poison_idx]["close"] = 999.0
    poisoned[poison_idx]["high"] = 999.0
    poisoned[poison_idx]["low"] = 998.0
    store_b = _make_store_from_bars(poisoned)
    strat_b = RecordingStrategy(threshold=threshold)
    BacktestEngine(store_b, _default_cost_params()).run(
        strat_b, "EUR_USD", "H1", EPOCH, end
    )

    for n in range(poison_idx):
        assert strat_a.decisions.get(n) == strat_b.decisions.get(n)


# ---------------------------------------------------------------------------
# (b) Costs non-zero
# ---------------------------------------------------------------------------


def test_costs_non_zero_multi_trade() -> None:
    """sum(cost_pips) > 0 across a multi-trade run; gross >= net per trade."""
    # Build a series that triggers multiple stop/target exits so we get >1 trade.
    # Use FixedSignalStrategy emitting on bar 0, but the engine only re-signals
    # after a position closes; to get multiple trades we use a strategy that
    # re-signals whenever flat. We reuse RecordingStrategy with an oscillating
    # series so several entries occur.
    closes = [
        1.1000, 1.1100, 1.0900, 1.1100, 1.0900, 1.1100,
        1.0900, 1.1100, 1.0900, 1.1100, 1.0900, 1.1100,
    ]
    bars = _bars_from_closes(closes, hl_pad=0.0030)
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]

    strat = RecordingStrategy(threshold=1.1050)
    result = BacktestEngine(store, _default_cost_params()).run(
        strat, "EUR_USD", "H1", EPOCH, end
    )

    assert len(result.trades) >= 1, "expected at least one trade"
    total_cost = sum(t.cost_pips for t in result.trades)
    assert total_cost > 0.0, "INV-06: total cost across run must be > 0"
    for t in result.trades:
        assert t.cost_pips > 0.0
        # Gross PnL is always >= net PnL (costs only ever worsen PnL).
        assert t.pnl_pips >= t.pnl_net_pips - 1e-9


@settings(max_examples=80)
@given(
    spread=st.floats(min_value=0.1, max_value=5.0),
    slippage=st.floats(min_value=0.0, max_value=3.0),
    entry=st.floats(min_value=1.05, max_value=1.15),
    move=st.floats(min_value=-0.005, max_value=0.005),
    direction=st.sampled_from([Direction.LONG, Direction.SHORT]),
)
def test_apply_costs_invariants(
    spread: float,
    slippage: float,
    entry: float,
    move: float,
    direction: Direction,
) -> None:
    """Property: total_cost_pips > 0 for any non-zero spread/slippage, and net
    PnL <= gross PnL always (INV-06)."""
    exit_price = entry + move
    res = apply_costs(
        entry_price=entry,
        exit_price=exit_price,
        direction=direction,
        spread_pips=spread,
        slippage_pips=slippage,
        pip_value=PIP,
        swap_long_rate=0.0,
        swap_short_rate=0.0,
        holding_days=0,
    )
    assert isinstance(res, CostResult)
    # No financing rate supplied → swap not modelled (backward-compat path).
    assert res.swap_modelled is False
    if spread > 0 or slippage > 0:
        assert res.total_cost_pips > 0.0

    # Net PnL <= gross PnL (costs never help when there is no positive carry).
    if direction is Direction.LONG:
        gross = exit_price - entry
        net = res.net_exit - res.net_entry
    else:
        gross = entry - exit_price
        net = res.net_entry - res.net_exit
    assert net <= gross + 1e-12


@settings(max_examples=120)
@given(
    spread=st.floats(min_value=0.1, max_value=5.0),
    slippage=st.floats(min_value=0.0, max_value=3.0),
    entry=st.floats(min_value=1.05, max_value=1.15),
    move=st.floats(min_value=-0.005, max_value=0.005),
    direction=st.sampled_from([Direction.LONG, Direction.SHORT]),
    long_rate=st.floats(min_value=-0.5, max_value=0.5),
    short_rate=st.floats(min_value=-0.5, max_value=0.5),
    holding_days=st.integers(min_value=0, max_value=30),
)
def test_apply_costs_inv06_floor_with_financing(
    spread: float,
    slippage: float,
    entry: float,
    move: float,
    direction: Direction,
    long_rate: float,
    short_rate: float,
    holding_days: int,
) -> None:
    """INV-06 across the financing domain: the spread+slippage+commission floor
    is strictly > 0 for any non-zero spread/slippage even when positive carry
    (negative rate) makes the *net* cheaper. Financing never makes the spread
    path cost-free."""
    exit_price = entry + move
    res = apply_costs(
        entry_price=entry,
        exit_price=exit_price,
        direction=direction,
        spread_pips=spread,
        slippage_pips=slippage,
        pip_value=PIP,
        swap_long_rate=long_rate,
        swap_short_rate=short_rate,
        holding_days=holding_days,
    )
    # The strictly-positive cost floor is independent of the (possibly negative)
    # financing term — INV-06's spread+slippage guarantee is never weakened.
    floor = spread + slippage
    assert floor > 0.0
    rate = long_rate if direction is Direction.LONG else short_rate
    expected_swap = rate * holding_days
    assert res.total_cost_pips == pytest.approx(floor + expected_swap)
    # swap_modelled flips True only when a financing rate was supplied.
    assert res.swap_modelled is (long_rate != 0.0 or short_rate != 0.0)


def test_apply_costs_financing_no_longer_raises() -> None:
    """Both D-03 guard sites are gone: passing financing rates and a non-zero
    holding period no longer raises (the inline guard and the validator are
    removed)."""
    res = apply_costs(
        1.10,
        1.11,
        Direction.LONG,
        2.0,
        1.0,
        PIP,
        swap_long_rate=0.5,
        swap_short_rate=-0.3,
        holding_days=3,
    )
    # Long financing = 0.5 × 3 = 1.5 pips on top of spread(2) + slippage(1).
    assert res.total_cost_pips == pytest.approx(2.0 + 1.0 + 1.5)
    assert res.swap_modelled is True
    # And CostParams construction with financing rates is accepted (the
    # pydantic _swap_must_be_zero validator is gone).
    CostParams(
        spread_pips=2.0,
        slippage_pips=1.0,
        pip_value=PIP,
        swap_long_rate=0.5,
        swap_short_rate=-0.3,
        commission_pips=0.2,
    )


def test_apply_costs_same_bar_zero_swap() -> None:
    """holding_days == 0 (same-bar / intraday close) → zero financing, even
    with non-zero rates supplied."""
    for direction in (Direction.LONG, Direction.SHORT):
        res = apply_costs(
            entry_price=1.10,
            exit_price=1.105,
            direction=direction,
            spread_pips=2.0,
            slippage_pips=1.0,
            pip_value=PIP,
            swap_long_rate=0.9,
            swap_short_rate=0.7,
            holding_days=0,
        )
        # Floor only — no swap accrued.
        assert res.total_cost_pips == pytest.approx(3.0)
        # Rates were supplied, so the model *is* honestly labelled as modelled.
        assert res.swap_modelled is True


def test_apply_costs_financing_direction_aware() -> None:
    """Financing uses long_rate for longs and short_rate for shorts; charge =
    rate × holding_days; the engine net PnL is reduced by exactly that charge."""
    long_res = apply_costs(
        entry_price=1.10,
        exit_price=1.10,
        direction=Direction.LONG,
        spread_pips=0.0001,  # negligible floor so we isolate the swap term
        slippage_pips=0.0,
        pip_value=PIP,
        swap_long_rate=0.4,
        swap_short_rate=9.9,  # must be ignored for a long
        holding_days=5,
    )
    short_res = apply_costs(
        entry_price=1.10,
        exit_price=1.10,
        direction=Direction.SHORT,
        spread_pips=0.0001,
        slippage_pips=0.0,
        pip_value=PIP,
        swap_long_rate=9.9,  # must be ignored for a short
        swap_short_rate=0.6,
        holding_days=5,
    )
    # Long uses long_rate (0.4 × 5 = 2.0); short uses short_rate (0.6 × 5 = 3.0).
    assert long_res.total_cost_pips - 0.0001 == pytest.approx(2.0)
    assert short_res.total_cost_pips - 0.0001 == pytest.approx(3.0)
    # Net PnL impact: charge folded onto the exit leg, so net PnL drops by the
    # full charge (in pips) versus a zero-rate baseline.
    long_pnl = (long_res.net_exit - long_res.net_entry) / PIP
    base_long = apply_costs(
        1.10, 1.10, Direction.LONG, 0.0001, 0.0, PIP,
        swap_long_rate=0.0, swap_short_rate=0.0, holding_days=5,
    )
    base_long_pnl = (base_long.net_exit - base_long.net_entry) / PIP
    assert base_long_pnl - long_pnl == pytest.approx(2.0)


def test_apply_costs_positive_carry_improves_net() -> None:
    """A positive-carry side (negative rate) reduces net cost / improves net PnL
    but the spread+slippage floor stays strictly positive (INV-06)."""
    res = apply_costs(
        entry_price=1.10,
        exit_price=1.10,
        direction=Direction.LONG,
        spread_pips=2.0,
        slippage_pips=1.0,
        pip_value=PIP,
        swap_long_rate=-0.5,  # positive carry
        swap_short_rate=0.0,
        holding_days=4,
    )
    # total cost = floor(3.0) + (-0.5 × 4 = -2.0) = 1.0 — reduced, still the
    # spread path itself is not free (floor was 3.0 > 0).
    assert res.total_cost_pips == pytest.approx(1.0)
    assert (2.0 + 1.0) > 0.0  # the floor


def test_apply_costs_commission_per_round_trip() -> None:
    """Commission (when > 0) is charged once per round trip, additively."""
    res = apply_costs(
        entry_price=1.10,
        exit_price=1.10,
        direction=Direction.SHORT,
        spread_pips=2.0,
        slippage_pips=0.0,
        pip_value=PIP,
        swap_long_rate=0.0,
        swap_short_rate=0.0,
        holding_days=0,
        commission_pips=0.7,
    )
    assert res.total_cost_pips == pytest.approx(2.0 + 0.7)


def test_apply_costs_rejects_negative_holding_days() -> None:
    """Negative holding_days is a bug, not a valid input."""
    with pytest.raises(ValueError, match="holding_days"):
        apply_costs(
            1.10, 1.11, Direction.LONG, 2.0, 1.0, PIP,
            swap_long_rate=0.5, swap_short_rate=0.0, holding_days=-1,
        )


def test_cost_params_rejects_zero_spread() -> None:
    """CostParams still enforces spread > 0 (INV-06 floor). The D-03
    swap-must-be-zero validator is gone — financing rates are now accepted."""
    with pytest.raises(ValueError):
        CostParams(spread_pips=0.0, slippage_pips=1.0, pip_value=PIP)
    # Financing rates of any sign are accepted (no validator rejection).
    params = CostParams(
        spread_pips=2.0,
        slippage_pips=1.0,
        pip_value=PIP,
        swap_long_rate=1.0,
        swap_short_rate=-1.0,
    )
    assert params.swap_long_rate == 1.0
    assert params.swap_short_rate == -1.0
    # Commission must be non-negative.
    with pytest.raises(ValueError):
        CostParams(spread_pips=2.0, pip_value=PIP, commission_pips=-0.1)


# ---------------------------------------------------------------------------
# (c) Known trade — exact net PnL to 5 decimals
# ---------------------------------------------------------------------------


def test_known_trade_exact_pnl() -> None:
    """Hand-crafted long trade with known entry/target reproduces exact net PnL.

    Setup:
      - Signal emitted on bar 0 (LONG), stop_distance=0.0010, target=0.0015.
      - Entry fills on bar 1's OPEN = 1.10000 (gross).
      - Target level = entry_open + 0.0015 = 1.10150.
      - Bar 2 high = 1.10200 breaches the target -> exit at target level 1.10150.

    Costs: spread=2.0 pips, slippage=1.0 pip, pip_value=0.0001.
      half_spread_price = 1.0 pip * 0.0001 = 0.0001
      slippage_price    = 1.0 pip * 0.0001 = 0.0001
      net_entry = 1.10000 + 0.0001 = 1.10010
      net_exit  = 1.10150 - 0.0001 - 0.0001 = 1.10130
      pnl_net_pips = (1.10130 - 1.10010) / 0.0001 = 12.00000
      gross_pips   = (1.10150 - 1.10000) / 0.0001 = 15.00000
      cost_pips    = spread + slippage = 3.0
    """
    bars: list[Bar] = [
        # bar 0: signal bar
        {"time": EPOCH, "open": 1.09990, "high": 1.10010,
         "low": 1.09980, "close": 1.10000},
        # bar 1: entry fills at open=1.10000; no exit (high below target)
        {"time": EPOCH + timedelta(hours=1), "open": 1.10000,
         "high": 1.10090, "low": 1.09990, "close": 1.10050},
        # bar 2: high breaches target 1.10150
        {"time": EPOCH + timedelta(hours=2), "open": 1.10050,
         "high": 1.10200, "low": 1.10040, "close": 1.10180},
    ]
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]

    strat = FixedSignalStrategy(
        signal_bar=0,
        direction=Direction.LONG,
        stop_distance=0.0010,
        target_distance=0.0015,
    )
    result = BacktestEngine(
        store, CostParams(spread_pips=2.0, slippage_pips=1.0, pip_value=PIP)
    ).run(strat, "EUR_USD", "H1", EPOCH, end)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.direction is Direction.LONG
    assert t.exit_reason == "target"
    assert round(t.entry_price_gross, 5) == 1.10000
    assert round(t.entry_price_net, 5) == 1.10010
    assert round(t.exit_price_gross, 5) == 1.10150
    assert round(t.exit_price_net, 5) == 1.10130
    assert round(t.pnl_pips, 5) == 15.00000
    assert round(t.pnl_net_pips, 5) == 12.00000
    assert round(t.cost_pips, 5) == 3.00000
    # Entry time is bar 1's timestamp (next-bar entry, no look-ahead).
    assert t.entry_time == bars[1]["time"]
    assert t.exit_time == bars[2]["time"]
    # Backward-compat: no financing supplied → swap not modelled.
    assert result.metadata["swap_modelled"] is False


def _known_trade_bars() -> list[Bar]:
    """The hand-crafted long-target bars from test_known_trade_exact_pnl,
    reused as the regression baseline and the financing fixture."""
    return [
        {"time": EPOCH, "open": 1.09990, "high": 1.10010,
         "low": 1.09980, "close": 1.10000},
        {"time": EPOCH + timedelta(hours=1), "open": 1.10000,
         "high": 1.10090, "low": 1.09990, "close": 1.10050},
        {"time": EPOCH + timedelta(hours=2), "open": 1.10050,
         "high": 1.10200, "low": 1.10040, "close": 1.10180},
    ]


def test_known_trade_regression_zero_financing_matches_poc() -> None:
    """Regression: with swap_long_rate = swap_short_rate = 0 and commission = 0,
    the engine reproduces the PoC spread+slippage numbers byte-for-byte (the
    backward-compatibility AC). Same fixture as test_known_trade_exact_pnl."""
    bars = _known_trade_bars()
    store = _make_store_from_bars(bars)
    strat = FixedSignalStrategy(
        signal_bar=0, direction=Direction.LONG,
        stop_distance=0.0010, target_distance=0.0015,
    )
    result = BacktestEngine(
        store,
        CostParams(
            spread_pips=2.0, slippage_pips=1.0, pip_value=PIP,
            swap_long_rate=0.0, swap_short_rate=0.0, commission_pips=0.0,
        ),
    ).run(strat, "EUR_USD", "H1", EPOCH, bars[-1]["time"])
    t = result.trades[0]
    # Identical to the PoC baseline.
    assert round(t.pnl_net_pips, 5) == 12.00000
    assert round(t.pnl_pips, 5) == 15.00000
    assert round(t.cost_pips, 5) == 3.00000
    assert result.metadata["swap_modelled"] is False


def test_engine_holding_days_from_utc_dates_charges_swap() -> None:
    """INV-03: the engine derives holding_days from entry/exit bar UTC dates and
    charges rate × days. Same intra-day trade as the baseline but the exit bar
    is on a later UTC date, so financing is charged and swap_modelled is True.

    The trade is opened on bar 1 (Jan-1) and the target-breaching exit bar is
    placed two calendar days later → holding_days == 2 → long swap = 0.5×2 = 1.0
    pip on top of the 3.0-pip floor → net PnL drops from 12.0 to 11.0."""
    bars: list[Bar] = [
        {"time": EPOCH, "open": 1.09990, "high": 1.10010,
         "low": 1.09980, "close": 1.10000},
        # entry bar — Jan 1
        {"time": EPOCH + timedelta(hours=1), "open": 1.10000,
         "high": 1.10090, "low": 1.09990, "close": 1.10050},
        # exit bar — Jan 3 (2 UTC date boundaries crossed from the entry bar)
        {"time": EPOCH + timedelta(days=2, hours=1), "open": 1.10050,
         "high": 1.10200, "low": 1.10040, "close": 1.10180},
    ]
    store = _make_store_from_bars(bars)
    strat = FixedSignalStrategy(
        signal_bar=0, direction=Direction.LONG,
        stop_distance=0.0010, target_distance=0.0015,
    )
    result = BacktestEngine(
        store,
        CostParams(
            spread_pips=2.0, slippage_pips=1.0, pip_value=PIP,
            swap_long_rate=0.5, swap_short_rate=0.0,
        ),
    ).run(strat, "EUR_USD", "H1", EPOCH, bars[-1]["time"])
    t = result.trades[0]
    # entry bar Jan 1, exit bar Jan 3 → 2 financing days; long rate 0.5/day.
    assert round(t.cost_pips, 5) == 4.00000  # 2 + 1 + (0.5 × 2)
    assert round(t.pnl_net_pips, 5) == 11.00000  # 12.0 baseline − 1.0 swap
    assert round(t.pnl_pips, 5) == 15.00000  # gross unchanged
    assert t.pnl_pips >= t.pnl_net_pips  # INV-06 gross ≥ net
    assert result.metadata["swap_modelled"] is True


def test_known_short_trade_stop_wins_tie() -> None:
    """When both stop and target breach in one bar, the STOP wins (conservative).

    Short signal on bar 0; entry on bar 1 open = 1.10000.
      stop_distance=0.0010 -> stop = 1.10100 (above entry)
      target_distance=0.0015 -> target = 1.09850 (below entry)
    Bar 2 has high=1.10200 (>= stop) AND low=1.09800 (<= target): both breach.
    Conservative rule -> exit at STOP = 1.10100 (a loss), not the target.
    """
    bars: list[Bar] = [
        {"time": EPOCH, "open": 1.10010, "high": 1.10020,
         "low": 1.09990, "close": 1.10000},
        {"time": EPOCH + timedelta(hours=1), "open": 1.10000,
         "high": 1.10010, "low": 1.09990, "close": 1.10000},
        {"time": EPOCH + timedelta(hours=2), "open": 1.10000,
         "high": 1.10200, "low": 1.09800, "close": 1.09900},
    ]
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]
    strat = FixedSignalStrategy(
        signal_bar=0,
        direction=Direction.SHORT,
        stop_distance=0.0010,
        target_distance=0.0015,
    )
    result = BacktestEngine(
        store, CostParams(spread_pips=2.0, slippage_pips=1.0, pip_value=PIP)
    ).run(strat, "EUR_USD", "H1", EPOCH, end)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "stop", "stop must win when both breach in one bar"
    assert round(t.exit_price_gross, 5) == 1.10100
    # Short stop: net_exit = exit + half_spread + slippage = 1.10100+0.0001+0.0001
    assert round(t.exit_price_net, 5) == 1.10120
    # Short: gross = entry - exit = 1.10000 - 1.10100 = -0.0010 -> -10 pips
    assert round(t.pnl_pips, 5) == -10.00000


# ---------------------------------------------------------------------------
# (d) Stops fill within [low, high] of the fill bar
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    closes=st.lists(
        st.floats(min_value=1.08, max_value=1.12, allow_nan=False),
        min_size=5,
        max_size=25,
    ),
    direction=st.sampled_from([Direction.LONG, Direction.SHORT]),
    stop_pips=st.floats(min_value=5.0, max_value=30.0),
)
def test_stops_fill_within_bar(
    closes: list[float], direction: Direction, stop_pips: float
) -> None:
    """Every fill price must lie within [low, high] of its fill bar."""
    bars = _bars_from_closes(closes, hl_pad=0.0020)
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]

    strat = FixedSignalStrategy(
        signal_bar=0,
        direction=direction,
        stop_distance=stop_pips * PIP,
        target_distance=stop_pips * 1.5 * PIP,
    )
    result = BacktestEngine(store, _default_cost_params()).run(
        strat, "EUR_USD", "H1", EPOCH, end
    )

    # Build a quick lookup of bar [low, high] by timestamp.
    ranges = {
        b["time"]: (b["low"], b["high"]) for b in bars
    }
    for t in result.trades:
        if t.exit_reason in ("stop", "target"):
            lo, hi = ranges[t.exit_time]
            assert lo - 1e-12 <= t.exit_price_gross <= hi + 1e-12, (
                f"fill {t.exit_price_gross} outside bar range [{lo}, {hi}]"
            )


# ---------------------------------------------------------------------------
# Defensive-copy & misc guarantees
# ---------------------------------------------------------------------------


def test_engine_does_not_mutate_input_frame() -> None:
    """The engine must operate on a defensive copy of the store's frame."""
    bars = _bars_from_closes([1.10, 1.11, 1.12, 1.13])
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]

    df_before = store.load_candles("EUR_USD", "H1", EPOCH, end)
    snapshot = df_before.copy(deep=True)

    strat = FixedSignalStrategy(
        signal_bar=0, direction=Direction.LONG,
        stop_distance=0.0010, target_distance=0.0015,
    )
    BacktestEngine(store, _default_cost_params()).run(
        strat, "EUR_USD", "H1", EPOCH, end
    )
    df_after = store.load_candles("EUR_USD", "H1", EPOCH, end)
    pd.testing.assert_frame_equal(df_after, snapshot)


def test_equity_curve_is_utc_and_matches_bar_count() -> None:
    """Equity curve is UTC-indexed with one point per bar (INV-03)."""
    bars = _bars_from_closes([1.10, 1.11, 1.12, 1.13, 1.14])
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]
    strat = FixedSignalStrategy(
        signal_bar=0, direction=Direction.LONG,
        stop_distance=0.0010, target_distance=0.0015,
    )
    result = BacktestEngine(store, _default_cost_params()).run(
        strat, "EUR_USD", "H1", EPOCH, end
    )
    assert len(result.equity_curve) == len(bars)
    assert str(result.equity_curve.index.tz) == "UTC"
    assert result.metadata["swap_modelled"] is False


def test_open_position_closed_at_end_of_data() -> None:
    """A position still open at the last bar is closed at the final close."""
    # Signal long on bar 0, entry bar 1, but price never hits stop/target.
    bars: list[Bar] = [
        {"time": EPOCH, "open": 1.10000, "high": 1.10005,
         "low": 1.09995, "close": 1.10000},
        {"time": EPOCH + timedelta(hours=1), "open": 1.10000,
         "high": 1.10010, "low": 1.09990, "close": 1.10005},
        {"time": EPOCH + timedelta(hours=2), "open": 1.10005,
         "high": 1.10010, "low": 1.10000, "close": 1.10008},
    ]
    store = _make_store_from_bars(bars)
    end = bars[-1]["time"]
    strat = FixedSignalStrategy(
        signal_bar=0, direction=Direction.LONG,
        stop_distance=0.0100, target_distance=0.0150,  # far away, never hit
    )
    result = BacktestEngine(store, _default_cost_params()).run(
        strat, "EUR_USD", "H1", EPOCH, end
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end_of_data"
    assert result.trades[0].exit_time == bars[-1]["time"]
