"""Fathom admin panel — Streamlit dashboard (P4-T-05).

Launch with::

    streamlit run panel/app.py [-- --db-path PATH]

This module is the *thin view* over :mod:`panel.data` (the tested seam).
All data logic lives in :mod:`panel.data`; this file is only layout, caching,
and widget wiring.

INV-01 read-only boundary
-------------------------
This module imports ONLY ``panel.data``, ``signals.scan``,
``risk.limits.LimitsConfig``, Streamlit, and the Lightweight Charts component.
It MUST NOT import ``cli``, ``execution.orders``,
``execution.models.build_bracket``, or ``risk`` sizing/placement — directly or
transitively. A transitive-import boundary test in
``tests/test_admin_panel.py`` enforces this.

The **Refresh** button calls ``signals.scan.run_scan(...)`` — the order-free
scan entrypoint — never ``cli.cmd_scan`` (which carries the order path at
module level).

INV-03: all displayed timestamps are UTC RFC 3339 (sourced from the store
via the data layer; no local-clock reformatting here).

INV-08: no secret (OANDA token, webhook URL, API key) is rendered or logged.

library_defaults (P4-T-05 task row)
-------------------------------------
* ``st.cache_data`` TTL is set explicitly (30 s) — never the forever default.
* ``renderLightweightCharts`` is called with the attribution option enabled
  (``watermark`` / logo visible) as required by Apache-2.0 (D-P4-3).
* Wide layout enabled (``st.set_page_config(layout="wide")``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

import streamlit as st

# Lightweight Charts component (Apache-2.0 attribution required — logo ON).
from streamlit_lightweight_charts import renderLightweightCharts

# Data layer (the tested seam — read-only view models).
import panel.data as pdata
from data.store import Store
from risk.limits import LimitsConfig
from signals.scan import run_scan

# ---------------------------------------------------------------------------
# Page config — must be the first Streamlit call.
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Fathom — Admin Panel",
    page_icon="chart_with_upwards_trend",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# CLI argument parsing (Streamlit passes extra args after `--`)
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = "data/fathom.db"


def _parse_db_path() -> str:
    """Parse ``--db-path`` from the Streamlit extra-args list (after ``--``).

    When launched as ``streamlit run panel/app.py -- --db-path PATH`` Streamlit
    passes everything after ``--`` as ``sys.argv[1:]``.  We parse with a minimal
    argparse so unknown Streamlit flags do not cause a hard exit.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db-path", default=_DEFAULT_DB_PATH, dest="db_path")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return str(args.db_path)


_DB_PATH: str = _parse_db_path()

# ---------------------------------------------------------------------------
# Cached data accessors (TTL = 30 s — short enough for near-realtime refresh)
# ---------------------------------------------------------------------------

# The ``ttl`` kwarg is mandatory (library_defaults: never the forever default).
_CACHE_TTL = 30  # seconds


@st.cache_data(ttl=_CACHE_TTL)
def _load_equity_series(db_path: str) -> list[pdata.EquityPoint]:
    """Load equity series from the store (cached, 30 s TTL)."""
    store = Store(db_path)
    try:
        return pdata.equity_series(store)
    finally:
        store.close()


@st.cache_data(ttl=_CACHE_TTL)
def _load_blotter(db_path: str) -> pdata.BlotterView:
    """Load blotter view from the store (cached, 30 s TTL)."""
    store = Store(db_path)
    try:
        return pdata.blotter(store, cfg=LimitsConfig())
    finally:
        store.close()


@st.cache_data(ttl=_CACHE_TTL)
def _load_watchlist(db_path: str) -> list[Any]:
    """Load watchlist candidates from the store (cached, 30 s TTL)."""
    store = Store(db_path)
    try:
        return pdata.watchlist(store)
    finally:
        store.close()


@st.cache_data(ttl=_CACHE_TTL)
def _load_deviation_log(db_path: str) -> list[pdata.DeviationRow]:
    """Load deviation-log rows from the store (cached, 30 s TTL)."""
    store = Store(db_path)
    try:
        return pdata.deviation_log(store)
    finally:
        store.close()


@st.cache_data(ttl=_CACHE_TTL)
def _load_instruments(db_path: str) -> list[str]:
    """Load distinct instruments from the candles table (cached, 30 s TTL)."""
    store = Store(db_path)
    try:
        meta = store.load_instruments()
        if meta:
            return sorted(m.name for m in meta)
        # Fallback: look at what's in the watchlist.
        candidates = pdata.watchlist(store)
        seen: list[str] = []
        for c in candidates:
            if c.instrument not in seen:
                seen.append(c.instrument)
        return seen if seen else ["EUR_USD"]
    finally:
        store.close()


@st.cache_data(ttl=_CACHE_TTL)
def _load_chart_data(
    db_path: str,
    instrument: str,
    timeframe: str,
) -> pdata.ChartData:
    """Load chart data (candles + overlays) for one instrument/timeframe pair."""
    store = Store(db_path)
    try:
        return pdata.chart_data(store, instrument, timeframe)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Sidebar — navigation + refresh button
# ---------------------------------------------------------------------------

VIEWS = ["Charts", "Equity", "Blotter", "Watchlist", "Deviation Log"]

with st.sidebar:
    st.title("Fathom")
    st.caption("Read-only dashboard — INV-01")
    view = st.radio("View", VIEWS, index=0, label_visibility="collapsed")

    st.divider()

    st.subheader("Refresh")
    st.caption(
        "Runs `fathom scan` (order-free) and re-reads the store. "
        "No order or execution path is triggered."
    )
    if st.button("Refresh scan", use_container_width=True, type="primary"):
        with st.spinner("Running scan …"):
            try:
                candidates = run_scan(db_path=_DB_PATH, dry_run=True)
                st.success(f"Scan complete — {len(candidates)} candidate(s).")
            except Exception as exc:
                st.error(f"Scan failed: {exc}")
        # Clear all caches so next render fetches fresh data.
        st.cache_data.clear()
        st.rerun()

    st.divider()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    st.caption(f"UTC: {now_utc}")
    st.caption(f"DB: `{_DB_PATH}`")

# ---------------------------------------------------------------------------
# Helper: Lightweight Charts attribution options (Apache-2.0 requirement)
# ---------------------------------------------------------------------------

# The attribution/watermark is the Apache-2.0 compliance requirement (D-P4-3).
# We add a watermark text "TradingView Lightweight Charts™" to every chart
# rendered through the component. The component itself does not expose a
# dedicated "logo" flag in its JSON schema, but the TradingView Lightweight
# Charts API supports a ``watermark`` object on the ``chart`` options dict.
_ATTRIBUTION_WATERMARK: dict[str, Any] = {
    "visible": True,
    "fontSize": 12,
    "horzAlign": "right",
    "vertAlign": "bottom",
    "color": "rgba(171, 71, 188, 0.4)",
    "text": "TradingView Lightweight Charts™",
}

_CHART_BASE_OPTIONS: dict[str, Any] = {
    "height": 400,
    "layout": {
        "background": {"type": "solid", "color": "#131722"},
        "textColor": "#d1d4dc",
    },
    "grid": {
        "vertLines": {"color": "rgba(42, 46, 57, 0)"},
        "horzLines": {"color": "rgba(42, 46, 57, 0.6)"},
    },
    "watermark": _ATTRIBUTION_WATERMARK,
    "timeScale": {
        "borderColor": "rgba(197, 203, 206, 0.4)",
        "timeVisible": True,
        "secondsVisible": False,
    },
}


# ---------------------------------------------------------------------------
# View 1 — Charts
# ---------------------------------------------------------------------------

def _view_charts() -> None:
    """Render the Charts view: instrument/timeframe picker + candle chart."""
    st.header("Charts")

    instruments = _load_instruments(_DB_PATH)
    timeframes = ["M5", "M15", "M30", "H1", "H4", "D", "W"]

    col1, col2 = st.columns(2)
    with col1:
        instrument = st.selectbox(
            "Instrument",
            instruments,
            index=0,
            key="charts_instrument",
        )
    with col2:
        timeframe = st.selectbox(
            "Timeframe",
            timeframes,
            index=timeframes.index("H1"),
            key="charts_timeframe",
        )

    if not instrument:
        st.info("No instrument selected.")
        return

    cd = _load_chart_data(_DB_PATH, str(instrument), str(timeframe))

    if cd.candles.empty:
        st.warning(
            f"No candle data for {instrument}/{timeframe}. "
            "Try running a refresh or checking the database."
        )
    else:
        # Build candlestick series data.
        candle_data: list[dict[str, Any]] = []
        for _, row in cd.candles.iterrows():
            # The time column is datetime64[ns, UTC]; format as ISO string.
            t = row["time"]
            if hasattr(t, "isoformat"):
                ts = t.isoformat()
            else:
                ts = str(t)
            candle_data.append(
                {
                    "time": ts,
                    "open": float(row["open_mid"]) if "open_mid" in cd.candles.columns else float(row["open_bid"]),
                    "high": float(row["high_mid"]) if "high_mid" in cd.candles.columns else float(row["high_bid"]),
                    "low": float(row["low_mid"]) if "low_mid" in cd.candles.columns else float(row["low_bid"]),
                    "close": float(row["close_mid"]) if "close_mid" in cd.candles.columns else float(row["close_bid"]),
                }
            )

        candle_series: dict[str, Any] = {
            "type": "Candlestick",
            "data": candle_data,
            "options": {
                "upColor": "#26a69a",
                "downColor": "#ef5350",
                "borderVisible": False,
                "wickUpColor": "#26a69a",
                "wickDownColor": "#ef5350",
            },
        }

        series: list[dict[str, Any]] = [candle_series]

        # Add overlay lines for entry/stop/target.
        # Overlays are rendered as horizontal line series anchored at the last
        # candle time (Lightweight Charts does not natively support horizontal
        # price lines via the JSON component API, so we use a thin Line series
        # with two points spanning the visible range).
        if cd.candles.shape[0] >= 2 and cd.overlays:
            first_time = cd.candles.iloc[0]["time"]
            last_time = cd.candles.iloc[-1]["time"]

            def _ts(t: Any) -> str:
                if hasattr(t, "isoformat"):
                    return str(t.isoformat())
                return str(t)

            _overlay_colours: dict[str, dict[str, str]] = {
                "active": {
                    "entry": "#ffd700",   # gold
                    "stop": "#ef5350",    # red
                    "target": "#26a69a",  # green
                },
                "proposed": {
                    "entry": "#64b5f6",   # light blue
                    "stop": "#ff8a65",    # light orange
                    "target": "#81c784",  # light green
                },
            }

            for overlay in cd.overlays:
                colours = _overlay_colours.get(
                    overlay.label,
                    {"entry": "#ffffff", "stop": "#ff0000", "target": "#00ff00"},
                )
                label_prefix = overlay.label.upper()
                for price_name, price_value in [
                    ("entry", overlay.entry),
                    ("stop", overlay.stop),
                    ("target", overlay.target),
                ]:
                    series.append(
                        {
                            "type": "Line",
                            "data": [
                                {"time": _ts(first_time), "value": price_value},
                                {"time": _ts(last_time), "value": price_value},
                            ],
                            "options": {
                                "color": colours[price_name],
                                "lineWidth": 1,
                                "lineStyle": 2,  # dashed
                                "title": f"{label_prefix} {price_name}",
                                "lastValueVisible": True,
                                "priceLineVisible": False,
                                "crosshairMarkerVisible": False,
                            },
                        }
                    )

        chart_opts = dict(_CHART_BASE_OPTIONS)
        chart_opts["height"] = 450

        renderLightweightCharts(
            [{"chart": chart_opts, "series": series}],
            key=f"chart_{instrument}_{timeframe}",
        )

        # Overlays summary table below the chart.
        if cd.overlays:
            st.caption("Chart overlays")
            overlay_rows = [
                {
                    "Label": o.label,
                    "Entry": f"{o.entry:.5f}",
                    "Stop": f"{o.stop:.5f}",
                    "Target": f"{o.target:.5f}",
                }
                for o in cd.overlays
            ]
            st.table(overlay_rows)

    # Attribution note (Apache-2.0 compliance).
    st.caption(
        "Charts powered by [TradingView Lightweight Charts™]"
        "(https://www.tradingview.com/lightweight-charts/) — Apache 2.0 licence."
    )


# ---------------------------------------------------------------------------
# View 2 — Equity
# ---------------------------------------------------------------------------

def _view_equity() -> None:
    """Render the Equity view: equity curve + drawdown."""
    st.header("Equity Curve")

    pts = _load_equity_series(_DB_PATH)
    if not pts:
        st.info("No equity snapshots yet. Reconciliation writes one per cycle.")
        return

    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "UTC timestamp": p.as_of,
                "Equity": p.equity,
                "Day P&L": p.day_pl,
                "Drawdown": p.drawdown,
            }
            for p in pts
        ]
    )
    df["UTC timestamp"] = pd.to_datetime(df["UTC timestamp"], utc=True)
    df = df.sort_values("UTC timestamp")

    col1, col2, col3 = st.columns(3)
    with col1:
        latest_equity = pts[-1].equity
        st.metric("Current Equity", f"{latest_equity:,.2f}")
    with col2:
        latest_day_pl = pts[-1].day_pl
        st.metric(
            "Today's P&L",
            f"{latest_day_pl:+,.2f}",
            delta=f"{latest_day_pl:+,.2f}",
        )
    with col3:
        max_dd = max(p.drawdown for p in pts)
        st.metric("Max Drawdown", f"{max_dd:.2%}")

    st.subheader("Equity")
    st.line_chart(df.set_index("UTC timestamp")["Equity"])

    st.subheader("Drawdown")
    st.line_chart(df.set_index("UTC timestamp")["Drawdown"])

    with st.expander("Raw equity snapshots (newest-first)"):
        st.dataframe(
            df.sort_values("UTC timestamp", ascending=False),
            use_container_width=True,
        )


# ---------------------------------------------------------------------------
# View 3 — Blotter
# ---------------------------------------------------------------------------

def _view_blotter() -> None:
    """Render the Blotter view: open positions + P&L + risk-in-use."""
    st.header("Blotter")

    bv = _load_blotter(_DB_PATH)

    # Summary metrics row.
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Open Positions", len(bv.positions))
    with col2:
        day_pl_val = bv.day_pl if bv.day_pl is not None else 0.0
        st.metric(
            "Day P&L",
            f"{day_pl_val:+,.2f}" if bv.day_pl is not None else "N/A",
            delta=f"{day_pl_val:+,.2f}" if bv.day_pl is not None else None,
            delta_color="normal" if day_pl_val >= 0 else "inverse",
        )
    with col3:
        st.metric("Risk In Use", f"{bv.risk_in_use:,.4f}")
    with col4:
        if bv.risk_budget is not None:
            pct = (bv.risk_in_use / bv.risk_budget * 100) if bv.risk_budget > 0 else 0.0
            st.metric(
                "Risk Budget",
                f"{bv.risk_budget:,.4f}",
                delta=f"{pct:.1f}% used",
                delta_color="normal" if pct < 80 else "inverse",
            )
        else:
            st.metric("Risk Budget", "N/A")

    st.divider()

    if not bv.positions:
        st.info("No open positions.")
        return

    import pandas as pd

    rows = [
        {
            "Trade ID": p.broker_trade_id,
            "Instrument": p.instrument,
            "Units": p.units,
            "Entry": p.entry_price,
            "Stop": p.stop_loss_price,
            "Target": p.take_profit_price,
            "Unrealized P&L": p.unrealized_pl,
            "Opened (UTC)": p.opened_at,
            "Candidate Ref": p.candidate_ref,
        }
        for p in bv.positions
    ]
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)

    # Unrealized P&L bar chart.
    if len(bv.positions) > 0:
        st.subheader("Unrealized P&L by position")
        chart_df = pd.DataFrame(
            {"Trade ID": [p.broker_trade_id for p in bv.positions],
             "Unrealized P&L": [p.unrealized_pl for p in bv.positions]}
        ).set_index("Trade ID")
        st.bar_chart(chart_df)


# ---------------------------------------------------------------------------
# View 4 — Watchlist
# ---------------------------------------------------------------------------

def _view_watchlist() -> None:
    """Render the Watchlist view: latest ranked Candidate[] (INV-13)."""
    st.header("Watchlist")

    candidates = _load_watchlist(_DB_PATH)
    if not candidates:
        st.info("Watchlist is empty. Run a refresh to populate it.")
        return

    import pandas as pd

    rows = [
        {
            "Rank": c.rank,
            "Instrument": c.instrument,
            "Timeframe": c.timeframe,
            "Strategy": c.strategy_name,
            "Direction": c.direction,
            "Entry Ref": c.entry_ref,
            "Stop Dist": c.stop_distance,
            "Target Dist": c.target_distance,
            "OOS Sharpe": c.oos_sharpe_mean,
            "Quality": c.quality_score,
            "Spread OK": c.spread_ok,
            "Session OK": c.session_ok,
            "News Flag": c.news_flag,
            "Generated (UTC)": c.generated_at,
        }
        for c in candidates
    ]
    df = pd.DataFrame(rows).sort_values("Rank")
    st.dataframe(df, use_container_width=True)

    st.caption(
        f"{len(candidates)} candidate(s) — INV-13 shape, unchanged from ranker."
    )


# ---------------------------------------------------------------------------
# View 5 — Deviation Log
# ---------------------------------------------------------------------------

def _view_deviation_log() -> None:
    """Render the Deviation Log view: monitor alerts, newest-first."""
    st.header("Deviation Log")

    rows = _load_deviation_log(_DB_PATH)
    if not rows:
        st.info("No deviation events recorded.")
        return

    import pandas as pd

    # Rows are already newest-first from the data layer.
    table_rows = [
        {
            "Event ID": r.event_id,
            "Instrument": r.instrument,
            "Type": r.deviation_type,
            "Severity": r.severity,
            "Detail": r.detail,
            "Trade ID": r.broker_trade_id or "",
            "Created (UTC)": r.created_at,
            "Delivered": r.delivered,
        }
        for r in rows
    ]
    df = pd.DataFrame(table_rows)

    # Colour-code severity in summary.
    critical = sum(1 for r in rows if r.severity.upper() == "CRITICAL")
    warnings = sum(1 for r in rows if r.severity.upper() == "WARNING")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Events", len(rows))
    with col2:
        st.metric("CRITICAL", critical)
    with col3:
        st.metric("WARNING", warnings)

    st.divider()
    st.dataframe(df, use_container_width=True)


# ---------------------------------------------------------------------------
# Main render loop — dispatch to the selected view.
# ---------------------------------------------------------------------------

_VIEW_FNS = {
    "Charts": _view_charts,
    "Equity": _view_equity,
    "Blotter": _view_blotter,
    "Watchlist": _view_watchlist,
    "Deviation Log": _view_deviation_log,
}

# Guard: only execute view rendering when running inside a live Streamlit
# session (``streamlit run panel/app.py``).  This prevents crashes on bare
# imports (e.g. ``python -c "import panel.app"`` for the INV-01 boundary
# check) where no session context exists.
from streamlit.runtime import exists as _st_runtime_exists  # noqa: E402

if _st_runtime_exists():
    _VIEW_FNS[str(view)]()
