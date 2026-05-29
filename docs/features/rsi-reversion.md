# Feature: rsi-reversion

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add an RSI-extremes mean-reversion strategy: LONG when RSI falls below an oversold threshold, SHORT when it rises above overbought. Shares `strategies/mean_reversion.py` with [[bollinger-zscore-reversion]]. **Per [[code-map]], these two specs target the same file and must be serialized (or merged into one `mean-reversion-strategies` task) — never parallel workers.**

## User-facing behaviour

Backend strategy. `RSIReversion(Strategy)`, parameterised by `period: int` (RSI lookback, default 14), `oversold: float` (default 30), `overbought: float` (default 70). `generate_signals(df)`: LONG when RSI crosses up out of oversold, SHORT when it crosses down out of overbought, at most one per bar.

## Acceptance criteria

- [ ] RSI computed with Wilder's smoothing (the standard) over `period` bars — documented explicitly (matches the ATR Wilder convention already used in the trend module).
- [ ] LONG on RSI crossing back above `oversold` (exiting the oversold zone); SHORT on crossing back below `overbought`. (Cross-out, not mere level — avoids a continuous signal while pinned in the zone.)
- [ ] No signal while RSI is mid-range.
- [ ] `stop_distance` = ATR(14) at the signal bar (> 0); `target_distance` = `stop_distance × rr_ratio` (default 1.5).
- [ ] `quality_score` ∈ [0, 1] from how deep into the extreme RSI reached before reverting.
- [ ] At most one signal per bar; `generated_at` = bar close (UTC-aware, INV-03).
- [ ] Tested at `(period, oversold, overbought)` ∈ {(14, 30, 70), (14, 20, 80)}.

## Non-goals

- No divergence detection (price/RSI divergence is a richer signal, deferred).
- No trend filter (see [[bollinger-zscore-reversion]] non-goals).

## Touches

- [INV-03] — `generated_at` UTC-aware. [INV-10] — gated by approved-set.
- [INV-11] — ATR(14) stop + `stop × rr_ratio` target via the shared indicator helper.

## Depends on

- `strategies/base.py` — exists on `main`.
- `strategies/_indicators.py::atr()` — the single shared ATR helper (per INV-11).
- Shares `strategies/mean_reversion.py` with [[bollinger-zscore-reversion]].

## Approach

Add `RSIReversion` to `mean_reversion.py`. RSI via Wilder's smoothing (`ewm(com=period-1, adjust=False)` on gains/losses — the same formulation the shared ATR uses). Signal on the cross-out of the extreme zone; stop from the shared `_indicators.atr()`. Because it co-lives with the Bollinger strategy, the worker building this either follows the Bollinger task (serialized) or both ship in one mean-reversion task.

## Open questions

- Cross-out vs level-based trigger — cross-out chosen to avoid repeated signals while pinned. Confirm in Plan.

## Out of scope

- The Bollinger strategy itself ([[bollinger-zscore-reversion]]) — separate spec, same file.
