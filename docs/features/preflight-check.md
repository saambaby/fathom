# Feature: preflight-check

**Status.** ready
**Phase.** Phase 5
**Owner.** saambaby
**Last updated.** 2026-05-30

## Summary

A read-only go/no-go readiness check the operator runs before considering a live
cutover (and before any live `fathom execute`). It verifies the *mechanical*
prerequisites are in place — account reachable, kill switch armed and not tripped,
brackets/INV-04 enforceable, env↔flag↔token consistency — and requires an explicit
operator **track-record attestation** (it never auto-judges "edge quality"). It
prints a clear GO / NO-GO with per-check status and exits non-zero on NO-GO. It is
the readiness gate that `live-trading-gate` requires before a live order; it places
no orders and changes no state.

## User-facing behaviour

- `execution/preflight.py` — `run_preflight(*, settings, store, client=None, attested: bool = False) -> PreflightReport`:
  - **Account reachable** — an account-summary read succeeds (uses the injected client; on demo this hits practice, never live unless the operator has already set ENV=live).
  - **Kill switch (B-3 — concrete shipped semantics):** there is no "armed" field on the shipped API. Preflight loads `store.load_account_state()` and calls `risk.limits.kill_switch_status(day_pl=…, start_of_day_equity=…, config=LimitsConfig(), now=…)`. **"Armed and healthy" ≡ `account_state is not None` AND its `as_of` is within the staleness window (default 10 min of `now`) AND `KillSwitchStatus.active is False` (not tripped).** NO-GO if `account_state` is missing, stale, or the switch is tripped (`active is True`). For single-source reuse (à la `book_risk_sum`, P4-T-02), extract a small **`kill_switch_armed(account_state, now, config) -> (bool, reason)`** into `risk/limits.py` and have preflight call it (coordinator-serialized edit to the shipped `risk/limits.py`).
  - **Brackets/INV-04** — confirms the execution path enforces SL+TP (a static check that `build_bracket`/order submission can't produce a naked order — e.g. config/contract assertion).
  - **Env/flag/token consistency** — if `ENV=live`, a live-shaped token + `oanda_account_id` are present; `live_trading_enabled` state is reported; no demo/live mismatch.
  - **Track-record attestation** — `attested` must be True (the operator asserts the demo track record per INV-07); preflight **does not** judge edge quality itself.
  - Returns a `PreflightReport` with an overall `go: bool` and a list of per-check `(name, ok, detail)`.
- `fathom preflight [--db-path PATH] [--attest-track-record]` — runs it, prints each check + an overall **GO**/**NO-GO**, exits 0 on GO / non-zero on NO-GO. Read-only; places nothing.

## Acceptance criteria

- [ ] `run_preflight` returns `go=False` if ANY check fails, with the failing check(s) named in the report; `go=True` only when **all** mechanical checks pass **and** `attested=True`.
- [ ] Track-record attestation is required: `attested=False` → NO-GO with a reason pointing at INV-07 (preflight never green-lights live on its own).
- [ ] Kill-switch check is NO-GO when `account_state is None`, when its `as_of` is older than the staleness window (default 10 min), or when `kill_switch_status(...).active is True` (tripped); GO only when present + fresh + not tripped (`kill_switch_armed` returns True). A test pins each case.
- [ ] Env/flag/token consistency: `ENV=live` without a token/account or with `live_trading_enabled=False` is reported clearly (NO-GO or a flagged warning per the rules); demo is always internally consistent.
- [ ] `fathom preflight` exits 0 on GO, non-zero on NO-GO; prints per-check status; **places no order and writes no state** (read-only — a test asserts no order/write capability).
- [ ] No secret (token) is printed (INV-08); all timestamps UTC (INV-03).
- [ ] Pure/deterministic core — `run_preflight` takes injected `settings`/`store`/`client`; unit-tested against a seeded store + stub client (no live HTTP).

## Component design

`execution/preflight.py` is a pure orchestration over read-only inputs: it composes
existing read paths (`store.load_account_state`, `risk.limits.kill_switch_status` /
the armed-check, an account-summary read via the injected client) into a
`PreflightReport`. No order/sizing call. The `fathom preflight` CLI command wires it
(single-writer on `cli.py`; this task adds `preflight`, `live-trading-gate` later
adds the `execute` gate — serialized). Demo-safe: with `ENV=demo` it checks the
practice account; it never forces a live connection.

## Non-goals

- No enforcement — preflight *reports*; the actual live refusal is [[live-trading-gate]].
- No edge-quality judgement — the operator attests the track record (INV-07).
- No order placement, no state writes (read-only).

## Touches

- [INV-07] — surfaces the track-record attestation; the readiness half of the go-live gate.
- [INV-08] — never prints the token. [INV-03] — UTC timestamps.
- [INV-09] — reads `settings` at the operator boundary (readiness reporting), does not alter the mechanics.

## Depends on

- [[live-trading-gate]] (lands its settings fields **first** — preflight reads `live_trading_enabled` directly), `risk/limits.py` (`kill_switch_status` + the new `kill_switch_armed` helper this spec extracts — coordinator-serialized edit to the shipped file), `data/store.py` (`load_account_state`), `data/oanda_client.py` (`account_summary` read).

## Approach

Build order (N-1, resolved): [[live-trading-gate]]'s settings fields land **first**,
so this spec reads `live_trading_enabled`/`live_risk_fraction` **directly — no
`getattr` hedge**. Add the `kill_switch_armed` helper to `risk/limits.py`
(coordinator-serialized), then build the pure `run_preflight` + report (full offline
tests with a seeded store + stub client), then the thin `fathom preflight` CLI
command (the second Phase-5 `cli.py` edit, after the gate's — see N-2 in the
taskgraph).

## Open questions

- "brackets/INV-04" check — static contract assertion vs a dry-run order
  construction. Lean **static** (assert the order/bracket contract can't yield a
  naked order); confirm.

**Resolved at cross-spec audit (2026-05-30):** B-3 — concrete `kill_switch_status`
semantics + a defined 10-min staleness window + the `kill_switch_armed` extraction
(no phantom "armed" API); N-1 — build order pinned, `getattr` hedge dropped.
Attestation marker = the `--attest-track-record` CLI flag for Phase 5 (a persisted
signed-off record later).

## Out of scope

- The live refusal/gate ([[live-trading-gate]]); the cutover doc ([[go-live-runbook]]); the actual live connection (operator-only, INV-07).
