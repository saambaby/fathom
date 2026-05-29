# Feature: position-sizing

**Status.** draft
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The deterministic function that turns an approved `Candidate` and the current
account equity into a **unit count**, such that the loss if the stop is hit is at
most **0.25% of equity** (INV-05). Size is *derived* from the stop distance and the
risk budget — never a fixed lot. This is the first gate that can reject a trade
(an uncomputable size → no order, never a naked or oversized one).

## User-facing behaviour

Backend module `risk/sizing.py`. `size_position(candidate, equity, *, instrument_meta, risk_fraction=0.0025) -> SizingResult`:

1. Compute the per-unit risk = `stop_distance` converted to account-currency value
   per unit (via `InstrumentMeta` pip value / quote conversion).
2. `risk_budget = equity × risk_fraction` (0.25% default).
3. `units = floor(risk_budget / per_unit_risk)`, signed by direction.
4. Clamp to the instrument's min/max trade size; **reject** (return a
   `SizingResult` with `units=0` and a reason) if the budget cannot fund even the
   minimum size, or if `stop_distance ≤ 0`.

`SizingResult` carries `units`, `risk_amount` (actual money at risk), `reason`
(when rejected). The execution CLI surfaces the reason on rejection.

## Acceptance criteria

- [ ] For a worked example (known equity, stop distance, pip value), `units` is the largest size whose stop-loss equals ≤ 0.25% of equity — verified by hand-computed fixtures for EUR_USD (quote=USD) and USD_JPY (quote=JPY, conversion required).
- [ ] The realized risk (`units × per_unit_risk`) never exceeds `equity × risk_fraction` — property-tested across random equity/stop combinations (INV-05).
- [ ] `stop_distance ≤ 0` → reject (`units=0`, reason set); never sizes naked (INV-04/11 boundary).
- [ ] A budget too small for the instrument minimum → reject with a clear reason; never rounds up to the minimum.
- [ ] `risk_fraction` is a parameter defaulting to `0.0025`; it is read from config, and the default cap cannot be exceeded silently.
- [ ] Quote-currency conversion is correct for non-USD-quote pairs (uses current rate from the candle store / account summary); a JPY-quote fixture pins it.
- [ ] Pure and deterministic — no network, no clock; equity and rates are inputs.

## Component design

`risk/sizing.py` is a pure function over `(Candidate, equity, InstrumentMeta,
rate)`. The pip/quote-conversion maths is the subtle part: per-unit risk in account
currency = `stop_distance × units_per_price_move × quote_to_account_rate`. Account
currency assumed = USD on the demo account (configurable). The 0.25% cap is the
single most safety-critical line in the module → opus, heavy property tests.

## Non-goals

- No exposure/correlation/daily-loss logic — that is [[risk-limits-kill-switch]].
- No order construction — emits a unit count consumed by [[order-model-and-brackets]]/[[order-placement]].
- No equity fetch — equity is an input (the CLI/orchestrator fetches it once).

## Touches

- [INV-05] — owns the 0.25% cap; the enforcement point for per-trade risk.
- [INV-11] — consumes the ATR-derived stop that makes sizing symmetric across strategies.
- [INV-04] — a trade with no valid stop is rejected here, before any order exists.

## Depends on

- [[order-model-and-brackets]] (for `SizingResult`/types alignment), `Candidate` (INV-13), `InstrumentMeta` — `Candidate`/meta on `main`.

## Approach

`risk/` package. Implement the account-currency risk conversion with explicit
`InstrumentMeta` inputs; property-test the cap invariant with hypothesis. The
account-currency assumption (USD) is config-driven, not hard-coded.

## Open questions

- Account currency: assume USD (demo account) for Phase 3, config-driven? Propose yes.
- Rate source for quote conversion: latest cached candle close vs account-summary
  margin rate. Propose latest cached mid; confirm in spec.

## Out of scope

- Book-level limits ([[risk-limits-kill-switch]]), submission ([[order-placement]]).
