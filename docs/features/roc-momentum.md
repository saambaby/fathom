# Feature: roc-momentum

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add a rate-of-change (ROC) momentum strategy with a volatility-confirmation filter: go with the move when momentum is strong *and* volatility confirms a real expansion (not noise). LONG on strong positive ROC, SHORT on strong negative ROC, suppressed when volatility is too low to trust the move. New file `strategies/momentum.py`.

## User-facing behaviour

Backend strategy. `ROCMomentum(Strategy)`, parameterised by `roc_period: int`, `roc_threshold: float` (momentum trigger), and `atr_filter_period: int` (volatility confirmation window). `generate_signals(df)`: LONG when ROC ≥ `+roc_threshold` and volatility confirms; SHORT when ROC ≤ `−roc_threshold` and volatility confirms; at most one per bar.

## Acceptance criteria

- [ ] ROC = percentage change of close over `roc_period` bars; LONG/SHORT when it crosses ±`roc_threshold`.
- [ ] **Volatility confirmation:** signal suppressed unless current ATR exceeds its own recent average (range expansion) — the "with volatility confirmation" requirement from the product spec. Documented threshold.
- [ ] No signal when momentum is below threshold OR volatility does not confirm.
- [ ] `stop_distance` = ATR(14) at the signal bar (> 0); `target_distance` = `stop_distance × rr_ratio` (default 1.5).
- [ ] `quality_score` ∈ [0, 1] from ROC magnitude beyond threshold (optionally scaled by the volatility-confirmation margin).
- [ ] At most one signal per bar; `generated_at` = bar close (UTC-aware, INV-03).
- [ ] Tested at `(roc_period, roc_threshold)` ∈ {(10, 0.5%), (20, 1.0%)} with the volatility filter on and off (to prove the filter changes behaviour).

## Non-goals

- No multi-timeframe confirmation.
- No breakout-of-range logic (that's [[session-range-breakout]] — kept distinct despite the product doc grouping them, because the surfaces differ).

## Touches

- [INV-03] — `generated_at` UTC-aware. [INV-10] — gated by approved-set.
- [INV-11] — ATR(14) stop + `stop × rr_ratio` target via the shared indicator helper.

## Depends on

- `strategies/base.py` — exists on `main`.
- `strategies/_indicators.py::atr()` — the single shared ATR helper (per INV-11); also used for the volatility-confirmation gate.

## Approach

New `momentum.py`. ROC via `close.pct_change(roc_period)`. Volatility confirmation compares current ATR (from the shared `_indicators.atr()`) to its rolling mean (range-expansion gate). Emit signal only when both momentum and volatility conditions hold; stop from the same shared ATR.

## Open questions

- Volatility-confirmation definition: ATR > rolling-mean-ATR (range expansion) vs ATR percentile vs Bollinger bandwidth. (Lean: ATR > k × rolling-mean-ATR, k≈1.0, tunable.)
- ROC threshold units — percent vs pips. (Lean: percent, instrument-agnostic.)

## Out of scope

- Session/range breakout ([[session-range-breakout]]).
