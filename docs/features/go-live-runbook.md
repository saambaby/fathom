# Feature: go-live-runbook

**Status.** ready
**Phase.** Phase 5
**Owner.** saambaby
**Last updated.** 2026-05-30

## Summary

The deliberate, reviewed go-live cutover procedure — a documentation/config artifact
(not code, like the Hermes job definition), written so that *when* the demo track
record justifies it, the operator can flip to live in one well-guarded, reversible
step. It encodes the INV-07 prerequisite, the gate sequence (the four
`live-trading-gate` gates + `fathom preflight`), the small-size start, the rollback
plan, and the monitoring-during-cutover plan. It is the single source of truth for
"how we go live" and the record of the go/no-go decision.

## User-facing behaviour

A markdown runbook (e.g. `docs/go-live-runbook.md` or
`hermes_integration/jobs/`-style) with:

- **Prerequisites (INV-07 — hard gate):** the demo track record is recorded and
  positive — Phase 2 (T-08 Discord), Phase 3 (T-11 live demo loop), Phase 4 (T-06
  panel) operator acceptances closed with results; the plumbing has proven reliable
  on fake money over a sustained demo period. **The cutover does not proceed until
  every box is ticked.**
- **The cutover sequence (operator-only):** set the live token in `.env`; `ENV=live`;
  run `fathom preflight --attest-track-record` → must be **GO**; enable
  `live_trading_enabled=True`; `fathom execute` a single small candidate (typed
  confirmation) at `live_risk_fraction` (0.10%); watch it fill + bracket;
  `scripts/run_monitor.py` running; `fathom reconcile` matches broker truth.
- **Small-size start + ramp:** begin at 0.10% (`LIVE_RISK_FRACTION=0.001` in `.env`);
  ramp toward 0.25% only after a documented live track record, by editing
  `LIVE_RISK_FRACTION` — the settings-time `Field(le=0.0025)` validator rejects any
  ramp typo above the INV-05 cap at startup. The ramp is a deliberate operator
  decision, never automatic.
- **Rollback:** how to immediately stand down — set `live_trading_enabled=False`
  (instant: the gate refuses), `ENV=demo`, flatten/close open live positions via the
  operator path, and the daily-loss kill switch as the automated backstop.
- **Monitoring during cutover:** the deviation monitor + Discord alerts live; what to
  watch (slippage, adverse path, feed health) for the first live trades.
- **Go/No-Go decision record:** a place to record the dated, reviewed decision and
  who signed off.

## Acceptance criteria

- [ ] The runbook states the INV-07 prerequisite as a hard gate (cutover blocked until the demo track record is recorded + positive) and lists the specific closed acceptances required (T-08, T-11, T-06).
- [ ] The cutover sequence references **only** the real, shipped controls: `ENV`, `live_trading_enabled`, `fathom preflight`, `fathom execute` (typed confirm), `live_risk_fraction`, `scripts/run_monitor.py`, `fathom reconcile`. No invented commands.
- [ ] A rollback/stand-down procedure exists (flag-off is instant; kill switch is the automated backstop) and a small-size-start + manual-ramp policy is documented.
- [ ] The monitoring-during-cutover plan and a dated go/no-go decision-record section are present.
- [ ] It is explicit that going live is **operator-only and deliberate** — no automated step performs the cutover (INV-07).

## Component design

Pure documentation — the capstone artifact. It is verified by an artifact lint
(the referenced commands/flags exist; the INV-07 + rollback + decision-record
sections are present), mirroring how the Phase 2 `daily.md` Hermes job was
"configured, not coded." It composes the controls built by [[preflight-check]] +
[[live-trading-gate]] into the human procedure.

## Non-goals

- No code, no live connection (operator-only, INV-07). It documents; it does not execute.
- No auto-ramp / auto-cutover.

## Touches

- [INV-07] — the procedure is the embodiment of demo-first; the prerequisite gate is its first section.
- [INV-05] — documents the small-size start (≤ the cap) + manual ramp.

## Depends on

- [[preflight-check]] + [[live-trading-gate]] (the controls it sequences) — drafted/built first so the runbook references real commands.

## Approach

Written last (the capstone), after the gate + preflight ship, so every referenced
command is real. Artifact-lint verification + operator review.

## Open questions

- Location — `docs/go-live-runbook.md` vs `hermes_integration/`-style. Propose `docs/`
  (it's an operator procedure, not a Hermes job).

## Out of scope

- The gate/preflight code (their own specs); the actual cutover (operator-only, INV-07).
