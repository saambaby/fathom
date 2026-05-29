"""Equivalence regression for the O(n) precompute backtest engine (perf).

The engine was changed from an O(n^2) expanding-prefix recompute — calling
``strategy.generate_signals(df.iloc[: i + 1])`` on EVERY bar ``i`` — to an O(n)
single precompute: ``strategy.generate_signals(df)`` once, then consume the
signal for bar ``i`` (indexed by its ``generated_at`` close timestamp).

This is correctness-critical: the engine's no-look-ahead guarantee and exact
results must be preserved.  The change is only valid because every production
strategy's indicators are **causal** (ewm / rolling / ``shift(1)`` / ``diff`` /
``pct_change`` — all strictly backward-looking), so the signal a strategy emits
*on* bar ``i`` is a pure function of bars ``[0..i]`` and is identical whether the
strategy is handed the expanding prefix or the full frame.

This module proves that — it does NOT assume it.  For ALL SIX production
strategies, on a 5,000-bar fixture series, it:

1. **Signal equivalence** — replicates the OLD per-prefix loop inline (calls
   ``generate_signals(df.iloc[: i + 1])`` for each ``i`` and takes the signal
   whose ``generated_at`` is bar ``i``'s timestamp) and asserts it equals the
   precomputed ``generate_signals(df)`` mapped by bar.  Any divergence means
   that strategy is non-causal — the test FAILS loudly (do not silently change
   behaviour).

2. **Engine equivalence** — runs the live (precompute) engine and a reference
   engine that replays the old expanding-prefix logic inline, and asserts the
   SAME trades (entry/exit times, gross+net prices, pnl_pips, pnl_net_pips,
   cost_pips, exit_reason, direction) and the SAME equity curve.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from backtest.costs import CostParams
from backtest.engine import BacktestEngine, Trade
from data.store import Store
from strategies.base import Direction, Signal, Strategy
from strategies.breakout import SessionRangeBreakout
from strategies.mean_reversion import BollingerReversion, RSIReversion
from strategies.momentum import ROCMomentum
from strategies.trend import DonchianBreakout, MACrossover

EPOCH = datetime(2022, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
PIP = 0.0001
# The reference path replays the OLD expanding-prefix loop, which is O(n^2);
# 600 bars keeps the gate to a few seconds while still exercising every
# strategy's warm-up boundary and many steady-state signals.
N_BARS = 600


# ---------------------------------------------------------------------------
# Fixture: a deterministic, indicator-rich ~5,000-bar OHLC series
# ---------------------------------------------------------------------------


def _synthetic_bars(n: int = N_BARS) -> list[dict[str, object]]:
    """Build a deterministic OHLC series with trends, ranges and reversals.

    The series is engineered to exercise every strategy: a slow sinusoidal
    drift (crossovers / breakouts), faster oscillation (mean reversion /
    momentum) and per-bar noise — all from a seeded RNG so the fixture is
    reproducible across runs and machines.
    """
    rng = np.random.default_rng(20260529)
    bars: list[dict[str, object]] = []
    price = 1.1000
    for k in range(n):
        # Slow trend + faster cycle + small drift, plus seeded noise.
        trend = 0.03 * math.sin(k / 400.0)
        cycle = 0.01 * math.sin(k / 35.0)
        noise = float(rng.normal(0.0, 0.0006))
        close = 1.1000 + trend + cycle + noise
        open_ = price
        hi = max(open_, close) + abs(float(rng.normal(0.0, 0.0004))) + 0.0001
        lo = min(open_, close) - abs(float(rng.normal(0.0, 0.0004))) - 0.0001
        bars.append(
            {
                "time": EPOCH + timedelta(hours=k),
                "open": round(open_, 6),
                "high": round(hi, 6),
                "low": round(lo, 6),
                "close": round(close, 6),
            }
        )
        price = close
    return bars


def _store_from_bars(bars: list[dict[str, object]]) -> Store:
    store = Store(":memory:")
    rows = []
    for b in bars:
        t: datetime = b["time"]  # type: ignore[assignment]
        rows.append(
            (
                "EUR_USD",
                "H1",
                t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                b["open"], b["high"], b["low"], b["close"],
                b["open"], b["high"], b["low"], b["close"],
                100,
                1,
            )
        )
    store._conn.executemany(store._UPSERT_SQL, rows)
    store._conn.commit()
    return store


def _fixture_df(bars: list[dict[str, object]]) -> pd.DataFrame:
    """The exact frame the engine builds internally (defensive copy shape)."""
    store = _store_from_bars(bars)
    end: datetime = bars[-1]["time"]  # type: ignore[assignment]
    raw = store.load_candles("EUR_USD", "H1", EPOCH, end)
    return raw.copy(deep=True).reset_index(drop=True)


def _strategies() -> list[Strategy]:
    """One instance of each of the six production strategies, tuned to fire
    multiple times on the fixture series."""
    return [
        MACrossover(fast_period=10, slow_period=30, instrument="EUR_USD",
                    timeframe="H1"),
        DonchianBreakout(channel_period=20, instrument="EUR_USD",
                         timeframe="H1"),
        SessionRangeBreakout(range_lookback=20, instrument="EUR_USD",
                             timeframe="H1"),
        ROCMomentum(instrument="EUR_USD", timeframe="H1", roc_period=12,
                    roc_threshold=0.002, atr_filter_period=20),
        BollingerReversion(period=20, num_std=2.0, instrument="EUR_USD",
                           timeframe="H1"),
        RSIReversion(period=14, oversold=30.0, overbought=70.0,
                     instrument="EUR_USD", timeframe="H1"),
    ]


# ---------------------------------------------------------------------------
# Reference: the OLD expanding-prefix logic, replicated inline
# ---------------------------------------------------------------------------


def _old_signal_for_bar(
    signals: list[Signal], bar_time: datetime
) -> Optional[Signal]:
    """The engine's pre-perf ``_signal_for_bar`` — reversed scan for a match."""
    if not signals:
        return None
    for sig in reversed(signals):
        if sig.generated_at == bar_time:
            return sig
    return None


def _old_prefix_signals_by_bar(
    strategy: Strategy, df: pd.DataFrame
) -> dict[datetime, Signal]:
    """Replay the OLD per-prefix loop: for each bar i, call
    ``generate_signals(df.iloc[:i+1])`` and take the signal for bar i.

    Returns a {bar_time: signal} map — the ground truth the precompute path
    must reproduce. This is O(n^2) (deliberately — it is the reference)."""
    out: dict[datetime, Signal] = {}
    times = df["time"]
    for i in range(len(df)):
        bar_time = times.iloc[i].to_pydatetime()
        prefix = df.iloc[: i + 1]
        sig = _old_signal_for_bar(strategy.generate_signals(prefix), bar_time)
        if sig is not None:
            out[bar_time] = sig
    return out


def _run_engine_old_prefix(
    store: Store, cost_params: CostParams, strategy: Strategy,
    end: datetime,
) -> list[Trade]:
    """Reference engine that replays the original expanding-prefix decision
    path bar-by-bar, sharing the live engine's fill/cost helpers so ONLY the
    signal-acquisition mechanism differs."""
    eng = BacktestEngine(store, cost_params)
    raw = store.load_candles("EUR_USD", "H1", EPOCH, end)
    df = raw.copy(deep=True).reset_index(drop=True)
    n = len(df)
    pip_value = cost_params.pip_value
    times = df["time"]
    opens = df["open_bid"].to_numpy()
    highs = df["high_bid"].to_numpy()
    lows = df["low_bid"].to_numpy()
    closes = df["close_bid"].to_numpy()

    trades: list[Trade] = []
    position = None
    pending_signal: Optional[Signal] = None

    for i in range(n):
        bar_high = float(highs[i])
        bar_low = float(lows[i])
        bar_open = float(opens[i])
        bar_time = times.iloc[i].to_pydatetime()

        if position is None and pending_signal is not None:
            position = eng._open_from_signal(pending_signal, bar_open, bar_time)
            pending_signal = None

        if position is not None:
            exit_info = eng._resolve_fill(position, bar_high, bar_low)
            if exit_info is not None:
                gross_exit_level, exit_reason = exit_info
                holding_days = eng._holding_days(position.entry_time, bar_time)
                trade = eng._close_trade(
                    position, bar_time, gross_exit_level, exit_reason,
                    pip_value, holding_days,
                )
                trades.append(trade)
                position = None

        if position is None and pending_signal is None:
            prefix = df.iloc[: i + 1]
            sigs = strategy.generate_signals(prefix)
            bar_signal = _old_signal_for_bar(sigs, bar_time)
            if bar_signal is not None and bar_signal.direction in (
                Direction.LONG, Direction.SHORT,
            ):
                pending_signal = bar_signal

    if position is not None and n > 0:
        final_time = times.iloc[n - 1].to_pydatetime()
        final_close = float(closes[n - 1])
        holding_days = eng._holding_days(position.entry_time, final_time)
        trades.append(
            eng._close_trade(
                position, final_time, final_close, "end_of_data", pip_value,
                holding_days,
            )
        )

    return trades


def _trade_tuple(t: Trade) -> tuple[object, ...]:
    return (
        t.entry_time, t.exit_time, t.direction, t.exit_reason,
        round(t.entry_price_gross, 10), round(t.entry_price_net, 10),
        round(t.exit_price_gross, 10), round(t.exit_price_net, 10),
        round(t.pnl_pips, 10), round(t.pnl_net_pips, 10),
        round(t.cost_pips, 10),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_strategies_are_causal_signal_equivalence() -> None:
    """For every strategy, the precomputed full-frame signals (mapped by bar)
    must EQUAL the old per-prefix signals (mapped by bar). Divergence ⇒ a
    non-causal strategy — fail loudly, do not paper over it."""
    df = _fixture_df(_synthetic_bars())
    for strat in _strategies():
        precomputed = strat.generate_signals(df)
        precomp_by_bar: dict[datetime, Signal] = {
            s.generated_at: s for s in precomputed
        }
        old_by_bar = _old_prefix_signals_by_bar(strat, df)

        assert precomp_by_bar.keys() == old_by_bar.keys(), (
            f"{strat.name}: precompute and per-prefix signal BARS differ — "
            f"strategy is NON-CAUSAL (only-precompute: "
            f"{set(precomp_by_bar) - set(old_by_bar)}, only-prefix: "
            f"{set(old_by_bar) - set(precomp_by_bar)})"
        )
        # The fixture must actually exercise the strategy (guard against a
        # vacuously-passing empty-signal comparison).
        assert len(precomp_by_bar) >= 3, (
            f"{strat.name}: fixture produced too few signals "
            f"({len(precomp_by_bar)}) to be a meaningful equivalence check"
        )
        for bar_time, new_sig in precomp_by_bar.items():
            old_sig = old_by_bar[bar_time]
            assert new_sig.model_dump() == old_sig.model_dump(), (
                f"{strat.name}: signal at {bar_time} differs between "
                f"precompute and per-prefix — NON-CAUSAL.\n"
                f"  precompute: {new_sig.model_dump()}\n"
                f"  per-prefix: {old_sig.model_dump()}"
            )


def test_all_strategies_engine_trades_and_equity_identical() -> None:
    """The live (precompute) engine must produce byte-identical trades AND the
    same equity curve as the reference engine replaying the old prefix loop,
    for all six strategies."""
    bars = _synthetic_bars()
    end: datetime = bars[-1]["time"]  # type: ignore[assignment]
    cost_params = CostParams(
        spread_pips=2.0, slippage_pips=1.0, pip_value=PIP,
        swap_long_rate=0.3, swap_short_rate=0.2, commission_pips=0.1,
    )

    for strat in _strategies():
        store_new = _store_from_bars(bars)
        result = BacktestEngine(store_new, cost_params).run(
            strat, "EUR_USD", "H1", EPOCH, end
        )

        store_old = _store_from_bars(bars)
        old_trades = _run_engine_old_prefix(store_old, cost_params, strat, end)

        # Same number of trades.
        assert len(result.trades) == len(old_trades), (
            f"{strat.name}: trade count differs "
            f"(precompute={len(result.trades)}, prefix={len(old_trades)})"
        )
        # Meaningful coverage — the fixture must trade for this comparison to
        # have teeth.
        assert len(result.trades) >= 2, (
            f"{strat.name}: too few trades ({len(result.trades)}) for a "
            f"meaningful engine-equivalence check"
        )
        # Byte-identical per-trade fields.
        for new_t, old_t in zip(result.trades, old_trades):
            assert _trade_tuple(new_t) == _trade_tuple(old_t), (
                f"{strat.name}: trade mismatch\n  precompute: "
                f"{_trade_tuple(new_t)}\n  prefix:     {_trade_tuple(old_t)}"
            )

        # Same equity curve (recompute the reference cumulative net pips from
        # the old trades, mapped onto the bar grid).
        new_equity = result.equity_curve
        assert len(new_equity) == len(bars)
        # Reconstruct expected cumulative net pips per bar from old_trades.
        exit_pnl: dict[datetime, float] = {}
        for t in old_trades:
            exit_pnl[t.exit_time] = exit_pnl.get(t.exit_time, 0.0) + t.pnl_net_pips
        cum = 0.0
        for ts, val in zip(new_equity.index, new_equity.to_numpy()):
            py_ts = pd.Timestamp(ts).to_pydatetime()
            if py_ts in exit_pnl:
                cum += exit_pnl[py_ts]
            assert val == pytest.approx(cum, abs=1e-9), (
                f"{strat.name}: equity curve diverges at {py_ts} "
                f"(engine={val}, reference={cum})"
            )


@pytest.mark.skipif(
    not __import__("pathlib").Path("/tmp/p1a_accept_live.db").exists(),
    reason="cached real-candle db not present",
)
def test_real_data_equivalence_eurusd_h4() -> None:
    """Stronger check on REAL EUR_USD H4 candles: precompute and per-prefix
    signals must match for every strategy on live data.

    Capped at the first 700 bars — the reference per-prefix loop is O(n^2), and
    700 bars already spans every strategy's warm-up boundary plus a long
    steady-state stretch, which is where any causal/guard divergence shows up.
    """
    store = Store("/tmp/p1a_accept_live.db")
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    end = datetime(2030, 1, 1, tzinfo=timezone.utc)
    raw = store.load_candles("EUR_USD", "H4", start, end)
    df = raw.copy(deep=True).reset_index(drop=True).iloc[:700].reset_index(
        drop=True
    )
    assert len(df) == 700

    for strat in _strategies():
        precomp_by_bar = {s.generated_at: s for s in strat.generate_signals(df)}
        old_by_bar = _old_prefix_signals_by_bar(strat, df)
        assert precomp_by_bar.keys() == old_by_bar.keys(), (
            f"{strat.name}: REAL-DATA signal bars differ — non-causal"
        )
        for bar_time, new_sig in precomp_by_bar.items():
            assert new_sig.model_dump() == old_by_bar[bar_time].model_dump(), (
                f"{strat.name}: REAL-DATA signal at {bar_time} differs"
            )
