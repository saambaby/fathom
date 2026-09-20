"""On-demand analyze orchestration (phase-07) — order-free (INV-01).

``run_analysis`` scans, computes session context over the full watchlist,
runs per-candidate news-risk (INV-02), narrates survivors, and appends
``analysis_log`` rows. It never imports ``execution.*``, ``risk.*``, or ``cli``.

Phase-09 will insert ``record_news_risk_verdict`` immediately after
``news_risk_check`` returns and before ``narrate``. This module does not
write ``veto_ledger`` (INV-09 Phase-9 skip).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

from ai.brief import RegimeTag, SessionAnalysis, session_analysis
from ai.llm_client import MODEL as _DEFAULT_LLM_MODEL
from ai.llm_client import _ClientAdapter
from ai.narration import narrate
from ai.news_risk import NewsRiskVerdict, news_risk_check
from data.calendar import CalendarEvent, FairEconomyCalendar
from data.store import Store
from signals.ranker import Candidate
from signals.scan import run_scan
from signals.timeframes import TIMEFRAME_BAR_LENGTH

_log = logging.getLogger(__name__)

_EMPTY_CALENDAR = "(no calendar events in window)"
_SESSION_CALENDAR_WINDOW = timedelta(hours=24)
_DEFAULT_HISTORY_YEARS = 3


class CandidateAnalysis(BaseModel):
    """Per-candidate annotation produced by ``run_analysis`` (internal)."""

    candidate: Candidate
    verdict: NewsRiskVerdict
    narration: str | None
    narration_source: Literal["model", "fallback", "none"]
    regime: RegimeTag


class AnalysisResult(BaseModel):
    """Library return for ``run_analysis`` — not a wire contract."""

    session: SessionAnalysis | None = None
    survivors: list[CandidateAnalysis] = []
    vetoed: list[CandidateAnalysis] = []
    run_ts: str
    watchlist_ts: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instrument_currencies(instrument: str) -> list[str]:
    parts = instrument.split("_")
    return [p for p in parts if p]


def _render_events(events: list[CalendarEvent]) -> str:
    if not events:
        return _EMPTY_CALENDAR
    lines: list[str] = []
    for event in events:
        time_s = _rfc3339(event.time)
        lines.append(
            f"{time_s} · {event.currency} · {event.impact} · {event.event_name}"
        )
    return "\n".join(lines)


def _bar_length(timeframe: str) -> timedelta:
    return TIMEFRAME_BAR_LENGTH.get(timeframe, timedelta(hours=1))


def _next_bar_open(now: datetime, bar: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = (now - epoch).total_seconds()
    bar_s = bar.total_seconds()
    remainder = elapsed % bar_s
    if remainder == 0:
        return now + bar
    return now + timedelta(seconds=bar_s - remainder)


def _entry_window_utc(timeframe: str, now: datetime) -> tuple[str, timedelta]:
    bar = _bar_length(timeframe)
    start = _next_bar_open(now, bar)
    end = start + bar
    look_ahead = end - now
    if look_ahead <= timedelta(0):
        look_ahead = bar
    return f"{_rfc3339(start)} → {_rfc3339(end)}", look_ahead


def _model_id(client: _ClientAdapter | None) -> str:
    if client is None and not os.environ.get("LLM_API_KEY"):
        return "offline"
    env_model = os.environ.get("LLM_MODEL")
    if env_model:
        return env_model
    model = getattr(client, "model", None)
    if isinstance(model, str) and model:
        return model
    return _DEFAULT_LLM_MODEL


def _candidates_summary(candidates: list[Candidate]) -> str:
    lines = []
    for c in candidates:
        lines.append(
            f"{c.rank}. {c.instrument} {c.timeframe} {c.strategy_name} {c.direction}"
        )
    return "\n".join(lines)


def _market_stats(store: Store, candidates: list[Candidate], now: datetime) -> str:
    """Plain-text stats: last close / ATR(14) / position-in-range / vol vs median."""
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    start = now - timedelta(days=40)
    for candidate in candidates:
        key = (candidate.instrument, candidate.timeframe)
        if key in seen:
            continue
        seen.add(key)
        try:
            df = store.load_candles(
                candidate.instrument, candidate.timeframe, start, now
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("market_stats: candle load failed (%s)", type(exc).__name__)
            lines.append(f"{candidate.instrument}: candles unavailable")
            continue
        if df is None or df.empty:
            lines.append(f"{candidate.instrument}: candles unavailable")
            continue
        close = float(df["close_bid"].iloc[-1])
        high = float(df["high_bid"].max())
        low = float(df["low_bid"].min())
        span = high - low
        pos = (close - low) / span if span else 0.0
        atr_val = 0.0
        try:
            from strategies._indicators import atr as atr_fn

            series = atr_fn(df, period=14)
            if series is not None and len(series.dropna()) > 0:
                atr_val = float(series.dropna().iloc[-1])
        except Exception:  # noqa: BLE001
            atr_val = 0.0
        rets = df["close_bid"].astype(float).pct_change().dropna()
        recent_vol = float(rets.tail(24).std()) if len(rets) else 0.0
        rolling = rets.rolling(24).std().dropna() if len(rets) else rets
        median_vol = float(rolling.tail(30).median()) if len(rolling) else 0.0
        vol_vs = (recent_vol / median_vol) if median_vol else 0.0
        lines.append(
            f"{candidate.instrument} close={close:.5f} ATR(14)={atr_val:.5f} "
            f"pos_in_range={pos:.3f} rvol_vs_30d_median={vol_vs:.3f}"
        )
    return "\n".join(lines) if lines else "(no market stats)"


def _regime_for(
    session: SessionAnalysis, instrument: str
) -> RegimeTag:
    tag = session.regimes.get(instrument)
    if tag is None:
        return "unavailable"
    return tag


def _unique_run_ts(store: Store, now: datetime) -> str:
    """Second-resolution RFC-3339 that does not collide with an existing run."""
    proposed = now.replace(microsecond=0)
    stamp = _rfc3339(proposed)
    existing = {
        str(row[0])
        for row in store._conn.execute("SELECT DISTINCT run_ts FROM analysis_log")
    }
    while stamp in existing:
        proposed = proposed + timedelta(seconds=1)
        stamp = _rfc3339(proposed)
    return stamp


def run_analysis(
    *,
    db_path: str,
    instruments: str = "ALL",
    timeframes: str = "H1,H4,D",
    history_years: int = _DEFAULT_HISTORY_YEARS,
    dry_run: bool = False,
    client: _ClientAdapter | None = None,
) -> AnalysisResult:
    """Scan then annotate. Empty watchlist → no LLM calls, no analysis_log rows."""
    now = _utc_now()
    candidates = run_scan(
        db_path=db_path,
        instruments=instruments,
        timeframes=timeframes,
        history_years=history_years,
        dry_run=dry_run,
    )
    if not candidates:
        return AnalysisResult(
            session=None, run_ts=_rfc3339(now.replace(microsecond=0)), watchlist_ts=None
        )

    store = Store(db_path)
    calendar = FairEconomyCalendar(db_path=db_path)
    try:
        run_ts = _unique_run_ts(store, now)
        watchlist_ts = store.latest_watchlist_run_ts() or run_ts
        currencies: list[str] = []
        seen_ccy: set[str] = set()
        for cand in candidates:
            for ccy in _instrument_currencies(cand.instrument):
                if ccy not in seen_ccy:
                    seen_ccy.add(ccy)
                    currencies.append(ccy)

        session_events = calendar.upcoming_events(
            currencies, _SESSION_CALENDAR_WINDOW
        )
        session = session_analysis(
            _candidates_summary(candidates),
            _render_events(session_events),
            _market_stats(store, candidates, now),
            client=client,
        )

        model_id = _model_id(client)
        log_rows: list[dict[str, object]] = []
        survivors: list[CandidateAnalysis] = []
        vetoed: list[CandidateAnalysis] = []

        for candidate in candidates:
            entry_window, look_ahead = _entry_window_utc(candidate.timeframe, now)
            events = calendar.upcoming_events(
                _instrument_currencies(candidate.instrument),
                look_ahead,
            )
            rendered = _render_events(events)
            verdict = news_risk_check(
                candidate,
                rendered,
                entry_window,
                client=client,
            )
            # phase-09 insertion point: record_news_risk_verdict(...) here,
            # before narrate. Skipped in phase-07 (INV-09: do not write
            # veto_ledger, including when settings.env == "live").

            regime = _regime_for(session, candidate.instrument)
            if verdict.suggest_action == "skip":
                analysis = CandidateAnalysis(
                    candidate=candidate,
                    verdict=verdict,
                    narration=None,
                    narration_source="none",
                    regime=regime,
                )
                vetoed.append(analysis)
                narration_text: str | None = None
                narration_source: str = "none"
            else:
                narration = narrate(candidate, client=client)
                analysis = CandidateAnalysis(
                    candidate=candidate,
                    verdict=verdict,
                    narration=narration.text,
                    narration_source=narration.source,
                    regime=regime,
                )
                survivors.append(analysis)
                narration_text = narration.text
                narration_source = narration.source

            log_rows.append(
                {
                    "run_ts": run_ts,
                    "watchlist_ts": watchlist_ts,
                    "instrument": candidate.instrument,
                    "timeframe": candidate.timeframe,
                    "strategy_name": candidate.strategy_name,
                    "event_risk": verdict.event_risk,
                    "suggest_action": verdict.suggest_action,
                    "reason": verdict.reason,
                    "narration": narration_text,
                    "narration_source": narration_source,
                    "regime": regime,
                    "model_id": model_id,
                }
            )

        store.append_analysis(log_rows)
        return AnalysisResult(
            session=session,
            survivors=survivors,
            vetoed=vetoed,
            run_ts=run_ts,
            watchlist_ts=watchlist_ts,
        )
    finally:
        store.close()
        try:
            calendar._conn.close()
        except Exception:  # noqa: BLE001
            pass
