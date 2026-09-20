"""Watchlist → Pine Script v6 renderer (phase-07 pine-generation).

``render_pine`` is a pure, clock-free transform over ``PineItem`` rows.
Stale / reduce-size flags are caller-computed (CLI or ``fathom analyze``);
this module never reads the wall clock, the watchlist run timestamp, or
the network. INV-01: no execution, risk, cli, AI, or HTTP imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from signals.ranker import Candidate

PINE_VERSION: int = 6
LINE_ANCHOR_BARS: int = 50


@dataclass(frozen=True)
class PineItem:
    """Thin frozen wrapper: INV-13 ``Candidate`` plus caller-computed flags."""

    candidate: Candidate
    stale: bool
    reduce_size: bool


def ticker_from_instrument(instrument: str) -> str:
    """OANDA ``EUR_USD`` → TradingView ``syminfo.ticker`` ``EURUSD``."""
    return instrument.replace("_", "")


def levels_for(candidate: Candidate) -> tuple[float, float, float]:
    """Return ``(entry, stop, target)`` absolute prices (direction-correct)."""
    entry = float(candidate.entry_ref)
    if candidate.direction == "SHORT":
        stop = entry + float(candidate.stop_distance)
        target = entry - float(candidate.target_distance)
    else:
        stop = entry - float(candidate.stop_distance)
        target = entry + float(candidate.target_distance)
    return entry, stop, target


def display_precision_for(instrument: str) -> int:
    """Fallback display precision: 3 dp for JPY quotes, else 5 dp."""
    if instrument.endswith("JPY") or instrument.endswith("_JPY"):
        return 3
    return 5


def format_price(instrument: str, price: float) -> str:
    return f"{price:.{display_precision_for(instrument)}f}"


def render_pine(items: list[PineItem]) -> str:
    """Render a compiling Pine v6 overlay indicator from ``items``.

    Empty ``items`` → timestamp-free no-candidates script. Non-empty lists
    are ordered by ``candidate.rank``. The only timestamps in the output
    are stored ``generated_at`` values (INV-03).
    """
    header = (
        f"//@version={PINE_VERSION}\n"
        'indicator("Fathom watchlist", overlay=true, '
        "max_lines_count=500, max_labels_count=500)\n"
        "var table fathomStatus = table.new(position.top_right, 1, 1)\n"
    )
    if not items:
        return (
            header
            + "if barstate.islast\n"
            + '    table.cell(fathomStatus, 0, 0, "Fathom: no candidates")\n'
        )

    ordered = sorted(items, key=lambda item: item.candidate.rank)
    any_stale = any(item.stale for item in ordered)
    max_generated = max(item.candidate.generated_at for item in ordered)
    status_bits = [max_generated, f"n={len(ordered)}"]
    if any_stale:
        status_bits.append("STALE")
    status_text = _pine_escape(" ".join(status_bits))

    body: list[str] = [
        "if barstate.islast",
        f'    table.cell(fathomStatus, 0, 0, "{status_text}")',
    ]
    for item in ordered:
        body.extend(_candidate_block(item))
    return header + "\n".join(body) + "\n"


def _candidate_block(item: PineItem) -> list[str]:
    c = item.candidate
    ticker = _pine_escape(ticker_from_instrument(c.instrument))
    entry, stop, target = levels_for(c)
    entry_s = format_price(c.instrument, entry)
    stop_s = format_price(c.instrument, stop)
    target_s = format_price(c.instrument, target)
    n = LINE_ANCHOR_BARS
    label = _pine_escape(_label_text(item))
    entry_color = "color.blue" if c.direction == "LONG" else "color.orange"
    return [
        f'    if syminfo.ticker == "{ticker}"',
        f"        line.new(bar_index - {n}, {entry_s}, bar_index, {entry_s}, "
        f"color={entry_color}, width=1)",
        f"        line.new(bar_index - {n}, {stop_s}, bar_index, {stop_s}, "
        "color=color.red, width=1)",
        f"        line.new(bar_index - {n}, {target_s}, bar_index, {target_s}, "
        "color=color.green, width=1)",
        f'        label.new(bar_index, {entry_s}, "{label}", '
        "style=label.style_label_left)",
    ]


def _label_text(item: PineItem) -> str:
    c = item.candidate
    sharpe = f"{float(c.oos_sharpe_mean):.2f}"
    parts = [
        str(c.rank),
        c.strategy_name,
        c.direction,
        c.timeframe,
        f"Sharpe {sharpe}",
    ]
    if c.news_flag:
        parts.append("⚠ news")
    if item.stale:
        parts.append("(stale)")
    if item.reduce_size:
        parts.append("reduce size")
    return " ".join(parts)


def _pine_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
