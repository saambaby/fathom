# Feature: donchian-breakout

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add a Donchian channel breakout strategy to the trend family. A long signal fires when price breaks above the highest high of the last N bars; a short when it breaks below the lowest low. It implements the existing `Strategy` interface and produces standard `Signal` objects, so the engine, walk-forward, and approved-set consume it with zero special-casing. Lives in `strategies/trend.py` alongside the shipped `MACrossover`.

## User-facing behaviour

Backend strategy. `DonchianBreakout(Strategy)`, parameterised by `channel_period: int` (lookback for the high/low channel). `generate_signals(df)` returns `Signal` objects: LONG on a close above the prior `channel_period`-bar high, SHORT on a close below the prior `channel_period`-bar low, at most one per bar.

## Acceptance criteria

- [ ] LONG signal when the bar's close breaks above the rolling max of the prior `channel_period` highs (excluding the current bar — no look-ahead within the indicator).
- [ ] SHORT signal on the mirror condition against the rolling min of lows.
- [ ] No signal while price stays inside the channel.
- [ ] `stop_distance` = ATR(14) at the signal bar (> 0); `target_distance` = `stop_distance × rr_ratio` (default 1.5) — same convention as `MACrossover`.
- [ ] `quality_score` ∈ [0, 1] derived from breakout strength (e.g. normalised distance beyond the channel edge).
- [ ] At most one signal per bar; `generated_at` = the bar's close timestamp (UTC-aware, INV-03), never `datetime.now()`.
- [ ] Tested at `channel_period` ∈ {20, 55} (classic Donchian/turtle values).

## Non-goals

- No position management or exits beyond the stop/target the engine applies.
- No volatility filter (that's the momentum strategy's confirmation step).

## Touches

- [INV-03] — `Signal.generated_at` UTC-aware from the bar close.
- [INV-10] — produces signals only; live use is gated by the approved-set.
- [INV-11] — ATR(14) stop + `stop × rr_ratio` target via the shared indicator helper.

## Depends on

- `strategies/base.py` (`Strategy`, `Signal`, `Direction`) — exists on `main`. Anchors to the same contract `MACrossover` uses.
- `strategies/_indicators.py::atr()` — the single shared ATR helper (per INV-11). Part of this work is extracting the existing `trend.py` ATR (`ewm(com=period-1, adjust=False)`) into `_indicators.py` so all strategies share one implementation.

## Approach

Mirror `MACrossover`'s shape in `trend.py`: compute the rolling channel with pandas (`.rolling(channel_period).max()/.min()`, shifted by 1 bar to exclude the current bar), detect the breakout crossing, emit one `Signal` per breakout bar with the shared `_indicators.atr()` stop.

## Open questions

- Use close-based breakout (close beyond channel) or intrabar high/low breakout? Close-based is less whippy and avoids intrabar look-ahead ambiguity. (Lean: close-based.)

## Out of scope

- Any change to `MACrossover` or the `Strategy` interface.
