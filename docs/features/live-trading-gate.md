# Feature: live-trading-gate

**Status.** draft
**Phase.** Phase 5
**Owner.** saambaby
**Last updated.** 2026-05-30

## Summary

The real-money safety gate. Today `ENV=live` alone would place live orders; this
feature makes a live order require **four independent gates**, all of which must
pass: `ENV=live` **AND** `live_trading_enabled=True` (default False) **AND** a
passing `fathom preflight` **AND** an interactive typed confirmation. It also
selects a **reduced live position size** (`live_risk_fraction`, default 0.10% ≤ the
0.25% INV-05 cap). Demo is unchanged — no new friction. This is the highest-stakes
code in the system: a bug here is an accidental real-money trade, so the gate logic
is a pure, exhaustively-tested module and the default is always "refuse."

## User-facing behaviour

- `config/settings.py` adds: `live_trading_enabled: bool = False`; `live_risk_fraction: float = 0.001` (0.10%), documented; validated `0 < live_risk_fraction <= 0.0025` (never above the INV-05 cap).
- `execution/live_gate.py` (pure):
  - `assert_live_allowed(*, settings, preflight_report, confirmed: bool) -> None` — raises `LiveTradingBlocked(reason)` unless **all** hold: `settings.env == "live"`, `settings.live_trading_enabled`, `preflight_report.go`, and `confirmed`. The reason names the **first** failing gate. On demo (`env != "live"`) it is a no-op (demo path unchanged).
  - `effective_risk_fraction(settings) -> float` — `live_risk_fraction` when `env=="live"`, else the demo `DEFAULT_RISK_FRACTION` (0.0025). The **only** place the env-dependent fraction is selected; sizing itself is unchanged (INV-09).
- `fathom execute` (live context): runs `fathom preflight`, then requires a typed confirmation (the operator types the `oanda_account_id`); calls `assert_live_allowed(...)` before sizing; sizes with `effective_risk_fraction(settings)`. Any gate failing ⇒ refuse with the named reason + non-zero exit, **no order placed**. On demo, `execute` behaves exactly as Phase 3 (no preflight, no confirm, 0.25% fraction).

## Acceptance criteria

- [ ] `assert_live_allowed` raises `LiveTradingBlocked` (naming the failing gate) if ANY of {`env=="live"`, `live_trading_enabled`, `preflight.go`, `confirmed`} is false; it returns (allows) only when all four are true. Exhaustively unit-tested across the truth table.
- [ ] On demo (`env != "live"`), `assert_live_allowed` is a no-op and `fathom execute` is byte-identical to Phase 3 — no preflight, no confirmation prompt, no new friction (a test pins the demo path unchanged).
- [ ] `effective_risk_fraction` returns `live_risk_fraction` (≤ 0.0025) for live and `DEFAULT_RISK_FRACTION` for demo; settings validation rejects `live_risk_fraction > 0.0025` or ≤ 0 (INV-05 — live is never larger than the cap).
- [ ] `live_trading_enabled` defaults to **False**; `ENV=live` with the flag False ⇒ refuse. The four gates are independent (no single misconfiguration places a live order).
- [ ] A live `fathom execute` requires the typed confirmation (the account id); a wrong/empty confirmation ⇒ refuse, no order. `--yes` does NOT bypass the live confirmation (live always confirms; `--yes` only affects demo).
- [ ] No code path connects to the live endpoint in tests; the live token is never required to run the suite or logged (INV-07/08). The gate is pure + offline-testable (settings/report/confirmed injected).
- [ ] INV-09 preserved: `risk/sizing.py`/`execution/orders.py` mechanics are unchanged; only the *fraction input* and the *operator-boundary gate* are env-aware.

## Sequence diagram

```mermaid
sequenceDiagram
    actor OP as Operator
    participant CLI as fathom execute (live)
    participant PF as fathom preflight
    participant GATE as live_gate.assert_live_allowed
    participant SZ as sizing (effective_risk_fraction)
    participant EX as execution/orders

    OP->>CLI: fathom execute <candidate>  (ENV=live)
    CLI->>PF: run_preflight(...)
    PF-->>CLI: PreflightReport(go=?)
    CLI->>OP: type the account id to confirm
    OP-->>CLI: <typed confirmation>
    CLI->>GATE: assert_live_allowed(env, flag, preflight.go, confirmed)
    alt any gate fails
        GATE-->>CLI: raise LiveTradingBlocked(reason)
        CLI-->>OP: REFUSE (named gate), exit ≠ 0 — no order
    else all four pass
        CLI->>SZ: size with effective_risk_fraction = live_risk_fraction (0.10%)
        SZ->>EX: bracketed, idempotent order (same mechanics as demo)
        EX-->>OP: Fill
    end
```

## Component design

The gate logic is a **pure** `execution/live_gate.py` (no I/O) so the entire truth
table is unit-tested without the CLI or a broker. `fathom execute` (single-writer on
`cli.py`; this task adds the live gate, after [[preflight-check]] added `fathom
preflight`) wires: preflight → typed confirm → `assert_live_allowed` → size with
`effective_risk_fraction` → the unchanged Phase 3 submit path. The reduced fraction
flows into the **same** `size_position` (INV-09: mechanics identical; only the
fraction input differs, selected once at the boundary). Default-refuse everywhere:
the gate's bias is to block.

## Non-goals

- No live connection / token wiring (operator-only, deferred — INV-07). The gate is built + tested offline.
- No size-ramp automation (ramping past 0.10% is an operator decision on a live track record).
- No change to sizing/orders/reconcile/monitor mechanics (INV-09).

## Touches

- [INV-07] — the multi-gate that keeps live deliberate; the cutover stays operator-only.
- [INV-05] — `live_risk_fraction` validated ≤ 0.25% (never larger).
- [INV-09] — mechanics unchanged; the env-aware gate + fraction selection are a sanctioned operator-boundary layer (invariant-clarification candidate — see phase-5 open questions).
- [INV-08] — live token in `.env`, never logged.

## Depends on

- [[preflight-check]] (`run_preflight`/`PreflightReport` — the gate requires a passing preflight; also serializes the `cli.py` edit after it), `config/settings.py` (the new flags), `risk/sizing.py` (`DEFAULT_RISK_FRACTION`, `size_position` — unchanged), `execution/orders.py` (unchanged submit path), `cli.py` (`fathom execute`).

## Approach

Build the pure `live_gate` + the settings flags + validation first (exhaustive
truth-table tests, demo-noop test, INV-05 validation test), then wire `fathom
execute` (live: preflight + confirm + gate + reduced fraction; demo: unchanged).
Never require the live token in tests.

## Open questions

- Confirm token = the `oanda_account_id` (proposed) vs a fixed `LIVE` string — lean account id (forces looking at *which* account).
- Should `effective_risk_fraction` also enforce an absolute notional ceiling, or just the fraction? Propose fraction-only for Phase 5; a notional ceiling later.

## Out of scope

- The readiness checks themselves ([[preflight-check]]); the cutover procedure ([[go-live-runbook]]); the actual live cutover (operator-only, INV-07).
