# Feature: session-range-breakout

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add a session/range breakout strategy: define a reference range (e.g. the Asian session range, or the prior N-bar range) and signal a breakout when price closes beyond it during the active session. Useful intraday around session opens. New file `strategies/breakout.py`. Session boundaries are defined in UTC (INV-03).

## User-facing behaviour

Backend strategy. `SessionRangeBreakout(Strategy)`, parameterised by the reference-range definition (`range_start_utc`, `range_end_utc` for a session window, or `range_lookback` bars) and an optional `buffer_pips` to filter marginal breaks. `generate_signals(df)`: LONG when a bar closes above the reference-range high (+ buffer), SHORT when it closes below the range low (− buffer), at most one per bar.

## Acceptance criteria

- [ ] Reference range computed correctly — either a fixed UTC session window per day or a rolling `range_lookback`-bar high/low.
- [ ] LONG when close breaks above range high + `buffer_pips`; SHORT below range low − `buffer_pips`.
- [ ] Session windows are defined and compared in **UTC** (INV-03) — no local-time assumptions; FX session boundaries (Sydney/Tokyo/London/NY) stated as UTC offsets and documented.
- [ ] At most one breakout signal per session/day per direction (no repeated firing once the range is broken).
- [ ] `stop_distance` = ATR(14) via the shared `strategies/_indicators.py::atr()` (> 0); `target_distance` = `stop_distance × rr_ratio` (default 1.5). Per **INV-11** the stop is ATR-derived (a range-width stop is a future-enhancement note, not this spec).
- [ ] `quality_score` ∈ [0, 1] from break distance beyond the range edge.
- [ ] `generated_at` = bar close (UTC-aware, INV-03).
- [ ] Tested on H1 with a defined session window and on a rolling-range variant.

## Non-goals

- No false-breakout fade logic.
- No daily-bias filter.

## Touches

- [INV-03] — session boundaries and `generated_at` strictly UTC. This is the strategy where UTC discipline matters most (sessions are time-of-day defined).
- [INV-10] — gated by approved-set.
- [INV-11] — ATR(14) stop + `stop × rr_ratio` target via the shared indicator helper.

## Depends on

- `strategies/base.py` — exists on `main`.

## Approach

New `breakout.py`. For the session variant: group bars by UTC date, compute the reference-window high/low, then detect the first close beyond it during the active window; reset per day. For the rolling variant: `rolling(range_lookback).max()/.min()` shifted by 1 bar. The "once per session per direction" rule needs explicit state across bars within a day — implement as a per-day latch, not a naive per-bar check.

## Open questions

- Which reference range is primary for Phase 1 — fixed Asian-session window (classic, but needs the session times pinned in UTC) or rolling N-bar range (simpler, timeframe-agnostic)? (Lean: rolling N-bar range for Phase 1 simplicity; add the session-window variant if it earns its place.)
- Session times: confirm the UTC offsets to use given DST shifts (London/NY observe DST; Tokyo does not). (Lean: if doing the session variant, use fixed UTC windows and note the DST caveat in results.)

## Out of scope

- ROC momentum ([[roc-momentum]]) — distinct strategy despite the product doc grouping breakout under momentum.
