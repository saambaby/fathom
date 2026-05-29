# Feature: position-sizing

**Status.** ready
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

1. Compute the per-unit risk in account currency (DRIFT-07): for a base/quote pair,
   the loss-per-unit if the stop is hit is `stop_distance × quote_to_account_rate`
   (price units × the quote→account-currency rate). For a quote==account pair the
   rate is 1; for a non-account-quote pair (e.g. USD_JPY with a USD account) apply
   the conversion rate. **There is no `InstrumentMeta.pip_value` field — per-unit
   risk is derived from `stop_distance` (a price distance) and the quote rate; only
   `pip_location` is used, and only to validate price precision.**
2. `risk_budget = equity × risk_fraction` (0.25% default).
3. `units = floor(risk_budget / per_unit_risk)`, signed by direction.
4. **Reject** (return a `SizingResult` with `units=0` and a reason) if the budget
   cannot fund the instrument's `InstrumentMeta.min_trade_size`, or if
   `stop_distance ≤ 0`. (No max-trade-size clamp — OANDA's `InstrumentMeta` exposes
   no per-instrument max; the book-level cap is enforced by [[risk-limits-kill-switch]].)

`SizingResult` carries `units`, `risk_amount` (actual money at risk), `reason`
(when rejected). The execution CLI surfaces the reason on rejection.

## Acceptance criteria

- [ ] For a worked example (known equity, stop distance, pip value), `units` is the largest size whose stop-loss equals ≤ 0.25% of equity — verified by hand-computed fixtures for EUR_USD (quote=USD) and USD_JPY (quote=JPY, conversion required).
- [ ] The realized risk (`units × per_unit_risk`) never exceeds `equity × risk_fraction` — property-tested across random equity/stop combinations (INV-05).
- [ ] `stop_distance ≤ 0` → reject (`units=0`, reason set); never sizes naked (INV-04/11 boundary).
- [ ] A budget too small for `InstrumentMeta.min_trade_size` → reject with a clear reason; never rounds up to the minimum. (No max-size clamp.)
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

**Resolved at cross-spec audit (2026-05-29):** DRIFT-07 — per-unit risk derives from
`stop_distance × quote_to_account_rate` (no `pip_value` field); the max-size clamp
is dropped (no source). AMBIGUOUS-02 — the quote→account **rate source is the latest
cached candle mid** for the conversion pair; `equity` is the live account-summary
value fetched once by [[execution-cli]]. The freshness mismatch (cached rate vs live
equity) is accepted — it perturbs the 0.25% cap by less than intraday rate drift.

## Out of scope

- Book-level limits ([[risk-limits-kill-switch]]), submission ([[order-placement]]).
