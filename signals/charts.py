"""Chart renderer for watchlist candidates — Phase 2 (P2-T-03).

Produces a static PNG for each ranked ``Candidate``, suitable for Discord
delivery via Hermes.  The chart shows recent OHLC candles plus overlays for
the proposed entry, stop-loss, and take-profit levels and a marker at the
signal bar.

Backend: ``matplotlib Agg`` (non-interactive, no display server required —
forced at module import so the module is safe under cron/Hermes with no ``$DISPLAY``).

INV-01: produces image artefacts only — no order placement.
INV-03: x-axis time labels are UTC; no naive/local times.
INV-13: reads flat ``Candidate`` fields only (no ``.signal.*`` nesting).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Force the Agg backend before any pyplot import so the module is safe under
# cron/Hermes with no display server (INV-03 / spec requirement).
import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402 (after backend selection)
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from signals.ranker import Candidate  # noqa: E402

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Default number of recent bars to include in the chart window.  Comfortably
#: readable at Discord resolution.  Callers may pass fewer via a sliced df.
DEFAULT_CANDLE_WINDOW: int = 100

#: Minimum bars required to produce a chart.  Below this we raise a clear
#: error rather than a blank/misleading image.
MIN_CANDLES: int = 1

#: Figure size for the saved PNG.
_FIG_WIDTH_IN: float = 12.0
_FIG_HEIGHT_IN: float = 6.0

#: DPI for saved PNG.
_DPI: int = 100

# ---------------------------------------------------------------------------
# Level-line styles
# ---------------------------------------------------------------------------

_ENTRY_STYLE: dict[str, object] = dict(color="#2196F3", linewidth=1.5, linestyle="--", label="entry")
_STOP_STYLE: dict[str, object] = dict(color="#F44336", linewidth=1.2, linestyle=":", label="stop")
_TARGET_STYLE: dict[str, object] = dict(color="#4CAF50", linewidth=1.2, linestyle=":", label="target")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_candidate_chart(
    candidate: Candidate,
    candles: pd.DataFrame,
    out_dir: str,
) -> str:
    """Render a candle chart for a watchlist candidate and save it as a PNG.

    Draws the recent OHLC candle window (up to ``DEFAULT_CANDLE_WINDOW`` bars),
    overlays three horizontal levels (entry, stop, target) and a signal marker
    at the ``generated_at`` bar, then saves the figure to ``out_dir``.

    Level placement per direction (INV-13 flat fields):

    * LONG  — stop  = ``entry_ref - stop_distance``  (below)
              target = ``entry_ref + target_distance`` (above)
    * SHORT — stop  = ``entry_ref + stop_distance``  (above)
              target = ``entry_ref - target_distance`` (below)

    Args:
        candidate: The ranked ``Candidate`` (flat INV-13 fields).
        candles: A ``pd.DataFrame`` in the ``load_candles`` contract —
            columns include ``time`` (``datetime64[ns, UTC]``), ``open_bid``,
            ``high_bid``, ``low_bid``, ``close_bid``.  Only the most recent
            ``DEFAULT_CANDLE_WINDOW`` rows are plotted; extra rows are silently
            trimmed.
        out_dir: Directory in which to save the PNG.  Created if it does not
            exist.

    Returns:
        Absolute path to the saved PNG file.

    Raises:
        ValueError: If ``candles`` is empty or has fewer than ``MIN_CANDLES``
            rows after trimming, or if required columns are missing.
    """
    _validate_candles(candles)

    # Trim to the most recent window (keep it readable at Discord size).
    df = candles.tail(DEFAULT_CANDLE_WINDOW).copy().reset_index(drop=True)

    # Derive level prices from flat Candidate fields (INV-13, no .signal.).
    entry = candidate.entry_ref
    is_long = candidate.direction.upper() == "LONG"
    stop = entry - candidate.stop_distance if is_long else entry + candidate.stop_distance
    target = entry + candidate.target_distance if is_long else entry - candidate.target_distance

    # Resolve the signal-bar index for the marker.
    signal_idx: Optional[int] = _find_signal_bar_index(df, candidate.generated_at)

    # Build output path (deterministic — re-render overwrites cleanly).
    out_path = _build_output_path(candidate, out_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Draw
    # -----------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(_FIG_WIDTH_IN, _FIG_HEIGHT_IN))

    try:
        _draw_candlesticks(ax, df)

        # Horizontal levels
        ax.axhline(entry, **_ENTRY_STYLE)  # type: ignore[arg-type]
        ax.axhline(stop, **_STOP_STYLE)  # type: ignore[arg-type]
        ax.axhline(target, **_TARGET_STYLE)  # type: ignore[arg-type]

        # Signal marker
        if signal_idx is not None:
            bar_time = df["time"].iloc[signal_idx]
            bar_high = df["high_bid"].iloc[signal_idx]
            ax.scatter(
                [bar_time],
                [bar_high],
                marker="^" if is_long else "v",
                color="#FF9800",
                zorder=5,
                s=80,
                label="signal",
            )

        # UTC x-axis (INV-03) — matplotlib stores dates as UTC-relative floats
        # when the Series already carries UTC timezone info; we just format them.
        ax.xaxis.set_major_formatter(
            mdates.DateFormatter("%Y-%m-%d\n%H:%M", tz=timezone.utc)  # type: ignore[no-untyped-call]
        )
        fig.autofmt_xdate(rotation=30, ha="right")
        ax.set_xlabel("Time (UTC)", fontsize=9)
        ax.set_ylabel("Price", fontsize=9)

        # Title
        title = (
            f"{candidate.instrument} · {candidate.timeframe} · "
            f"{candidate.strategy_name}\n"
            f"Direction: {candidate.direction}  |  "
            f"OOS Sharpe: {candidate.oos_sharpe_mean:.3f}  |  "
            f"Rank: {candidate.rank}"
        )
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(axis="y", alpha=0.3, linewidth=0.5)

        fig.tight_layout()
        fig.savefig(out_path, dpi=_DPI, bbox_inches="tight")

    finally:
        plt.close(fig)  # no leak across a batch

    _log.info(
        "Chart saved: %s (%d bars, direction=%s)",
        out_path,
        len(df),
        candidate.direction,
    )
    return out_path


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_candles(candles: pd.DataFrame) -> None:
    """Raise ``ValueError`` on unusable candle data (graceful degradation)."""
    required = {"time", "open_bid", "high_bid", "low_bid", "close_bid"}
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(
            f"render_candidate_chart: candles DataFrame is missing required "
            f"columns: {sorted(missing)}.  "
            f"Got columns: {list(candles.columns)}"
        )
    if len(candles) < MIN_CANDLES:
        raise ValueError(
            f"render_candidate_chart: candles has {len(candles)} row(s), "
            f"need at least {MIN_CANDLES}.  "
            "Supply more candles or catch this ValueError to skip the chart."
        )


def _find_signal_bar_index(df: pd.DataFrame, generated_at: str) -> Optional[int]:
    """Return the integer row index of the signal bar, or ``None`` if not found.

    ``generated_at`` is an RFC 3339 UTC string (INV-03).  We parse it to a
    UTC-aware Timestamp and find the closest bar by time (exact match first,
    then nearest — the signal bar may have been emitted mid-bar).
    """
    try:
        ts = pd.Timestamp(generated_at, tz="UTC")
    except Exception:
        _log.warning(
            "Could not parse generated_at=%r as a timestamp; no signal marker.",
            generated_at,
        )
        return None

    # Exact match.
    exact = df.index[df["time"] == ts].tolist()
    if exact:
        return int(exact[0])

    # Nearest bar (signal may fall after the last stored bar, or between bars).
    times = df["time"].values  # numpy array of ns-since-epoch UTC
    ts_ns = ts.value  # ns since epoch (int)
    diffs = abs(times.astype("int64") - ts_ns)
    nearest_idx = int(diffs.argmin())

    # Only annotate if the nearest bar is within ±2 * median bar spacing.
    if len(df) >= 2:
        bar_spacing = abs(
            df["time"].iloc[1].value - df["time"].iloc[0].value
        )
        if diffs[nearest_idx] <= 2 * bar_spacing:
            return nearest_idx

    # The signal falls outside the plotted window.
    return None


def _build_output_path(candidate: Candidate, out_dir: str) -> str:
    """Return a deterministic, collision-resistant path for the PNG.

    Pattern: ``{out_dir}/{instrument}_{timeframe}_{run_ts_iso}.png``

    ``generated_at`` is used as the run timestamp (UTC RFC 3339 string) with
    colons and periods replaced by hyphens for filesystem safety.  Re-rendering
    the same candidate overwrites the same file (idempotent).
    """
    safe_ts = (
        candidate.generated_at
        .replace(":", "-")
        .replace(".", "-")
        .rstrip("Z")
        .replace("T", "_")
    )
    filename = f"{candidate.instrument}_{candidate.timeframe}_{safe_ts}.png"
    return str(Path(out_dir) / filename)


def _draw_candlesticks(ax: plt.Axes, df: pd.DataFrame) -> None:  # type: ignore[name-defined]
    """Draw OHLC candlesticks using matplotlib primitives (no extra deps).

    Each bar is rendered as:
    - A thin vertical line (wick) from ``low_bid`` to ``high_bid``.
    - A thicker rectangle (body) from ``open_bid`` to ``close_bid``,
      coloured green (bullish) or red (bearish).

    Args:
        ax: The matplotlib ``Axes`` to draw on.
        df: The candle DataFrame, already trimmed to the plot window.
    """
    if df.empty:
        return

    times = df["time"].dt.to_pydatetime()  # list[datetime] — matplotlib-friendly

    for i, (t, row) in enumerate(zip(times, df.itertuples(index=False))):
        o = row.open_bid
        h = row.high_bid
        lo = row.low_bid
        c = row.close_bid
        color = "#26a69a" if c >= o else "#ef5350"  # teal / red

        # Wick
        ax.plot([t, t], [lo, h], color=color, linewidth=0.7, zorder=2)

        # Body — use a bar width proportional to the typical bar spacing.
        # We use vlines for the body (open → close) as it avoids Rectangle
        # width calculations that depend on the axis scale.
        body_lo = min(o, c)
        body_hi = max(o, c)
        ax.vlines(t, body_lo, body_hi, linewidth=4, color=color, zorder=3)
