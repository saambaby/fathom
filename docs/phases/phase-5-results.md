# Fathom Phase 5 — Results

**Date:** 2026-05-30
**Verdict:** ✅ **Go-live guardrails code-complete and verified demo-safe.** The
defense-in-depth gate refuses by default, `fathom preflight` returns NO-GO without an
explicit track-record attestation, and the reduced live size is wired and
cap-validated. ⛔ **The actual live cutover (P5-T-05) is NOT done and is
INV-07-blocked** — it requires a recorded, positive demo track record (Phase 2 T-08,
Phase 3 T-11, Phase 4 T-06 acceptances closed) and is an operator-only, deliberate
step. **Nothing in this phase connects to the live endpoint or was flipped to live.**

This is the go-live phase (product-spec Phase 6). Its purpose was to replace "one
`ENV=live` slip = real-money orders" with a deliberate, multi-gate, small-size,
reviewed cutover — and to do so **without going live**.

---

## What shipped (all merged to `main`)

| Task | Unit | Model | PR |
|---|---|---|---|
| P5-T-01 | `config/settings.py` flags (`live_trading_enabled=False`, `live_risk_fraction=Field(gt=0,le=0.0025)`) + `risk/limits.py::kill_switch_armed` extraction | sonnet | #125 |
| P5-T-03 | `execution/preflight.py::run_preflight` + `fathom preflight` GO/NO-GO (read-only) | sonnet | #126 |
| P5-T-02 | `execution/live_gate.py` (four-gate `assert_live_allowed`, `effective_risk_fraction`) + `fathom execute` live gate | **opus** | #127 |
| P5-T-04 | `docs/go-live-runbook.md` — the deliberate cutover procedure (artifact + lint tests) | sonnet | #128 |
| P5-T-05 | live cutover | n/a | ⛔ **operator-only, INV-07-blocked** |

Health on assembled `main`: `mypy .` strict = **0 errors (92 files)**; `pytest` =
**1179 passed**; no new runtime dependency.

Every PR passed a fresh, independent read-only reviewer. The real-money gate (T-02)
was reviewed with maximal skepticism; the reviewer ruled the four-gate truth table
genuine, found no live-order-leak path, and explicitly ruled the execute-time
attestation auto-pass **sound** (the `live_trading_enabled` flag is the persisted
evidence of the prior attested preflight ceremony — see the T-02 review + the
runbook's hard-ordering requirement). Reviews caught + fixed real issues before
merge: T-01's INV-05 `Field` bound, T-04's incorrect `fathom execute` syntax in the
cutover step.

---

## The four-gate defense-in-depth (D-P5-2)

A live order requires **all four**, independently — any one missing ⇒ refuse:
1. `ENV=live`
2. `live_trading_enabled=True` (default **False**)
3. a passing `fathom preflight` (account reachable, kill switch armed + not tripped, brackets/INV-04, env/flag/token consistency, operator track-record attestation)
4. an interactive typed confirmation (the operator types the `oanda_account_id`) — **not** bypassable by `--yes`

The gate logic is a **pure, default-refuse** `execution/live_gate.py` with an
exhaustive 16-combo truth table + bad-preflight rows (`None`/malformed/non-`True`
`.go` → refuse; `run_preflight` exception → refuse). Live sizing uses
`live_risk_fraction` (0.10%, validated ≤ the 0.25% INV-05 cap); the sizing/order
mechanics are unchanged demo vs live (INV-09, per the amended operator-boundary
clause — an enforcement test asserts no `env`-branch in sizing/orders/reconcile/monitor).

---

## Stack-assembly verification (2026-05-30 — demo-safe, nothing live)

- **`fathom preflight` (demo, no attestation) → NO-GO** — `track_record_attested`
  FAILs with the INV-07 reason; read-only (no order, no state write).
- **The live gate refuses by default** — with `env=live` but the flag off / no
  preflight / no confirm, `assert_live_allowed` raises `LiveTradingBlocked`
  ("`live_trading_enabled is not True`"). On demo it is a no-op.
- **Reduced size wired** — `effective_risk_fraction` returns `0.0025` on demo
  (byte-identical to Phase 3) and `0.001` on live; the `Field(le=0.0025)` validator
  rejects a `.env` ramp typo above the cap at startup.
- **INV-09 enforcement test green** — no `env`-aware branch in the mechanics.
- **Nothing live:** no test connects to `api-fxtrade.oanda.com`, no live token is
  required, the demo path is unchanged.

---

## Residual: P5-T-05 live cutover (operator-only, INV-07-blocked)

**Not done — and must not be done until the demo track record exists.** The
prerequisite (a recorded, positive demo track record — Phase 2 T-08, Phase 3 T-11,
Phase 4 T-06 acceptances closed, plumbing proven on fake money over a sustained demo
period) is **not yet met**.

**When it is,** the operator follows `docs/go-live-runbook.md`: set the live token +
`ENV=live` → `fathom preflight --attest-track-record` must be **GO** → **only then**
set `LIVE_TRADING_ENABLED=true` (the flag is the attestation record — never set it
before a passing attested preflight) → `fathom execute` one small candidate (typed
account-id confirm) at 0.10% → confirm bracketed fill + monitor + reconcile → record
the dated go/no-go decision. The agent never performs this.

---

## Bottom line

Phase 5 is **code-complete**: the go-live safety system (defense-in-depth gate,
preflight readiness check, reduced initial size, and the deliberate cutover runbook)
is merged, type-clean, tested, and verified to **refuse by default while connecting
nothing live**. Going live is now a deliberate, multi-gate, small-size, reviewed
operator action — no longer one env var from real money. The cutover itself remains
the operator's gated, INV-07-blocked decision.

**Project status:** PoC + Phases 1–5 are all code-complete. The remaining work is
the chain of **operator acceptance gates** — Phase 2 T-08 (Discord), Phase 3 T-11
(live demo loop), Phase 4 T-06 (panel), and finally Phase 5 T-05 (live cutover) —
which together constitute the demo track record INV-07 requires before real money.
