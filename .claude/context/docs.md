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
