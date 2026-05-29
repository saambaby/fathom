# Feature: swap-cost-model

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add overnight swap/financing (and commission, if the account charges it) to the backtest cost model, satisfying INV-06 in full. The PoC deferred swap (D-03): `CostParams`/`apply_costs` rejected any non-zero `swap_pips` and every output carried `swap_modelled=False`. Phase 1 lifts that deferral — multi-day positions (especially on H4/D timeframes) accrue real financing cost, and a backtest that ignores it overstates swing-strategy returns. Financing rates come from the instrument metadata fetched by [[data-layer-expansion]].

## User-facing behaviour

Backend module surface. Consumed by the engine:

- `CostParams` gains `swap_long_rate` / `swap_short_rate` (per-instrument daily financing, mapped from `InstrumentMeta.long_rate`/`short_rate`) and a `commission_pips` field (default 0.0 for spread-only accounts). The legacy `swap_pips` field and both `swap_pips != 0.0 → ValueError` guard sites are **removed**; `swap_modelled` becomes `True` when financing is applied.
- `apply_costs(...)` takes `holding_days: int` (financing days, computed by the engine) so it can charge `swap = rate × days` on the appropriate side. The swap charge is direction-aware: long uses `swap_long_rate`, short uses `swap_short_rate`.
- `CostResult.swap_modelled` is now `True` for any run that passes financing data; `metrics.py` / `walkforward.py` / the approved-set propagate the honest label unchanged.

## Acceptance criteria

- [ ] `apply_costs` charges financing = `daily_rate × holding_days` on the correct side (long vs short), in addition to spread + slippage; `total_cost_pips` includes it.
- [ ] A position closed same-bar (0 holding days) accrues **zero** swap — swap applies only to overnight holds.
- [ ] `swap_modelled` is `True` whenever financing data is supplied and `False` only when explicitly run without it; the label propagates to `Metrics`, `ApprovedSetEntry`.
- [ ] INV-06 still holds: `total_cost_pips > 0` for any non-zero spread/slippage; gross PnL ≥ net PnL on every trade (financing can only worsen or, for a positive-carry side, slightly improve net — the engine must handle positive carry without ever producing a cost-free *spread* path).
- [ ] Commission (if configured > 0) is charged per round trip.
- [ ] A regression test confirms the PoC's spread+slippage numbers are unchanged when `swap=0, commission=0` (backward compatibility).
- [ ] The `swap_pips`-must-be-zero guard (D-03) is removed and its tests updated, not deleted silently.

## Component design

Extend `backtest/costs.py`. Precise, committed changes (resolving audit DRIFT-02):

- **`CostParams`:** **remove** the `swap_pips` field entirely; **remove** the `_swap_must_be_zero` validator. **Add** `swap_long_rate: float`, `swap_short_rate: float` (daily financing in pips, populated from `InstrumentMeta` — see field mapping below), and `commission_pips: float = 0.0`.
- **`apply_costs`:** final committed signature —
  `apply_costs(entry_price, exit_price, direction, spread_pips, slippage_pips, pip_value, swap_long_rate, swap_short_rate, holding_days: int, commission_pips=0.0) -> CostResult`.
  The legacy `swap_pips=0.0` parameter is **removed**, and the inline guard at `costs.py:159` (`if swap_pips != 0.0: raise`) is **removed** along with the pydantic validator — both D-03 guard sites go, not just one.
- **`holding_days`** is computed by the engine (`backtest/engine.py`) per trade from the entry/exit bar UTC dates and passed in. Financing charged = `rate × holding_days` on the direction's side (long → `swap_long_rate`, short → `swap_short_rate`).
- `CostResult.swap_modelled = True` whenever financing is applied.

**Field-name mapping (resolving audit AMBIGUOUS-01):** [[data-layer-expansion]] owns the authoritative names on `InstrumentMeta`: `long_rate`, `short_rate`. This spec maps them at the engine boundary: `CostParams.swap_long_rate = InstrumentMeta.long_rate`, `CostParams.swap_short_rate = InstrumentMeta.short_rate`. The rename happens once, in the engine's `CostParams` construction — documented here so there is no ambiguity about which side owns the name.

Carry sign: positive-carry trades reduce net cost; the cost-floor invariant is on *spread+slippage*, which remains strictly positive, so INV-06 is not weakened by positive carry.

## Non-goals

- No intraday financing (financing is a daily overnight event).
- No modelling of broker-specific swap-free (Islamic) accounts.
- No change to spread/slippage logic (that stays exactly as shipped in the PoC).

## Touches

- [INV-06] — this feature is what makes INV-06 *fully* satisfied (all four cost categories: spread ✓, slippage ✓, commission, swap).
- [INV-03] — holding-days derived from UTC bar timestamps.

## Depends on

- [[data-layer-expansion]] — supplies per-instrument financing rates (`swap_long_rate`, `swap_short_rate`) and `financing_days_of_week` via `InstrumentMeta`.
- PoC `backtest/costs.py` + `backtest/engine.py` — exist on `main`.

## Approach

Surgical extension of the existing, well-tested cost model. The PoC's structural guarantee (costs applied adversely on both legs, path-independent spread+slippage floor) is preserved; swap is an additive per-day charge keyed off holding duration and the per-instrument rate. Reverse the D-03 deferral cleanly: remove the guard, flip `swap_modelled` to reflect reality, keep the spread+slippage maths byte-identical.

## Open questions

- **Weekend triple-swap:** most brokers charge 3× financing on Wednesday (Wed→Thu rollover covers the weekend). Model it, or use a flat per-calendar-day rate for Phase 1? (Lean: flat per-overnight for Phase 1; note the simplification in results.)
- **Holding-days source:** derive from bar count × timeframe, or from calendar days between entry/exit UTC dates? Calendar days is more accurate for D; bar-count is simpler for H1. (Lean: calendar days between entry and exit dates.)
- Does the OANDA demo account charge commission? If spread-only, `commission_pips` defaults to 0 and that path is exercised only by tests.

## Out of scope

- Re-running the PoC's MA-crossover approved-set with swap (that's the runner's job, [[full-universe-backtest-runner]]).
- Position sizing / risk (Phase 3).
