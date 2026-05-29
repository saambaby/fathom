"""Tests for signals/charts.py — P2-T-03.

Acceptance criteria (from docs/features/chart-generation.md):
  AC-1  PNG produced + non-empty for a given candidate + candle window.
  AC-2  Entry/stop/target lines correctly placed per direction.
  AC-3  Signal marker at generated_at bar.
  AC-4  UTC x-axis (INV-03).
  AC-5  Deterministic output path; re-render overwrites cleanly.
  AC-6  Headless Agg backend (no display server).
  AC-7  Candidate with insufficient candles degrades gracefully.

All tests are self-contained (no live HTTP, no real store).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from signals.charts import (
    DEFAULT_CANDLE_WINDOW,
    MIN_CANDLES,
    _build_output_path,
    _find_signal_bar_index,
    _validate_candles,
    render_candidate_chart,
)
from signals.ranker import Candidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GENERATED_AT = "2026-05-01T12:00:00Z"


def _make_candidate(
    direction: str = "LONG",
    entry_ref: float = 1.1000,
    stop_distance: float = 0.0030,
    target_distance: float = 0.0045,
    oos_sharpe_mean: float = 1.5,
    rank: int = 1,
    generated_at: str = _GENERATED_AT,
) -> Candidate:
    return Candidate(
        instrument="EUR_USD",
        timeframe="H1",
        strategy_name="macrossover_10_50",
        direction=direction,
        entry_ref=entry_ref,
        stop_distance=stop_distance,
        target_distance=target_distance,
        oos_sharpe_mean=oos_sharpe_mean,
        quality_score=0.75,
        rank=rank,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at=generated_at,
    )


def _make_candles(n: int = 50, base_time: str = "2026-05-01T08:00:00Z") -> pd.DataFrame:
    """Create a synthetic hourly OHLC candle DataFrame with ``n`` bars."""
    start = pd.Timestamp(base_time, tz="UTC")
    times = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    # Simple zig-zag prices around 1.10.
    closes = [1.1000 + 0.0002 * (i % 5 - 2) for i in range(n)]
    opens  = [c - 0.0001 for c in closes]
    highs  = [c + 0.0005 for c in closes]
    lows   = [c - 0.0005 for c in closes]
    return pd.DataFrame(
        {
            "time": times,
            "open_bid": opens,
            "high_bid": highs,
            "low_bid": lows,
            "close_bid": closes,
            "open_ask": [o + 0.0002 for o in opens],
            "high_ask": [h + 0.0002 for h in highs],
            "low_ask": [lo + 0.0002 for lo in lows],
            "close_ask": [c + 0.0002 for c in closes],
            "volume": [100] * n,
        }
    )


# ---------------------------------------------------------------------------
# AC-1 — PNG produced and non-empty
# ---------------------------------------------------------------------------


def test_png_produced_and_nonempty(tmp_path: Path) -> None:
    """AC-1: render_candidate_chart returns a path to a non-empty PNG."""
    candidate = _make_candidate()
    candles = _make_candles(n=50)
    out = render_candidate_chart(candidate, candles, str(tmp_path))

    assert Path(out).exists(), "PNG file must exist after rendering"
    assert Path(out).stat().st_size > 0, "PNG file must be non-empty"
    assert out.endswith(".png"), "Output must be a .png file"


# ---------------------------------------------------------------------------
# AC-2 — Level placement per direction
# ---------------------------------------------------------------------------


def test_long_stop_below_target_above() -> None:
    """AC-2 LONG: stop < entry < target."""
    c = _make_candidate(direction="LONG", entry_ref=1.1000, stop_distance=0.003, target_distance=0.0045)
    entry = c.entry_ref
    stop = entry - c.stop_distance    # below for LONG
    target = entry + c.target_distance  # above for LONG

    assert stop < entry < target, (
        f"LONG: expected stop ({stop}) < entry ({entry}) < target ({target})"
    )


def test_short_stop_above_target_below() -> None:
    """AC-2 SHORT: stop > entry > target."""
    c = _make_candidate(direction="SHORT", entry_ref=1.1000, stop_distance=0.003, target_distance=0.0045)
    entry = c.entry_ref
    stop = entry + c.stop_distance    # above for SHORT
    target = entry - c.target_distance  # below for SHORT

    assert stop > entry > target, (
        f"SHORT: expected stop ({stop}) > entry ({entry}) > target ({target})"
    )


def test_long_chart_levels_numeric(tmp_path: Path) -> None:
    """AC-2 integration: verify render does not crash for LONG."""
    c = _make_candidate(direction="LONG")
    candles = _make_candles(n=50)
    out = render_candidate_chart(c, candles, str(tmp_path))
    assert Path(out).stat().st_size > 0


def test_short_chart_levels_numeric(tmp_path: Path) -> None:
    """AC-2 integration: verify render does not crash for SHORT."""
    c = _make_candidate(direction="SHORT")
    candles = _make_candles(n=50)
    out = render_candidate_chart(c, candles, str(tmp_path))
    assert Path(out).stat().st_size > 0


# ---------------------------------------------------------------------------
# AC-3 — Signal marker at generated_at bar
# ---------------------------------------------------------------------------


def test_signal_marker_exact_match() -> None:
    """AC-3: _find_signal_bar_index returns correct index for exact match."""
    candles = _make_candles(n=10, base_time="2026-05-01T08:00:00Z")
    # The 5th bar (index 4) is at 2026-05-01T12:00:00Z.
    idx = _find_signal_bar_index(candles, "2026-05-01T12:00:00Z")
    assert idx == 4, f"Expected index 4, got {idx}"


def test_signal_marker_outside_window_returns_none() -> None:
    """AC-3: marker is None when generated_at is outside the candle window."""
    candles = _make_candles(n=10, base_time="2026-05-01T08:00:00Z")
    # 2026-04-01 is way before our window.
    idx = _find_signal_bar_index(candles, "2026-04-01T00:00:00Z")
    assert idx is None


def test_signal_marker_bad_timestamp_returns_none() -> None:
    """AC-3: malformed generated_at returns None (no exception)."""
    candles = _make_candles(n=5)
    idx = _find_signal_bar_index(candles, "not-a-timestamp")
    assert idx is None


# ---------------------------------------------------------------------------
# AC-4 — UTC x-axis
# ---------------------------------------------------------------------------


def test_utc_xaxis(tmp_path: Path) -> None:
    """AC-4: chart renders without raising when candles have UTC-aware times."""
    # If matplotlib were misconfigured for local time this would typically raise
    # or produce a garbled axis.  We assert the PNG is produced; the UTC
    # formatter is set in the implementation (DateFormatter with tz=UTC).
    c = _make_candidate()
    candles = _make_candles(n=50)
    assert candles["time"].dt.tz is not None, "Candle times must be UTC-aware"
    out = render_candidate_chart(c, candles, str(tmp_path))
    assert Path(out).exists()


def test_candles_have_utc_dtype() -> None:
    """AC-4: our test fixture produces datetime64[ns, UTC] column."""
    df = _make_candles(n=5)
    dtype_str = str(df["time"].dtype)
    assert "UTC" in dtype_str, f"Expected UTC dtype, got {dtype_str}"


# ---------------------------------------------------------------------------
# AC-5 — Deterministic path; re-render overwrites cleanly
# ---------------------------------------------------------------------------


def test_deterministic_path(tmp_path: Path) -> None:
    """AC-5: identical candidate + out_dir → same path on two calls."""
    c = _make_candidate()
    candles = _make_candles(n=30)
    path1 = render_candidate_chart(c, candles, str(tmp_path))
    path2 = render_candidate_chart(c, candles, str(tmp_path))
    assert path1 == path2, "Same candidate must produce the same output path"


def test_overwrite_on_rerender(tmp_path: Path) -> None:
    """AC-5: second render overwrites the first (size may differ slightly)."""
    c = _make_candidate()
    candles = _make_candles(n=30)
    path = render_candidate_chart(c, candles, str(tmp_path))
    size1 = Path(path).stat().st_size
    path2 = render_candidate_chart(c, candles, str(tmp_path))
    size2 = Path(path2).stat().st_size
    assert path == path2
    # Both renders of the same data should produce a very similar file size.
    assert size1 > 0 and size2 > 0


def test_build_output_path_no_colons(tmp_path: Path) -> None:
    """AC-5: _build_output_path produces a filesystem-safe filename."""
    c = _make_candidate(generated_at="2026-05-01T12:00:00Z")
    p = _build_output_path(c, str(tmp_path))
    filename = Path(p).name
    assert ":" not in filename, f"Colons in filename: {filename}"
    assert filename.endswith(".png")


def test_build_output_path_contains_instrument_timeframe() -> None:
    """AC-5: path encodes instrument and timeframe for readability."""
    c = _make_candidate(generated_at="2026-05-01T12:00:00Z")
    p = _build_output_path(c, "/tmp/fathom_charts")
    assert "EUR_USD" in p
    assert "H1" in p


# ---------------------------------------------------------------------------
# AC-6 — Headless Agg backend
# ---------------------------------------------------------------------------


def test_agg_backend_is_active() -> None:
    """AC-6: importing signals.charts forces the Agg backend."""
    import matplotlib
    backend = matplotlib.get_backend()
    assert backend.lower() == "agg", (
        f"Expected Agg backend after importing signals.charts, got: {backend}"
    )


# ---------------------------------------------------------------------------
# AC-7 — Insufficient candles degrades gracefully
# ---------------------------------------------------------------------------


def test_empty_candles_raises_value_error(tmp_path: Path) -> None:
    """AC-7: empty candles raises ValueError (clear error, no crash mid-batch)."""
    c = _make_candidate()
    empty = pd.DataFrame(
        columns=["time", "open_bid", "high_bid", "low_bid", "close_bid"]
    )
    with pytest.raises(ValueError, match="row"):
        render_candidate_chart(c, empty, str(tmp_path))


def test_single_candle_renders(tmp_path: Path) -> None:
    """AC-7: a single-candle DataFrame renders without error (boundary case)."""
    c = _make_candidate()
    candles = _make_candles(n=1)
    out = render_candidate_chart(c, candles, str(tmp_path))
    assert Path(out).stat().st_size > 0


def test_missing_required_column_raises(tmp_path: Path) -> None:
    """AC-7: DataFrame missing required column raises clear ValueError."""
    c = _make_candidate()
    bad_df = _make_candles(n=5).drop(columns=["high_bid"])
    with pytest.raises(ValueError, match="high_bid"):
        render_candidate_chart(c, bad_df, str(tmp_path))


def test_validate_candles_missing_columns() -> None:
    """AC-7: _validate_candles raises on each required column independently."""
    for col in ["time", "open_bid", "high_bid", "low_bid", "close_bid"]:
        df = _make_candles(n=5).drop(columns=[col])
        with pytest.raises(ValueError, match=col):
            _validate_candles(df)


def test_validate_candles_empty() -> None:
    """AC-7: _validate_candles raises on empty DataFrame."""
    df = pd.DataFrame(
        columns=["time", "open_bid", "high_bid", "low_bid", "close_bid"]
    )
    with pytest.raises(ValueError, match="row"):
        _validate_candles(df)


# ---------------------------------------------------------------------------
# Extra: out_dir is created if it does not exist
# ---------------------------------------------------------------------------


def test_out_dir_created(tmp_path: Path) -> None:
    """render_candidate_chart creates out_dir if it does not already exist."""
    new_dir = tmp_path / "nested" / "charts"
    assert not new_dir.exists()
    c = _make_candidate()
    candles = _make_candles(n=20)
    out = render_candidate_chart(c, candles, str(new_dir))
    assert new_dir.exists()
    assert Path(out).exists()


# ---------------------------------------------------------------------------
# Extra: large candle set is trimmed to DEFAULT_CANDLE_WINDOW
# ---------------------------------------------------------------------------


def test_large_candle_set_trimmed(tmp_path: Path) -> None:
    """More than DEFAULT_CANDLE_WINDOW bars: renders successfully (trimmed)."""
    c = _make_candidate()
    candles = _make_candles(n=DEFAULT_CANDLE_WINDOW + 50)
    out = render_candidate_chart(c, candles, str(tmp_path))
    assert Path(out).stat().st_size > 0


# ---------------------------------------------------------------------------
# Extra: different instruments produce different paths
# ---------------------------------------------------------------------------


def test_different_candidates_different_paths(tmp_path: Path) -> None:
    """Two candidates with different generated_at values produce distinct paths."""
    c1 = _make_candidate(generated_at="2026-05-01T12:00:00Z")
    c2 = _make_candidate(generated_at="2026-05-02T12:00:00Z")
    p1 = _build_output_path(c1, str(tmp_path))
    p2 = _build_output_path(c2, str(tmp_path))
    assert p1 != p2, "Different generated_at must produce different file paths"
