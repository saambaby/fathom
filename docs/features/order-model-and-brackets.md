# Feature: order-model-and-brackets

**Status.** ready
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The frozen, in-process data contract for everything execution touches: the
`Order`, `Fill`, and `Position` pydantic models plus the pure function that turns
an approved `Candidate` + a sized unit count into a **bracket** order spec (entry +
stop-loss + take-profit). This is the Phase 3 prerequisite hub — `position-sizing`,
`order-placement`, `reconciliation`, and the monitor all build against these
shapes. It is the execution-side analogue of what `Candidate` (INV-13) is to the
watchlist. No I/O, no OANDA calls — models and bracket maths only.

## User-facing behaviour

Backend module `execution/models.py` (models) + a `build_bracket()` pure function.
Not a CLI surface; consumed by other Phase 3 modules.

- `Order` — an intent to open a position: `client_order_id` (idempotency key,
  derived — see below), `instrument`, `direction`, `units` (signed: +long/−short),
  `entry_type` (`market`), `stop_loss_price`, `take_profit_price`, `candidate_ref`
  (the originating `Candidate` identity — see below), `created_at` (UTC).
- `Fill` — the broker's confirmation: `client_order_id`, `broker_trade_id`,
  `fill_price`, `units_filled` (signed), `slippage` (signed — see convention),
  `filled_at` (UTC), `status` (`filled`|`partial`|`rejected`).
- `Position` — current open state: `broker_trade_id`, `instrument`, `units`
  (signed), `entry_price`, `stop_loss_price`, `take_profit_price`, `opened_at`,
  `unrealized_pl`, `closed_at` (nullable), `realized_pl` (nullable until close),
  `candidate_ref`. (Persisted column list is pinned in [[order-placement]].)
- `build_bracket(candidate, units, *, execution_date, precision) -> Order` —
  converts the `Candidate`'s **price-distance** stop/target into **absolute**
  bracket prices for the order's direction, rounded to `precision`, and computes the
  `client_order_id`. `precision` is bound to `InstrumentMeta.display_precision`
  (DRIFT-07).

**`client_order_id` derivation (DRIFT-03 / INV-15).** `build_bracket` computes
`client_order_id = sha256(f"{instrument}:{strategy_name}:{timeframe}:{generated_at}:{execution_date}").hexdigest()[:32]`
from the `Candidate` fields + the injected `execution_date`. The same formula is
stated in [[order-placement]] and [[execution-cli]].

**`candidate_ref` (DRIFT-04).** A string `f"{instrument}:{timeframe}:{strategy_name}"`
— the same value `fathom execute` ([[execution-cli]]) accepts as its argument and
resolves against the latest persisted watchlist. It is provenance, distinct from the
`client_order_id` (which additionally folds in `generated_at` + `execution_date`).

**Sign convention (AMBIGUOUS-05).** `units`/`units_filled` are signed to match
OANDA v20 (long > 0, short < 0). `slippage` is signed so **positive = adverse**
(worse than `Candidate.entry_ref`) regardless of direction. Pinned by the AC worked
examples.

## Acceptance criteria

- [ ] `build_bracket` produces a stop **and** a take-profit price for every order — there is no code path that yields a stop-less or target-less `Order` (INV-04). A candidate with a non-positive `stop_distance` raises (rejected upstream, never sized naked).
- [ ] Bracket prices are computed by direction: LONG → `stop = entry − stop_distance`, `target = entry + target_distance`; SHORT → mirrored. Verified against worked examples for both directions.
- [ ] Prices are rounded to the instrument's price precision (from `InstrumentMeta`); a round-trip test pins the rounding for a 5-dp (EUR_USD) and a 3-dp (USD_JPY) instrument.
- [ ] `units` sign encodes direction (LONG > 0, SHORT < 0); a zero-unit order is invalid (validator rejects).
- [ ] All datetime fields are UTC-aware RFC 3339 (INV-03); naive datetimes are rejected by validators.
- [ ] The three models serialise/deserialise round-trip losslessly (a JSON round-trip test pins the shape — this is a frozen contract).
- [ ] `client_order_id` is a required, non-empty string on every `Order` (idempotency precondition for `order-placement`).

## Component design

`execution/models.py` holds the three pydantic v2 models with strict validators
(positive prices, signed non-zero units, UTC-aware timestamps), mirroring the
`Signal`/`Candidate` validator style. `build_bracket()` is a pure function (no
broker, no clock beyond the passed `created_at`) so it is exhaustively
unit-testable with hypothesis (property: stop and target always straddle entry on
the correct sides; rounding never moves a stop to the wrong side of entry).

**Frozen-contract intent.** Like `Candidate` (INV-13), these field names/types/shape
are the stable execution wire contract within Fathom. A promotion to a new
invariant is proposed at the Phase 3 cross-spec audit; until then, treat changes as
breaking and reviewable.

## Non-goals

- No OANDA submission, no network, no retries — that is [[order-placement]].
- No sizing (units are an input here) — that is [[position-sizing]].
- No persistence — the store schema lives with [[order-placement]]/[[reconciliation]].

## Touches

- [INV-04] — the bracket is constructed here; no order shape lacks SL+TP.
- [INV-03] — UTC timestamps on all models.
- [INV-11] — consumes the ATR-derived `stop_distance`/`target_distance` unchanged.
- [INV-13] — reads the frozen `Candidate`; never mutates it.
- [INV-14] — these models **are** the frozen execution contract (this spec defines it).
- [INV-15] — computes the deterministic `client_order_id`.

## Depends on

- `signals/ranker.py::Candidate` (shipped, INV-13), `data` `InstrumentMeta` (price precision) — both on `main`.

## Approach

New `execution/` package. Models first, then `build_bracket`. Property-based tests
for the bracket maths. No other Phase 3 module is drafted against an unfrozen
contract — this spec ships and its round-trip test passes before the execution
fan-out begins.

## Open questions

- Entry type: market-only for Phase 3, or also limit/stop-entry? Propose
  **market-only** (the watchlist `entry_ref` is a reference, fills at market);
  revisit if slippage capture shows it matters.

**Resolved at cross-spec audit (2026-05-29):** `Position` carries both
`unrealized_pl` and `realized_pl` (the latter written by [[reconciliation]] on
close); `precision` binds to `InstrumentMeta.display_precision` (DRIFT-07);
`client_order_id`/`candidate_ref`/sign conventions pinned above; models promoted to
**INV-14**.

## Out of scope

- Sizing ([[position-sizing]]), submission ([[order-placement]]), reconciliation
  ([[reconciliation]]), monitoring ([[deviation-monitor]]).
