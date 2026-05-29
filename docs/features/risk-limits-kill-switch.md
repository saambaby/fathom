# Feature: risk-limits-kill-switch

**Status.** draft
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The book-level deterministic gate that decides whether a freshly-sized order is
*allowed onto the book* right now. It enforces exposure caps (max concurrent
trades, max total risk on the book), correlation-aware shared exposure (correlated
pairs count as one bet), and a **daily-loss kill switch** that halts all new
entries once the day's cumulative realized loss crosses a threshold. Like sizing,
it can only subtract: every check is a potential reject, never a green light.

## User-facing behaviour

Backend module `risk/limits.py`. `check_limits(order, *, open_positions, day_pl, equity, config) -> LimitDecision`:

1. **Daily-loss kill switch:** if `day_pl ≤ −(daily_loss_cap × start_of_day_equity)`
   → **reject all** new entries (`kill_switch_active=True`), until UTC-midnight reset.
2. **Max concurrent:** if `len(open_positions) ≥ max_concurrent` → reject.
3. **Book risk:** if `current_book_risk + order_risk > max_book_risk` → reject.
4. **Correlation cap:** group the prospective + open positions by correlation
   bucket; if adding this order pushes a bucket's shared exposure past
   `max_per_correlation_group` → reject.

`LimitDecision` carries `allowed: bool`, `reason`, and `kill_switch_active`. A
read-only `kill_switch_status()` lets the CLI/monitor report state.

## Acceptance criteria

- [ ] Daily cumulative loss at/over the cap → every subsequent `check_limits` returns `allowed=False, kill_switch_active=True` until the UTC-day boundary; a fixture pins the reset at 00:00 UTC (INV-03).
- [ ] `len(open_positions) == max_concurrent` → next order rejected; one fewer → allowed.
- [ ] Book risk that would exceed `max_book_risk` → rejected; the sum is computed from each position's stop-distance risk, not notional.
- [ ] Two correlated instruments (per the correlation source) count as shared exposure; a third correlated entry past `max_per_correlation_group` is rejected while an uncorrelated entry is allowed.
- [ ] Every reject carries a human-readable `reason`; `kill_switch_status()` reports active/inactive + the triggering figure without side effects.
- [ ] Pure/deterministic — open positions, day P&L, equity, and config are inputs (no DB/network/clock beyond an injected `now`).
- [ ] Default config values are explicit and documented (daily_loss_cap, max_concurrent, max_book_risk, correlation thresholds).

## Component design

`risk/limits.py` is a pure decision function over injected state. The correlation
source reuses Phase 2's `portfolio.py` correlation grouping where possible (shared
helper) so the watchlist and the book speak the same correlation language. The
kill switch reads `day_pl` (realized, from the store) and `start_of_day_equity`;
the UTC-day boundary is computed from the injected `now`.

## Non-goals

- No sizing (units arrive sized) — [[position-sizing]].
- No P&L computation — `day_pl` is supplied by the store/reconciliation.
- No auto-flatten — halting *new* entries only; position-level responses live in [[deviation-monitor]].

## Touches

- [INV-05] — book-level extension of the per-trade cap; the daily-loss backstop.
- [INV-03] — UTC-day boundary for the kill-switch reset.

## Depends on

- [[position-sizing]] (order risk), [[order-model-and-brackets]], `signals/portfolio.py` correlation helper (Phase 2, on `main`), store `day_pl`/positions (from [[order-placement]]/[[reconciliation]]).

## Approach

`risk/limits.py`. Reuse the Phase 2 correlation grouping. Inject all state for
testability. Config defaults proposed below; confirm at cross-spec audit.

## Open questions

- **Daily-loss cap value** — propose **1.0% of start-of-day equity** (≈4 max-loss
  trades). Operator-overridable in config.
- Kill-switch reset — propose **UTC midnight** (INV-03-consistent) vs broker-day.
- `max_concurrent` / `max_book_risk` / correlation thresholds — propose defaults
  (e.g. 5 concurrent, 1.0% book risk, 2 per correlation group); confirm.
- Correlation source: reuse Phase 2's static/rolling correlation? Propose the same source as `portfolio.py`.

## Out of scope

- Monitoring responses ([[deviation-monitor]]), submission ([[order-placement]]).
