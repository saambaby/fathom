# Fathom Phase 5 — Cross-Spec Audit (2026-05-30)

Run per `runbook-cross-spec-audit` by a fresh, independent, read-only auditor (no
prior context). This phase touches **real money** — any ambiguity that could let a
live order slip through was treated as blocking. Fixes applied by the lead; each
finding annotated with its resolution. Audit + fixes landed together in one PR.

## Scope

The 3 Phase 5 specs (`preflight-check`, `live-trading-gate`, `go-live-runbook`),
cross-checked against `invariants.md` (INV-07/09/05/04/08), `phase-5.md`,
`code-map.md`, `INDEX.md`, and the shipped contracts (`config/settings.py`,
`risk/sizing.py`, `risk/limits.py`, `data/store.py`, `data/oanda_client.py`,
`cli.py::cmd_execute`).

## Summary

12 shared concepts · 5 consistent · **5 blocking** · 4 non-blocking · 2
invariant-promotion/clarification candidates. The four-gate design is sound; the
blockers are spec-level (no architectural rework) but include **two live-order-leak
holes** and an **unresolved INV-09 violation** — all fixed below.

## Drift findings & resolutions

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| B-1 | blocking | `assert_live_allowed` default-refuse undefined for `preflight_report` that is `None`/malformed/`.go`-non-True, or when `run_preflight` raises mid-gate — "safe by accident" is not acceptable for real money | **Fixed** — `live-trading-gate` now pins: a `None`/non-`PreflightReport`/non-exactly-`True` `.go` is a **failed** preflight gate (raise); `cmd_execute` catches any `run_preflight` exception → refuse (never GO). Truth-table tests add `None`/exception rows. |
| B-2 | blocking | `effective_risk_fraction` not actually threaded in — shipped `cmd_execute` (`cli.py:1528`) hard-codes `risk_fraction=DEFAULT_RISK_FRACTION`; a live trade would size at 0.25%, not the reduced 0.10% | **Fixed** — spec names the exact substitution `risk_fraction=effective_risk_fraction(settings)` at `cli.py:~1528` + an AC pinning demo receives `0.0025` and live receives `live_risk_fraction`. |
| B-3 | blocking | preflight's "armed and not tripped" has no shipped counterpart — `kill_switch_status` only reports tripped/not; `load_account_state` has no staleness flag | **Fixed** — `preflight-check` pins concrete semantics: armed ≡ `account_state` present + `as_of` fresh (10-min window) + `KillSwitchStatus.active is False`; extract a reusable `kill_switch_armed(account_state, now, config)` into `risk/limits.py` (single-source, à la `book_risk_sum`). |
| B-4 | blocking | the env-aware gate violates INV-09 as written ("no `if env=='live'` branches; only `oanda_client` reads env"); the phase deferred the decision instead of resolving it | **Fixed (P-1)** — **INV-09 amended** with an operator-boundary-gate enforcement clause: mechanics (`sizing`/`orders`/`reconcile`/monitor) stay env-free + single-path; the go-live gate (`live_gate.py` + cli wiring) is the one sanctioned `env`-reader for gate behaviour + the fraction *input*; a test asserts no env-branch in the mechanics. |
| B-5 | blocking | the `0 < live_risk_fraction <= 0.0025` bound was prose-only; shipped `Settings` has no such validator — a `.env` typo (`0.025`) would load a 10× cap breach | **Fixed** — pinned `live_risk_fraction: float = Field(default=0.001, gt=0.0, le=0.0025)` (mirrors `LimitsConfig`; `le` references the cap so they can't drift) + a both-bounds `ValidationError` AC. |

## Ambiguity findings & resolutions

| ID | Finding | Resolution |
|---|---|---|
| N-1 | preflight↔settings ordering hand-wavy (`getattr` hedge) | **Fixed** — build order pinned: gate's settings fields land first; preflight reads them directly, no `getattr`. |
| N-2 | two Phase-5 tasks edit `cli.py`; serialization not explicit | **Fixed** — `cli.py` serialized across exactly the two Phase-5 tasks: `fathom preflight` (preflight-check) then the `execute` gate (live-trading-gate); to be encoded in the taskgraph. |
| N-3 | risk an implementer reuses the `--yes`-gated confirm for live | **Fixed** — the live typed-account-id confirm is a **distinct prompt, not `--yes`-gated**; the existing `[y/N]` confirm stays demo-only; AC: live `execute --yes` still requires the typed account id. |
| N-4 | runbook ramp doesn't name the env key / validation | **Fixed** — names `LIVE_RISK_FRACTION` + notes the `Field(le=0.0025)` startup validation rejects a ramp typo above the cap. |

## Invariant promotion / clarification

- **P-1 → applied:** INV-09 amended (above, B-4) to sanction the operator-boundary
  go-live gate while keeping the mechanics single-path + env-free, with a
  no-env-branch-in-mechanics enforcement test.
- **P-2 → applied as a code-structure decision:** extract `kill_switch_armed` into
  `risk/limits.py` (single-source readiness read), referenced by `preflight-check`
  (B-3). Not a new invariant.

## Consistent (no action)

Confirm token = `oanda_account_id` (shipped plain `str`, safe to echo, not an INV-08
secret); `account_summary` reachability maps to the real `OandaClient.account_summary`;
`size_position(risk_fraction=…)` kwarg exists + correctly named; demo-path-unchanged
stated consistently across both code specs; INV-08 token-non-printing correct (only
`oanda_api_token` is `SecretStr`).

## Action plan — status

All 5 blocking + 4 non-blocking findings **Fixed** in this PR; INV-09 amended; the
`kill_switch_armed` extraction decided. Two coordinator-serialized shipped-file
edits surfaced for the taskgraph: the `kill_switch_armed` extraction
(`risk/limits.py`) and the `cli.py` two-task serialization (preflight command →
execute gate). Nothing in this phase connects live (INV-07).

**Verdict after fixes:** spec corpus coherent; **ready for taskgraph generation.**
