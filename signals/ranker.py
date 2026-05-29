"""Signal ranker — the deterministic core of Phase 2 (P2-T-01).

Turns "all approved strategies evaluated against current data" into a short,
ranked, filtered list of trade **candidates**.  This module owns two
load-bearing contracts that the whole of Phase 2 builds on:

* **INV-13 — the ``Candidate`` wire contract.**  ``Candidate`` is a flat
  (non-nested) pydantic model.  Its field names (snake_case), types, and shape
  are frozen: a ``fathom watchlist`` JSON response is always a JSON array of
  ``Candidate`` objects serialised by this model, and the Hermes job, charts,
  narration, and the portfolio layer all build against this exact shape.  A
  serialisation round-trip test pins the shape (see ``tests/test_ranker.py``).

* **INV-10 — the approved-set gate.**  ``Ranker`` only emits candidates for
  (strategy, pair, timeframe) combinations present in
  ``store.load_approved_set()``.  An **empty** approved-set means **no signals**
  (not all signals): ``rank()`` returns ``[]`` and logs it.

Pipeline (each stage a pure function, unit-testable in isolation):

    gate → evaluate → filter (spread/session) → news → conflict → rank

INV-03: every timestamp is UTC RFC 3339 (sourced from the ``Signal`` bar's
    close time, never ``datetime.now()`` for candidate content).
INV-11: consumes ``Signal``s whose ATR(14)-derived stops make cross-strategy
    ``oos_sharpe_mean`` comparable.
INV-01: produces a watchlist of *candidates only* — never sizes or places an
    order.  There is deliberately no import of ``execution`` or ``risk`` here.

Gate join (DRIFT-01 resolution).  The approved-set rows key the dimension as
    ``row['granularity']``; ``Signal`` calls it ``timeframe``.  They are the
    **same dimension**.  The gate match is::

        signal.instrument    == row['instrument']
        signal.strategy_name == row['strategy_name']
        signal.timeframe     == row['granularity']

    The ``Candidate`` exposes it as ``timeframe`` (human-facing, matching
    ``Signal``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

import pandas as pd
from pydantic import BaseModel

from strategies.base import Direction, Signal, Strategy

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (D-P2 worker-Plan level; see docs/features/signal-ranker.md)
# ---------------------------------------------------------------------------

#: News-gate look-ahead windows (spec proposal, confirmed in Plan).  A
#: high-impact event for either leg-currency within ``NEWS_WINDOW_HIGH`` of
#: ``now`` drops the candidate; a medium-impact event within
#: ``NEWS_WINDOW_MEDIUM`` flags it (``news_flag=True``).
NEWS_WINDOW_HIGH: timedelta = timedelta(hours=4)
NEWS_WINDOW_MEDIUM: timedelta = timedelta(hours=1)

#: How many of the most recent cached bars to load when evaluating a combo's
#: strategy.  Comfortably exceeds the longest indicator warm-up of the shipped
#: strategies (Donchian 55, MA 100, ROC filter windows) so the latest bar can
#: produce a signal.  Pure read window — no look-ahead implication (the
#: strategies are themselves look-ahead-free).
EVAL_LOOKBACK_BARS: int = 400


# ---------------------------------------------------------------------------
# The Candidate wire contract — FROZEN (INV-13)
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """The frozen Hermes-facing wire contract (INV-13).

    Flat, snake_case, no nested ``signal`` object — the relevant ``Signal``
    fields are flattened so the ``fathom watchlist`` JSON is flat for
    Hermes/Discord.  Field names, types, and shape are frozen; a change is a
    breaking change to the Hermes integration and must be treated as an
    amendment to INV-13.

    Fields (exactly the INV-13 table):

    instrument      : OANDA instrument identifier, e.g. ``"EUR_USD"``.
    timeframe       : granularity string from ``Signal.timeframe`` — the **same
                      dimension** the approved-set/DB calls ``granularity``.
    strategy_name   : the strategy that produced the signal (== approved-set row).
    direction       : ``"LONG"`` | ``"SHORT"``.
    entry_ref       : reference entry price (from ``Signal``).
    stop_distance   : ATR(14)-derived stop distance (from ``Signal``, INV-11).
    target_distance : RR-multiple target distance (from ``Signal``, INV-11).
    oos_sharpe_mean : validated expectancy from the approved-set row — the
                      **primary** rank key.
    quality_score   : current signal strength in [0,1] (from ``Signal``) — the
                      **tie-break only** rank key.
    rank            : 1-based position after sorting.
    spread_ok       : passed the spread filter.
    session_ok      : passed the session-liquidity filter.
    news_flag       : medium-impact event nearby (high-impact ⇒ dropped, never
                      flagged).
    generated_at    : UTC RFC-3339 string — the signal bar's close time (INV-03).
    """

    instrument: str
    timeframe: str
    strategy_name: str
    direction: str
    entry_ref: float
    stop_distance: float
    target_distance: float
    oos_sharpe_mean: float
    quality_score: float
    rank: int
    spread_ok: bool
    session_ok: bool
    news_flag: bool
    generated_at: str


# ---------------------------------------------------------------------------
# Injectable filter hooks (spread / session)
# ---------------------------------------------------------------------------
# The spread- and session-liquidity checks depend on data sources that are not
# the focus of this task (live tick spread, broker session schedule).  They are
# injected so the ranker is fully testable with mocks and so a later task can
# wire the real sources without touching the pipeline.  Defaults are
# permissive-but-honest: with no live spread feed available we cannot prove a
# spread breach, so the default passes (the Hermes Claude layer is the finer
# veto on survivors).  Both hooks must return a bool.


class SpreadCheck(Protocol):
    """``(instrument, timeframe, now) -> bool``; True ⇒ spread acceptable."""

    def __call__(self, instrument: str, timeframe: str, now: datetime) -> bool:
        ...


class SessionCheck(Protocol):
    """``(instrument, timeframe, now) -> bool``; True ⇒ liquid session."""

    def __call__(self, instrument: str, timeframe: str, now: datetime) -> bool:
        ...


def _default_spread_ok(instrument: str, timeframe: str, now: datetime) -> bool:
    """Default spread check — no live spread feed wired here, so pass.

    A real implementation derives the threshold from
    ``InstrumentMeta.typical_spread × k`` and compares the current spread.  In
    the absence of a live spread source we cannot demonstrate a breach, so we
    do not fabricate one (INV: never invent a filter result).
    """
    return True


def _default_session_ok(instrument: str, timeframe: str, now: datetime) -> bool:
    """Default session check — pass (no session schedule wired here)."""
    return True


# ---------------------------------------------------------------------------
# Store / calendar Protocols (structural — keeps the ranker decoupled + mockable)
# ---------------------------------------------------------------------------


class _StoreLike(Protocol):
    def load_approved_set(
        self, run_timestamp: Optional[datetime] = None
    ) -> list[dict[str, object]]:
        ...

    def load_candles(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        ...


class _CalendarLike(Protocol):
    def upcoming_events(
        self, currencies: list[str], window: timedelta
    ) -> list[object]:
        ...


# Strategy registry: strategy_name → Strategy instance, for one combo.  The
# approved-set stores the strategy's ``name`` (which already encodes its params,
# e.g. ``"macrossover_10_50_eur_usd_h1"``), so we cannot rebuild the exact
# instance from the registry-key grid alone.  Instead the builder is injected;
# the default reuses the runner's ``_build_strategy`` keyed by the strategy-name
# prefix.  Tests inject a trivial builder returning a stub Strategy.
StrategyBuilder = Callable[[str, str, str], Strategy]
"""``(strategy_name, instrument, timeframe) -> Strategy``."""


def _split_currencies(instrument: str) -> list[str]:
    """Split an OANDA instrument (``"EUR_USD"``) into its leg currencies.

    Returns ``[base, quote]``; for a malformed identifier returns whatever
    splitting on ``_`` yields (defensive — never raises).
    """
    parts = instrument.split("_")
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# Internal evaluated-row carrier (pre-Candidate)
# ---------------------------------------------------------------------------


class _Scored(BaseModel):
    """A surviving signal paired with its approved-set expectancy + filter flags.

    Intermediate carrier between pipeline stages; never serialised externally.
    """

    instrument: str
    timeframe: str
    strategy_name: str
    direction: Direction
    entry_ref: float
    stop_distance: float
    target_distance: float
    oos_sharpe_mean: float
    quality_score: float
    spread_ok: bool
    session_ok: bool
    news_flag: bool
    generated_at: datetime

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Ranker
# ---------------------------------------------------------------------------


class Ranker:
    """Rank approved strategy signals into a filtered ``Candidate`` watchlist.

    Composes the candle store (``load_approved_set`` + ``load_candles``), a
    strategy registry (``strategy_name → Strategy``), and the economic calendar
    (``upcoming_events``).  The pipeline is gate → evaluate → filter → news →
    conflict → rank.

    Args:
        store: anything exposing ``load_approved_set`` and ``load_candles``.
        calendar: anything exposing ``upcoming_events(currencies, window)``.
        strategy_builder: ``(strategy_name, instrument, timeframe) -> Strategy``.
            Defaults to the runner-derived builder (see ``_default_builder``).
        spread_ok: injectable spread check (default passes).
        session_ok: injectable session check (default passes).
        eval_lookback_bars: how many recent bars to load per combo.
    """

    def __init__(
        self,
        store: _StoreLike,
        calendar: _CalendarLike,
        *,
        strategy_builder: Optional[StrategyBuilder] = None,
        spread_ok: SpreadCheck = _default_spread_ok,
        session_ok: SessionCheck = _default_session_ok,
        eval_lookback_bars: int = EVAL_LOOKBACK_BARS,
    ) -> None:
        self._store = store
        self._calendar = calendar
        self._build_strategy = strategy_builder or _default_builder
        self._spread_ok = spread_ok
        self._session_ok = session_ok
        self._eval_lookback_bars = eval_lookback_bars

    # -- public API ----------------------------------------------------------

    def rank(self, now: datetime) -> list[Candidate]:
        """Run the full pipeline and return a ranked ``Candidate`` list.

        Args:
            now: UTC-aware "current time" — the reference for the news-gate
                window.  Must be UTC-aware (INV-03).

        Returns:
            A ranked list of ``Candidate`` (1-based ``rank``).  Empty when the
            approved-set is empty (INV-10) or when nothing survives filtering.

        Raises:
            ValueError: if ``now`` is not UTC-aware (INV-03).
        """
        if now.tzinfo is None:
            raise ValueError("rank(now): now must be UTC-aware (INV-03).")

        # 1. Gate (INV-10) -----------------------------------------------------
        approved = self._gate()
        if not approved:
            _log.info(
                "Approved-set is empty — INV-10: no signals (not all signals). "
                "Returning [] candidates."
            )
            return []

        # 2. Evaluate ----------------------------------------------------------
        scored = self._evaluate(approved, now)

        # 3. Filter (spread / session) ----------------------------------------
        scored = self._filter(scored, now)

        # 4. News gate ---------------------------------------------------------
        scored = self._news_gate(scored)

        # 5. Conflict (D-P2-1) -------------------------------------------------
        scored = self._resolve_conflicts(scored)

        # 6. Rank --------------------------------------------------------------
        return self._rank(scored)

    # -- stage 1: gate -------------------------------------------------------

    def _gate(self) -> list[dict[str, object]]:
        """Load the approved-set (INV-10).  Empty list ⇒ no signals."""
        return self._store.load_approved_set()

    # -- stage 2: evaluate ---------------------------------------------------

    def _evaluate(
        self, approved: list[dict[str, object]], now: datetime
    ) -> list[_Scored]:
        """Run each approved combo's strategy; take the most-recent bar's Signal.

        The gate join (DRIFT-01) is the matching that produced ``approved``: a
        candidate is only created for a (strategy, instrument, timeframe) combo
        present as a row, and the produced ``Signal`` is matched back to its row
        on ``signal.instrument == row['instrument'] AND
        signal.strategy_name == row['strategy_name'] AND
        signal.timeframe == row['granularity']`` so the row's ``oos_sharpe_mean``
        attaches to the right signal.
        """
        scored: list[_Scored] = []
        # Look-back window: load the most recent cached candles.  We do not
        # know the exact bar cadence, so we load by a generous time span and
        # take the tail; load_candles requires UTC-aware bounds.
        start = now - _lookback_span(self._eval_lookback_bars)

        for row in approved:
            instrument = str(row["instrument"])
            strategy_name = str(row["strategy_name"])
            granularity = str(row["granularity"])  # DB dimension name
            oos_sharpe_mean = float(row["oos_sharpe_mean"])  # type: ignore[arg-type]

            df = self._store.load_candles(instrument, granularity, start, now)
            if df is None or df.empty:
                continue

            strategy = self._build_strategy(strategy_name, instrument, granularity)
            signals = strategy.generate_signals(df)
            if not signals:
                continue

            # Most recent bar's signal — the last by generated_at.
            signal = max(signals, key=lambda s: s.generated_at)

            # Gate join (DRIFT-01): granularity (DB) ↔ timeframe (Signal) are the
            # SAME dimension.  Confirm the produced signal matches its row on all
            # three keys before attaching the row's validated expectancy.
            if not (
                signal.instrument == instrument
                and signal.strategy_name == strategy_name
                and signal.timeframe == granularity
            ):
                _log.warning(
                    "Gate-join mismatch: approved row (%s, %s, %s) but signal "
                    "(%s, %s, %s) — skipping (DRIFT-01).",
                    strategy_name,
                    instrument,
                    granularity,
                    signal.strategy_name,
                    signal.instrument,
                    signal.timeframe,
                )
                continue

            if signal.direction is Direction.FLAT:
                # FLAT is not a tradeable candidate.
                continue

            scored.append(
                _Scored(
                    instrument=instrument,
                    timeframe=signal.timeframe,
                    strategy_name=strategy_name,
                    direction=signal.direction,
                    entry_ref=signal.entry_ref,
                    stop_distance=signal.stop_distance,
                    target_distance=signal.target_distance,
                    oos_sharpe_mean=oos_sharpe_mean,
                    quality_score=signal.quality_score,
                    spread_ok=True,
                    session_ok=True,
                    news_flag=False,
                    generated_at=signal.generated_at,
                )
            )
        return scored

    # -- stage 3: filter (spread / session) ----------------------------------

    def _filter(self, scored: list[_Scored], now: datetime) -> list[_Scored]:
        """Drop candidates failing the spread or session-liquidity check."""
        survivors: list[_Scored] = []
        for c in scored:
            spread_ok = self._spread_ok(c.instrument, c.timeframe, now)
            session_ok = self._session_ok(c.instrument, c.timeframe, now)
            if not spread_ok:
                _log.info(
                    "Dropping %s/%s/%s: spread_ok=False.",
                    c.strategy_name,
                    c.instrument,
                    c.timeframe,
                )
                continue
            if not session_ok:
                _log.info(
                    "Dropping %s/%s/%s: session_ok=False.",
                    c.strategy_name,
                    c.instrument,
                    c.timeframe,
                )
                continue
            c.spread_ok = True
            c.session_ok = True
            survivors.append(c)
        return survivors

    # -- stage 4: news gate --------------------------------------------------

    def _news_gate(self, scored: list[_Scored]) -> list[_Scored]:
        """Drop high-impact-in-window; flag medium; clear low/none.

        For each candidate, query the calendar for either leg-currency.  A
        high-impact event within ``NEWS_WINDOW_HIGH`` ⇒ drop (hard pre-filter).
        A medium-impact event within ``NEWS_WINDOW_MEDIUM`` ⇒ keep with
        ``news_flag=True``.  Otherwise ``news_flag=False``.
        """
        # Import here to avoid a hard import dependency on the calendar module's
        # enum at module load (keeps the ranker importable in mock-only tests).
        from data.calendar import Impact

        survivors: list[_Scored] = []
        for c in scored:
            currencies = _split_currencies(c.instrument)

            high_events = self._calendar.upcoming_events(
                currencies, NEWS_WINDOW_HIGH
            )
            if any(getattr(e, "impact", None) is Impact.high for e in high_events):
                _log.info(
                    "Dropping %s/%s/%s: high-impact news within %s.",
                    c.strategy_name,
                    c.instrument,
                    c.timeframe,
                    NEWS_WINDOW_HIGH,
                )
                continue

            medium_events = self._calendar.upcoming_events(
                currencies, NEWS_WINDOW_MEDIUM
            )
            c.news_flag = any(
                getattr(e, "impact", None) is Impact.medium for e in medium_events
            )
            survivors.append(c)
        return survivors

    # -- stage 5: conflict (D-P2-1) ------------------------------------------

    def _resolve_conflicts(self, scored: list[_Scored]) -> list[_Scored]:
        """Suppress BOTH legs of a same-(instrument, timeframe) opposite-direction.

        Different timeframes are independent (no conflict).  Per D-P2-1 (lead
        ruling): if any two surviving candidates share (instrument, timeframe)
        but disagree on direction, every candidate in that group is suppressed —
        conservative / bounded-downside.
        """
        groups: dict[tuple[str, str], list[_Scored]] = {}
        for c in scored:
            groups.setdefault((c.instrument, c.timeframe), []).append(c)

        survivors: list[_Scored] = []
        for (instrument, timeframe), members in groups.items():
            directions = {m.direction for m in members}
            if Direction.LONG in directions and Direction.SHORT in directions:
                _log.info(
                    "Conflict on (%s, %s): opposite directions — suppressing "
                    "all %d (D-P2-1).",
                    instrument,
                    timeframe,
                    len(members),
                )
                continue
            survivors.extend(members)
        return survivors

    # -- stage 6: rank -------------------------------------------------------

    def _rank(self, scored: list[_Scored]) -> list[Candidate]:
        """Sort by oos_sharpe_mean desc, quality_score desc; assign 1-based rank.

        Final stable tie-break is (instrument, strategy_name) ascending so the
        order is fully deterministic regardless of input order.
        """
        ordered = sorted(
            scored,
            key=lambda c: (
                -c.oos_sharpe_mean,
                -c.quality_score,
                c.instrument,
                c.strategy_name,
            ),
        )
        return [
            Candidate(
                instrument=c.instrument,
                timeframe=c.timeframe,
                strategy_name=c.strategy_name,
                direction=c.direction.value,
                entry_ref=c.entry_ref,
                stop_distance=c.stop_distance,
                target_distance=c.target_distance,
                oos_sharpe_mean=c.oos_sharpe_mean,
                quality_score=c.quality_score,
                rank=i,
                spread_ok=c.spread_ok,
                session_ok=c.session_ok,
                news_flag=c.news_flag,
                generated_at=_to_rfc3339(c.generated_at),
            )
            for i, c in enumerate(ordered, start=1)
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_rfc3339(dt: datetime) -> str:
    """Format a UTC-aware datetime as an RFC 3339 ``...Z`` string (INV-03).

    The input is the ``Signal.generated_at`` bar-close time, which the ``Signal``
    validator already guarantees to be UTC-aware.  Convert to UTC defensively
    in case the bar carried a non-UTC tzinfo.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookback_span(bars: int) -> timedelta:
    """A generous time span guaranteed to cover ``bars`` of any cadence.

    We do not know the combo's bar cadence at the store boundary, so we load a
    wide window (``bars`` days) and let ``generate_signals`` use whatever is
    present.  This over-loads for sub-daily timeframes — harmless: the strategy
    only emits at most one signal per bar and we take the most recent.
    """
    return timedelta(days=max(1, bars))


def _default_builder(strategy_name: str, instrument: str, timeframe: str) -> Strategy:
    """Build a ``Strategy`` instance from an approved-set ``strategy_name``.

    Mirrors the runner's ``_build_strategy`` registry.  The approved-set stores
    the strategy's ``name`` (which encodes its tuning params); we route on the
    leading registry key (``macrossover``, ``donchian``, ``bollinger``, ``rsi``,
    ``roc``, ``session``) and build with that strategy's documented defaults for
    the given (instrument, timeframe).

    A name whose prefix is not a known key raises ``KeyError`` — an approved-set
    row that does not map to a buildable strategy is a programming/data error,
    not a silently-skipped candidate.
    """
    # Lazy imports: keep ``signals.ranker`` importable in mock-only tests that
    # inject their own builder and never touch the concrete strategies.
    from strategies.breakout import SessionRangeBreakout
    from strategies.mean_reversion import BollingerReversion, RSIReversion
    from strategies.momentum import ROCMomentum
    from strategies.trend import DonchianBreakout, MACrossover

    key = strategy_name.split("_", 1)[0].lower()
    if key.startswith("macrossover"):
        return MACrossover(
            fast_period=10, slow_period=50, instrument=instrument, timeframe=timeframe
        )
    if key.startswith("donchian"):
        return DonchianBreakout(
            channel_period=20, instrument=instrument, timeframe=timeframe
        )
    if key.startswith("bollinger"):
        return BollingerReversion(
            period=20, num_std=2.0, instrument=instrument, timeframe=timeframe
        )
    if key.startswith("rsi"):
        return RSIReversion(
            period=14,
            oversold=30.0,
            overbought=70.0,
            instrument=instrument,
            timeframe=timeframe,
        )
    if key.startswith("roc"):
        return ROCMomentum(
            instrument=instrument,
            timeframe=timeframe,
            roc_period=10,
            roc_threshold=0.5,
            atr_filter_period=14,
        )
    if key.startswith("session"):
        return SessionRangeBreakout(
            range_lookback=20, instrument=instrument, timeframe=timeframe
        )
    raise KeyError(
        f"No strategy registry entry for approved-set strategy_name "
        f"{strategy_name!r} (prefix {key!r})."
    )
