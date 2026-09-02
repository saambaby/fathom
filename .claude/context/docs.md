# Context: docs area

## P5-T-04 — go-live-runbook (2026-05-30)

### What was built

Created `docs/go-live-runbook.md` — the Phase 5 capstone documentation artifact:
the deliberate, reviewed go-live cutover procedure. It is prose only (no code),
verified by `tests/test_go_live_runbook.py` (40 artifact-lint checks).

### Key design decisions

**Critical ordering requirement (load-bearing):** The runbook is explicit that
the flag `LIVE_TRADING_ENABLED=true` must be set ONLY after a passing
`fathom preflight --attest-track-record` run. The flag IS the persisted
attestation record. This ordering is stated as a hard prerequisite, not a
suggestion, because `fathom execute` auto-passes the attestation check based on
the presence of the flag — the flag's existence is the ceremony's receipt.

**INV-07 hard gate:** Section 1 lists the three specific closed acceptances that
block the cutover: Phase 2 T-08 (Discord), Phase 3 T-11 (live demo loop), Phase
4 T-06 (panel). None are met as of 2026-05-30.

**Only shipped controls referenced:** The runbook uses only real commands:
`fathom preflight --attest-track-record`, `fathom execute`, `fathom positions`,
`fathom reconcile`, `scripts/run_monitor.py`, and the `.env` vars
`LIVE_TRADING_ENABLED`, `LIVE_RISK_FRACTION`, `ENV`. No invented commands.

**`Field(le=0.0025)` validator:** The runbook documents that this validator in
`config/settings.py` rejects `LIVE_RISK_FRACTION` above the INV-05 cap (0.25%)
at startup, making a ramp typo impossible to deploy accidentally.

### New files

- `docs/go-live-runbook.md` — the runbook
- `tests/test_go_live_runbook.py` — 40 artifact-lint checks

### CLAUDE.md updated

Added `docs/go-live-runbook.md` to the Documentation table in CLAUDE.md.

### No new dependencies, no new CLI commands.

## Spec sprint phases 07–10 (2026-09-01)

Recovered Claude Code worktree `claude/phase-7-10-spec-sprint-1acfe9` (7
commits: phase-07 five specs ready + INV-20/21/22) onto `main`. Closed the
sprint: `review-command` promoted; `journal`, `ask-command`, `veto-report`
authored. Phase-10 Layer-4 specs remain deferred to epic kickoff. INV-09
telemetry skip now includes `operator_journal`; INV-21 lists `fathom ask`
disclose-stale as a consumer.

Next Layer 5: `/halfcycle:taskgraph` per phase, starting pine-generation
(phase-07 riskiest-assumption first).

## Spec sprint close — push + worktree (2026-09-01, main)

Pushed `794d094..c8e14c1` to `origin/main`. Removed git worktree
`phase-7-10-spec-sprint-1acfe9` and local branch
`claude/phase-7-10-spec-sprint-1acfe9` (already an ancestor of main). Did not
merge leftover squash-PR branch tips (`docs/phase-09-veto-ledger`,
`fix/ws1-adapter`, …) — those already landed as GitHub squash merges.

**Gotchas from spec review (implementers):**
- `calendar_events` is **not** in `Store`'s CREATE list. `FairEconomyCalendar.__init__`
  runs `CREATE TABLE IF NOT EXISTS` + commit (mutates the DB file). `review` /
  `ask` must SELECT via Store and treat missing table as `[]`, never construct
  that client on the live store.
- No persisted `ReconcileReport` / `drift_flags`. `fathom review` reads
  last `account_state` + `as_of`, and must not call `reconcile()`.
- `TIMEFRAME_BAR_LENGTH` still lives in `cli.py` (~217). Analyze-command relocates
  it to `signals/timeframes.py`; `fathom ask` INV-21 stamps must import that
  leaf module, never `cli`.
- `fathom veto-report` CLI is owned by the veto-report spec; tracker exposes
  `refresh_counterfactuals` → `RefreshCounts` only (no exit-2 placeholder).
- `operator_journal` is a lifecycle UPSERT on `client_order_id` (INV-22
  exception), demo-only skip at `cmd_execute` (INV-09). Not append-only.

Uncommitted local noise in `tests/test_admin_panel.py` was left out of the
sprint commits (docstring/format churn, not part of the spec work).

