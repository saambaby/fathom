# Feature: chart-generation

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Render a candle chart for a watchlist candidate — recent candles plus overlays for the proposed entry, stop-loss, and take-profit levels and the signal marker — and save it as a PNG for Discord delivery. Uses `matplotlib` (D-P2-2): simple dependency, native PNG export, no headless-browser/kaleido requirement. This is the visual that accompanies each ranked candidate in the daily watchlist.

## User-facing behaviour

Backend module `signals/charts.py`. `render_candidate_chart(candidate: Candidate, candles: pd.DataFrame, out_dir: str) -> str`:
- Plots the recent candle window (OHLC) for the candidate's instrument/timeframe.
- Overlays: a marker at the signal bar, a horizontal line at `candidate.entry_ref`, a stop line at `entry_ref ∓ candidate.stop_distance`, a target line at `entry_ref ± candidate.target_distance` (sign per `candidate.direction`).
- Title carries `candidate.instrument`, `timeframe`, `strategy_name`, `direction`, `oos_sharpe_mean`, and `rank` (the flat `Candidate` fields per INV-13 — not `Signal.quality_score`).
- Saves a PNG to `out_dir`, returns the path. X-axis labelled in UTC (INV-03).

## Acceptance criteria

- [ ] Produces a PNG file on disk for a given candidate + candle window; returns its path.
- [ ] The chart shows candles plus three correctly-placed horizontal levels (entry, stop, target) — stop below/target above for LONG, inverted for SHORT.
- [ ] A signal marker sits at the candidate's `generated_at` bar.
- [ ] X-axis time labels are UTC (INV-03); no naive/local times.
- [ ] Deterministic output path (e.g. `{instrument}_{timeframe}_{run_ts}.png`); re-render overwrites cleanly.
- [ ] Renders headless (Agg backend) — no display server required (it runs under Hermes/cron).
- [ ] A candidate with insufficient candles degrades gracefully (renders what it has or raises a clear error — no crash mid-batch).

## Component design

`signals/charts.py` using `matplotlib` with the `Agg` (non-interactive) backend forced at import (so it works under cron/Hermes with no display). Candles from `Store.load_candles`. Levels read from the **flat `Candidate` fields** (`candidate.entry_ref`, `candidate.stop_distance`, `candidate.target_distance`, `candidate.direction`) per the INV-13 contract — `Candidate` flattens `Signal`'s fields, so there is no `candidate.signal.*` nesting. Keep it a pure function (candidate + df → path) for testability — assert the file exists and is non-empty rather than pixel-diffing.

## Non-goals

- No interactive/JS charts — that's TradingView Lightweight Charts in the Phase 5 admin panel. Phase 2 produces static PNGs for Discord.
- No indicator panels beyond the candidate's own levels (keep it readable for a Discord attachment).
- No delivery — Hermes posts the PNG to Discord (INV-01 boundary; this feature only renders).

## Touches

- [INV-03] — UTC time axis.
- [INV-01] — produces an image artefact only; no orders.

## Depends on

- [[signal-ranker]] — the `Candidate` (and its `Signal` levels) being charted.
- `Store.load_candles` (shipped) — the candle window.

External:
- `matplotlib` — NEW dependency (coordinator adds to `pyproject.toml` + CLAUDE.md Stack).

## Approach

Force `matplotlib.use("Agg")` before `pyplot` import. Draw OHLC bars/candlesticks over the recent N candles, then `axhline` for entry/stop/target with distinct styles, a scatter marker at the signal bar, a descriptive title. Save with `fig.savefig(path, dpi=…)` and close the figure (no leak across a batch). Tests assert the PNG is produced and non-trivial in size for a representative candidate.

## Open questions

- Candle window length to show (e.g. last 100 bars) — confirm a readable default.
- Candlestick rendering: hand-drawn with `matplotlib` primitives vs a tiny helper — lean hand-drawn to avoid another dependency (`mplfinance`).

## Out of scope

- Discord posting ([[hermes-job-definitions]]), the CLI `chart` command surface ([[cli-commands]]), the Phase 5 interactive panel.
