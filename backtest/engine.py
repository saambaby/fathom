"""Event-driven backtest engine (POC-T-05) — the thesis-proving component.

This engine walks a candle DataFrame **strictly bar by bar, in chronological
order**, and runs a :class:`~strategies.base.Strategy` forward through time
under a hard no-look-ahead guarantee.  Three downstream tasks (T-06, T-07,
T-08) and the project's go/no-go decision rest on its correctness, so the
correctness properties are spelled out explicitly below and pinned by tests.

No-look-ahead guarantee
-----------------------
Every strategy's indicators are **causal** — built only from ewm / rolling /
``shift(1)`` / ``diff`` / ``pct_change``, all strictly backward-looking — so the
signal a strategy emits *on* bar ``i`` is a pure function of bars ``[0..i]`` and
is identical whether the strategy is handed the expanding prefix
``df.iloc[: i + 1]`` or the full frame.  The engine exploits this to precompute
all signals **once** over the full frame (O(n) rather than the old O(n^2)
expanding-prefix recompute), then indexes them by the bar's close timestamp and
consumes only the signal *for bar ``i``* at bar ``i``.  A signal generated on
bar ``i`` is **entered at bar ``i + 1``'s open**, never at bar ``i``'s own
prices (which were the inputs to the signal).  Fill checks for an open position
on bar ``i`` use only bar ``i``'s OHLC.  The canary test (test_no_lookahead)
verifies this empirically by poisoning bar ``N + 1`` and asserting bar ``N``'s
decision is unchanged; a dedicated equivalence regression test
(test_engine_precompute_equivalence) replays the old per-prefix loop for all six
strategies and asserts byte-identical signals and trades.

Intrabar fill semantics
------------------------
* Long stop fills if ``bar.low <= stop`` ; short stop if ``bar.high >= stop``.
* Long target fills if ``bar.high >= target`` ; short target if ``bar.low <= target``.
* **If both stop and target breach within the same bar, the stop wins**
  (conservative — we cannot know the intrabar path, so we assume the worse
  outcome).
* The gross fill price is the stop/target *level*, then **clamped to**
  ``[bar.low, bar.high]`` so a fill can never be reported at an impossible
  price (test_stops_fill_within_bar).

Determinism / purity
---------------------
``run`` takes a defensive copy of the loaded DataFrame and never mutates the
caller's frame.  All timestamps in the output are sourced from the bar data
(UTC-aware, INV-03) — never ``datetime.now()``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict

from backtest.costs import CostParams, apply_costs
from data.store import Store
from strategies.base import Direction, Signal, Strategy


class Trade(BaseModel):
    """A single completed round-trip trade.

    All prices are in instrument price units; PnL is in pips.  ``*_gross``
    prices are the raw fill levels; ``*_net`` prices include spread + slippage.

    INV-03: ``entry_time`` and ``exit_time`` are UTC-aware datetimes taken from
    the bar data — never wall-clock time.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    entry_time: datetime
    exit_time: datetime
    entry_price_gross: float
    entry_price_net: float
    exit_price_gross: float
    exit_price_net: float
    direction: Direction
    pnl_pips: float
    pnl_net_pips: float
    cost_pips: float
    exit_reason: str  # "stop" | "target" | "end_of_data"


class BacktestResult(BaseModel):
    """Result of a single backtest run.

    Attributes
    ----------
    trades:
        Completed round-trip trades, in chronological order.
    equity_curve:
        Cumulative **net** PnL in pips, indexed by bar close time (UTC).  One
        point per bar; flat across bars with no realised PnL.
    metadata:
        Run metadata including ``swap_modelled`` (``True`` when financing
        rates were supplied — P1A-T-03), instrument, granularity, bar count,
        and the cost parameters used (spread, slippage, financing, commission).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trades: list[Trade]
    equity_curve: pd.Series
    metadata: dict[str, object]


class _OpenPosition:
    """Mutable bookkeeping for the single open position during a run.

    Not part of the public API — internal to the engine's bar loop.
    """

    __slots__ = (
        "direction",
        "entry_time",
        "entry_price_gross",
        "stop_price",
        "target_price",
    )

    def __init__(
        self,
        direction: Direction,
        entry_time: datetime,
        entry_price_gross: float,
        stop_price: float,
        target_price: float,
    ) -> None:
        self.direction = direction
        self.entry_time = entry_time
        self.entry_price_gross = entry_price_gross
        self.stop_price = stop_price
        self.target_price = target_price


class BacktestEngine:
    """Strict-chronological, single-position event-driven backtester.

    Parameters
    ----------
    store:
        The candle store (T-03).  ``run`` loads candles via
        ``store.load_candles`` and operates on a defensive copy.
    cost_params:
        Spread + slippage + commission + financing configuration.  The engine
        maps ``InstrumentMeta.long_rate``/``short_rate`` into
        ``CostParams.swap_long_rate``/``swap_short_rate`` at construction (done
        by the caller / runner).  Required, not optional — a backtest without
        costs is invalid (INV-06).  ``CostParams`` enforces ``spread_pips > 0``,
        so the engine can never run cost-free.

    Notes
    -----
    The engine holds **at most one open position at a time** (PoC scope).  A new
    signal is ignored while a position is open; once the position closes, the
    next signal on a later bar can open a new one.
    """

    def __init__(self, store: Store, cost_params: CostParams) -> None:
        self._store = store
        self._cost_params = cost_params

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        strategy: Strategy,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> BacktestResult:
        """Run ``strategy`` over ``[start, end]`` for one instrument/granularity.

        Parameters
        ----------
        strategy:
            The strategy to drive forward through time.
        instrument, granularity:
            Selectors passed straight to ``store.load_candles``.
        start, end:
            Inclusive UTC-aware range bounds (validated by the store).

        Returns
        -------
        BacktestResult
            Trades, equity curve (cumulative net pips), and metadata
            (including ``swap_modelled`` — ``True`` when financing rates were
            supplied via ``CostParams``, ``False`` only on a spread-only run;
            P1A-T-03 lifted the D-03 swap deferral).
        """
        raw = self._store.load_candles(instrument, granularity, start, end)
        # Defensive copy — never mutate the caller's / store's frame.
        df = raw.copy(deep=True).reset_index(drop=True)

        n = len(df)
        pip_value = self._cost_params.pip_value

        # Pre-extract numpy arrays for O(1) bar access without ever exposing a
        # future row to the strategy. The strategy only ever receives a prefix
        # slice; these arrays are used only for *fill* logic on the current bar.
        times = df["time"]
        # Use the mid price if available, else bid (the store guarantees bid).
        # Entry fills are on the *open* of the entry bar.
        opens = df["open_bid"].to_numpy()
        highs = df["high_bid"].to_numpy()
        lows = df["low_bid"].to_numpy()
        closes = df["close_bid"].to_numpy()

        # --- Precompute all signals ONCE over the full frame (O(n) total).
        # PERFORMANCE / CORRECTNESS: every strategy's indicators are *causal*
        # (ewm / rolling / shift(1) / diff / pct_change — backward-looking only),
        # so the signal a strategy emits *on* bar i is a pure function of bars
        # [0..i] and is byte-identical whether the strategy is handed the
        # expanding prefix ``df.iloc[:i+1]`` (the old O(n^2) path) or the full
        # frame ``df`` (this O(n) path). ``generate_signals`` already returns the
        # signal for every bar of whatever frame it is given (at most one per
        # bar — the existing contract), so we call it a single time and index the
        # result by the bar's close timestamp. The no-look-ahead guarantee is
        # unchanged: the entry convention below still only *acts* on bar i's
        # signal at bar i (queued to enter at bar i+1's open), and a precomputed
        # signal can never carry information from a future bar because the
        # indicators that produced it cannot read forward. This equivalence is
        # not assumed — it is pinned by a regression test that replays the old
        # per-prefix loop for all six strategies and asserts identical results.
        precomputed = strategy.generate_signals(df)
        signals_by_bar: dict[datetime, Signal] = {}
        for sig in precomputed:
            # At most one signal per bar (contract); last write wins defensively,
            # mirroring the old loop's reversed-scan "most recent on this bar".
            signals_by_bar[sig.generated_at] = sig

        trades: list[Trade] = []
        position: Optional[_OpenPosition] = None
        # Pending entry: a signal emitted on bar i is entered on bar i+1's open.
        pending_signal: Optional[Signal] = None

        # Equity curve: cumulative realised net pips per bar (UTC index).
        realised_net_pips = 0.0
        equity_values: list[float] = []

        for i in range(n):
            bar_high = float(highs[i])
            bar_low = float(lows[i])
            bar_open = float(opens[i])
            bar_time = self._bar_time(times, i)

            # --- 1. Open a pending entry at THIS bar's open (no look-ahead).
            # The signal was produced from the slice ending at bar i-1, so we
            # only learn its fill price now, at bar i's open.
            if position is None and pending_signal is not None:
                position = self._open_from_signal(
                    pending_signal, bar_open, bar_time
                )
                pending_signal = None

            # --- 2. Resolve fills for an open position using ONLY this bar.
            if position is not None:
                exit_info = self._resolve_fill(position, bar_high, bar_low)
                if exit_info is not None:
                    gross_exit_level, exit_reason = exit_info
                    holding_days = self._holding_days(
                        position.entry_time, bar_time
                    )
                    trade = self._close_trade(
                        position, bar_time, gross_exit_level, exit_reason,
                        pip_value, holding_days,
                    )
                    trades.append(trade)
                    realised_net_pips += trade.pnl_net_pips
                    position = None

            # --- 3. Consume the PRECOMPUTED signal for THIS bar (if any).
            # Equivalent to the old ``generate_signals(df.iloc[:i+1])`` +
            # _signal_for_bar(bar_time) — see the precompute note above — but
            # O(1) per bar. Done last so a signal on bar i cannot be entered
            # until bar i+1. Skipped while a position is open (single-position
            # PoC) and while a signal is already pending entry, exactly as
            # before — the precompute does not change *when* a signal is acted
            # on, only *how* it is obtained.
            if position is None and pending_signal is None:
                bar_signal = signals_by_bar.get(bar_time)
                if bar_signal is not None and bar_signal.direction in (
                    Direction.LONG,
                    Direction.SHORT,
                ):
                    pending_signal = bar_signal

            equity_values.append(realised_net_pips)

        # --- Close any position still open at the end of the data at the final
        # bar's close (no dangling open trade leaking into the equity curve).
        if position is not None and n > 0:
            final_time = self._bar_time(times, n - 1)
            final_close = float(closes[n - 1])
            holding_days = self._holding_days(position.entry_time, final_time)
            trade = self._close_trade(
                position, final_time, final_close, "end_of_data", pip_value,
                holding_days,
            )
            trades.append(trade)
            realised_net_pips += trade.pnl_net_pips
            # Reflect the realised PnL on the final equity point.
            if equity_values:
                equity_values[-1] = realised_net_pips

        equity_curve = self._build_equity_curve(times, equity_values)

        # swap_modelled is True whenever financing data was supplied (either
        # rate non-zero) — the honest INV-06 label. It is False only when the
        # engine is run with both rates at 0.0 (the backward-compatible
        # spread-only path), regardless of how many trades held overnight.
        swap_modelled = (
            self._cost_params.swap_long_rate != 0.0
            or self._cost_params.swap_short_rate != 0.0
        )
        metadata = {
            "instrument": instrument,
            "granularity": granularity,
            "bar_count": n,
            "trade_count": len(trades),
            "strategy_name": strategy.name,
            "swap_modelled": swap_modelled,
            "spread_pips": self._cost_params.spread_pips,
            "slippage_pips": self._cost_params.slippage_pips,
            "pip_value": pip_value,
            "swap_long_rate": self._cost_params.swap_long_rate,
            "swap_short_rate": self._cost_params.swap_short_rate,
            "commission_pips": self._cost_params.commission_pips,
        }

        return BacktestResult(
            trades=trades, equity_curve=equity_curve, metadata=metadata
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bar_time(times: pd.Series, i: int) -> datetime:
        """UTC-aware datetime for bar ``i`` (INV-03 — sourced from bar data)."""
        ts = times.iloc[i]
        # pandas Timestamp → python datetime, preserving tz (UTC).
        result: datetime = ts.to_pydatetime()
        return result

    @staticmethod
    def _holding_days(entry_time: datetime, exit_time: datetime) -> int:
        """Calendar overnight count between entry and exit bar UTC dates.

        INV-03: both timestamps are UTC-aware datetimes sourced from bar data
        (never wall-clock). The financing-day count is the number of UTC date
        boundaries crossed — ``(exit_date - entry_date).days`` — so a position
        opened and closed within the same UTC date (an intraday / same-bar
        close) returns ``0`` and accrues no swap, while one held overnight
        returns the number of overnight rollovers. Normalising to UTC dates (not
        a wall-clock 24h delta) means an H1 trade from 23:00 to 01:00 the next
        day correctly counts as one overnight hold.
        """
        entry_date = entry_time.astimezone(timezone.utc).date()
        exit_date = exit_time.astimezone(timezone.utc).date()
        return (exit_date - entry_date).days

    def _open_from_signal(
        self, signal: Signal, entry_open: float, entry_time: datetime
    ) -> _OpenPosition:
        """Open a position at the (next) bar's open from a queued signal.

        Stop and target levels are computed from the *actual* gross entry price
        (the bar open we filled at), not the signal's reference price — the
        signal carries distances, the engine anchors them to the real fill.
        """
        if signal.direction is Direction.LONG:
            stop_price = entry_open - signal.stop_distance
            target_price = entry_open + signal.target_distance
        else:  # SHORT
            stop_price = entry_open + signal.stop_distance
            target_price = entry_open - signal.target_distance

        return _OpenPosition(
            direction=signal.direction,
            entry_time=entry_time,
            entry_price_gross=entry_open,
            stop_price=stop_price,
            target_price=target_price,
        )

    @staticmethod
    def _resolve_fill(
        position: _OpenPosition, bar_high: float, bar_low: float
    ) -> Optional[tuple[float, str]]:
        """Determine whether an open position exits on this bar.

        Returns ``(gross_exit_level, reason)`` where ``reason`` is ``"stop"`` or
        ``"target"``, or ``None`` if neither level was breached this bar.

        Conservative tie-break: if BOTH stop and target are breached within the
        same bar, the **stop wins** — we cannot know the intrabar path, so we
        assume the adverse outcome.

        The returned level is clamped to ``[bar_low, bar_high]`` so the reported
        fill price can never lie outside the bar's range.
        """
        if position.direction is Direction.LONG:
            stop_hit = bar_low <= position.stop_price
            target_hit = bar_high >= position.target_price
        else:  # SHORT
            stop_hit = bar_high >= position.stop_price
            target_hit = bar_low <= position.target_price

        if not stop_hit and not target_hit:
            return None

        # Conservative: stop takes priority when both breach the same bar.
        if stop_hit:
            level = position.stop_price
            reason = "stop"
        else:
            level = position.target_price
            reason = "target"

        # Clamp to the bar range — a fill can never be at an impossible price.
        clamped = min(max(level, bar_low), bar_high)
        return clamped, reason

    def _close_trade(
        self,
        position: _OpenPosition,
        exit_time: datetime,
        gross_exit_level: float,
        exit_reason: str,
        pip_value: float,
        holding_days: int,
    ) -> Trade:
        """Apply costs and build the completed ``Trade`` record.

        ``holding_days`` is the calendar-day count between the entry and exit
        bar UTC dates (see :meth:`_holding_days`); it drives the overnight
        financing charge (``rate × holding_days`` on the direction's side).
        """
        # Slippage applies only on stop/target (market) fills, NOT on an
        # end-of-data close at the bar's close price.
        slippage = (
            self._cost_params.slippage_pips
            if exit_reason in ("stop", "target")
            else 0.0
        )

        cost = apply_costs(
            entry_price=position.entry_price_gross,
            exit_price=gross_exit_level,
            direction=position.direction,
            spread_pips=self._cost_params.spread_pips,
            slippage_pips=slippage,
            pip_value=pip_value,
            swap_long_rate=self._cost_params.swap_long_rate,
            swap_short_rate=self._cost_params.swap_short_rate,
            holding_days=holding_days,
            commission_pips=self._cost_params.commission_pips,
        )

        gross_pips = self._pnl_pips(
            position.direction,
            position.entry_price_gross,
            gross_exit_level,
            pip_value,
        )
        net_pips = self._pnl_pips(
            position.direction, cost.net_entry, cost.net_exit, pip_value
        )

        return Trade(
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price_gross=position.entry_price_gross,
            entry_price_net=cost.net_entry,
            exit_price_gross=gross_exit_level,
            exit_price_net=cost.net_exit,
            direction=position.direction,
            pnl_pips=gross_pips,
            pnl_net_pips=net_pips,
            cost_pips=cost.total_cost_pips,
            exit_reason=exit_reason,
        )

    @staticmethod
    def _pnl_pips(
        direction: Direction,
        entry_price: float,
        exit_price: float,
        pip_value: float,
    ) -> float:
        """PnL in pips for a round trip (long: exit-entry; short: entry-exit)."""
        if direction is Direction.LONG:
            price_delta = exit_price - entry_price
        else:  # SHORT
            price_delta = entry_price - exit_price
        return price_delta / pip_value

    @staticmethod
    def _build_equity_curve(
        times: pd.Series, equity_values: list[float]
    ) -> pd.Series:
        """Build the cumulative net-pips equity curve indexed by bar time (UTC)."""
        if not equity_values:
            return pd.Series(
                [], index=pd.DatetimeIndex([], tz="UTC"), dtype="float64",
                name="equity_pips",
            )
        index = pd.DatetimeIndex(times.iloc[: len(equity_values)].to_numpy())
        return pd.Series(
            equity_values, index=index, dtype="float64", name="equity_pips"
        )
