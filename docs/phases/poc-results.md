# Fathom PoC — Results

**Run date:** 2026-05-29 (live OANDA demo account, `ENV=demo`)
**Verdict:** ❌ **0 of 36 combinations approved** — empty approved-set
**Decision:** Accepted as an honest negative for MA-crossover-alone. Per-window gate kept strict. **Proceed to Phase 1.**
**Exit status:** 0 (empty approved-set is a valid PoC result, not a failure — see [poc.md](poc.md))

---

## What was tested

- **Pairs:** EUR_USD, GBP_USD, USD_JPY
- **Timeframes:** H1, D
- **Strategy:** MA crossover (the single PoC strategy)
- **Param grid:** fast ∈ {10, 20} × slow ∈ {50, 100, 200} → 6 combos
- **Total:** 3 × 2 × 6 = **36 (pair, timeframe, params) combinations**
- **Walk-forward:** 12-month train / 3-month test, stepping 3 months → 4 OOS windows over ~2 years
- **Costs modelled:** spread + slippage (INV-06). Swap NOT modelled (`swap_modelled=False`, per D-03).
- **Approval gate (lead ruling):** per-window — every OOS window must individually have Sharpe > 0 AND trade_count ≥ 5.

## Data fetched (live demo, cached to SQLite)

| Pair | H1 candles | D candles |
|---|---|---|
| EUR_USD | 12,433 | 519 |
| GBP_USD | 12,433 | 519 |
| USD_JPY | 12,433 | 519 |

## Headline result

```
PoC runner finished. Approved entries: 0
No combinations passed walk-forward criteria.
```

## Diagnostic — EUR_USD H1 (representative sample)

Per-window OOS trade counts and Sharpe (cost params: spread 1.0 pip, slippage 0.5 pip):

| fast | slow | OOS trades / window | OOS Sharpe / window | Approved |
|---|---|---|---|---|
| 10 | 50 | [34, 31, 39, 37] | [−0.13, −0.21, −0.64, **+0.24**] | ✗ |
| 20 | 50 | [32, 26, 20, 28] | [−0.76, −0.52, −0.65, −0.15] | ✗ |
| 10 | 100 | [25, 22, 29, 26] | [−0.10, −0.30, −0.59, **+0.20**] | ✗ |
| 20 | 200 | [14, 16, 10, 16] | [**+0.35**, **+0.14**, **+0.55**, −0.11] | ✗ |

> Note: the original run's full 36-combo log was truncated (`tail` buffering). This EUR_USD H1 sample is representative; the daily-timeframe combos in the run tail showed 0–2 trades/window (structurally starved — a 3-month daily window is ~65 bars, too few for a slow MA to form).

## Diagnosis

1. **The binding constraint is edge, not the gate.** On H1, the strategy trades plenty (20–39 trades/window — well clear of the ≥5 floor). Rejection is driven by the **Sharpe gate**: OOS Sharpe is mostly negative. `fast=20/slow=50` loses in all four windows. MA-crossover alone has no robust out-of-sample edge here — the expected result for a known-weak baseline.

2. **The daily timeframe is structurally starved** (~0–2 trades/window) — slow MAs can't form on ~65 daily bars per test window. Daily was never going to clear a ≥5-trades/window bar at these params.

3. **One near-miss:** `fast=20/slow=200` on H1 is positive in 3 of 4 windows (mean OOS Sharpe ≈ +0.23) with only the final window at −0.11. Under the per-window ruling ("every window must be positive"), the one negative window rejects it. This is the single case where a majority/aggregate criterion would have differed. Judgement: 3-of-4 positive with small magnitudes is *suggestive but not robust*; loosening the gate to force a pass would risk the overfitting trap the project's own caveats warn against ([product-spec.md](../product-spec.md) §5). Gate kept strict.

## What the PoC proved (independent of the edge result)

The PoC's primary deliverable — the **research pipeline** — works end-to-end against live demo data: OANDA REST fetch → SQLite cache → MA-crossover signals → event-driven backtest with costs → walk-forward validation → approved-set table. INV-03 (UTC timestamps), INV-06 (non-zero costs), and INV-08 (no committed/logged secrets) all held. The thesis question was answered cheaply, before any execution, orchestration, or dashboard code was written.

## Decision and next step

**Accepted negative → proceed to Phase 1.** MA-crossover alone is insufficient; Phase 1's broader strategy set (Donchian, Bollinger/z-score, RSI, ROC momentum, session/range breakout) across the full pair universe is where genuine edge is more likely to surface. The approval criteria stay strict — Phase 1 seeks *robust* edge, not a manufactured pass.

See [phase-1.md](phase-1.md). Before Phase 1 can be orchestrated, the deferred planning artefacts are: per-feature specs under `docs/features/`, `docs/code-map.md`, then a Phase 1 taskgraph (per the half-cycle Layer 4 → Layer 5 loop).
