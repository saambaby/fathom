# Feature: bollinger-zscore-reversion

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add a Bollinger-band / z-score mean-reversion strategy. When price stretches a configurable number of standard deviations from its moving average (a high absolute z-score), it signals a reversion *against* the stretch — SHORT when over-extended above, LONG when over-extended below. New file `strategies/mean_reversion.py`. **Shares that file with [[rsi-reversion]]** — per [[code-map]] these two must be serialized or merged, never built by parallel workers.

## User-facing behaviour

Backend strategy. `BollingerReversion(Strategy)`, parameterised by `period: int` (MA + std window) and `num_std: float` (band width / z-score threshold). `generate_signals(df)`: SHORT when close's z-score ≥ `+num_std` (upper band breach), LONG when ≤ `−num_std` (lower band breach), at most one per bar.

## Acceptance criteria

- [ ] LONG when close z-score relative to the rolling `period`-bar mean/std is ≤ `−num_std`; SHORT when ≥ `+num_std`.
- [ ] No signal while price is within the bands.
- [ ] `stop_distance` = ATR(14) via the shared `strategies/_indicators.py::atr()` (> 0); `target_distance` = `stop_distance × rr_ratio` (default 1.5). Per **INV-11** this convention is fixed — no band-midline target.
- [ ] `quality_score` ∈ [0, 1] from the magnitude of the z-score beyond the threshold.
- [ ] At most one signal per bar; `generated_at` = bar close (UTC-aware, INV-03).
- [ ] Rolling std uses sample std (ddof=1) consistently; EMAs/MAs documented (SMA vs EMA chosen explicitly).
- [ ] Tested at `(period, num_std)` ∈ {(20, 2.0), (20, 2.5)}.

## Non-goals

- No trend filter to suppress reversion in strong trends (a known weakness — left to the ranker/portfolio layer in Phase 2, or a future enhancement).
- No band-midline trailing exit (engine applies fixed stop/target).

## Touches

- [INV-03] — `generated_at` UTC-aware. [INV-10] — signals gated by approved-set.
- [INV-11] — ATR(14) stop + `stop × rr_ratio` target via the shared indicator helper (fixed; no midline target).

## Depends on

- `strategies/base.py` — exists on `main`.
- `strategies/_indicators.py::atr()` — the single shared ATR helper (per INV-11 / AMBIGUOUS-02).
- File-shares `strategies/mean_reversion.py` with [[rsi-reversion]] (serialize per code-map).

## Approach

New `mean_reversion.py`. Compute rolling mean + sample std over `period`; z = (close − mean) / std. Emit SHORT/LONG on threshold breach with the shared `_indicators.atr()` stop (INV-11) and a fixed `stop × rr_ratio` target.

## Open questions

- SMA or EMA for the band centre? (Lean: SMA — classic Bollinger.)

## Out of scope

- The RSI reversion strategy ([[rsi-reversion]]) — separate spec, same file.
