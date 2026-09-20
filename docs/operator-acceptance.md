# Operator Acceptance — Start Here to Finish Fathom

**This is the resume point.** All engineering is done: PoC + Phases 1–5 are
code-complete (`mypy .` clean, 1179 tests green, 0 open PRs). What remains is **three
human/operator acceptance gates** that no code can close — they require external
services and judgment. This doc threads them into one ordered checklist with the
exact commands and prerequisites, so you can pick up cold.

> Status snapshot: see [`phases-manifest.json`](phases/phases-manifest.json). Method retrospective:
> see [`half-cycle-verdict.md`](reference/half-cycle-verdict.md).

---

## The 3 gates, in order

> **P2-T-08 superseded:** the daily Discord watchlist gate is retired by phase-07
> teardown. Acceptance transfers to the phase-07 pine/analyze walk (`fathom pine`,
> then `fathom analyze` once it ships).

| Gate | Issue | Blocks |
|---|---|---|
| **P3-T-11** live demo execution loop | #86 | the go-live track record |
| **P4-T-06** admin-panel acceptance | #109 | the go-live track record |
| **P5-T-05** live cutover (real money) | #123 | T-11 + T-06 closed **positive** **plus** the phase-07 pine/analyze acceptance walk (INV-07) |

T-11 and T-06, together with the phase-07 pine/analyze walk, build the **demo
track record** INV-07 requires. T-05 is the deliberate go-live decision and must
not happen until those are not just *run* but *convincingly positive*.

---

## Prerequisites you must supply

From the current `.env` (audited 2026-05-30):

| Item | State | Needed for |
|---|---|---|
| `OANDA_API_TOKEN` + `OANDA_ACCOUNT_ID` + `ENV=demo` | ✅ set | everything |
| `data/fathom.db` (seeded approved-set + watchlist) | ✅ present | everything |
| `LLM_API_KEY` (+ optional `LLM_BASE_URL` / `LLM_MODEL`) | ❌ **you add** | T-11 (in-process pre-trade veto — any OpenAI-compatible provider) |
| `DISCORD_WEBHOOK_URL` | ❌ **you add** | T-11 (deviation alerts) |
| `LIVE_TRADING_ENABLED` / `LIVE_RISK_FRACTION` / live token | ❌ **T-05 only** | the cutover — leave unset until then |

**One-time setup before T-11:** set `LLM_API_KEY` for the in-process veto and
`DISCORD_WEBHOOK_URL` for deviation alerts. (Re-seed anytime with `fathom backtest`
then `fathom scan` if the DB goes stale.)

---

## P3-T-11: live demo execution loop (#86)

**Goal:** the deterministic execution gate runs end-to-end on the **demo** account.
*(This is the "Phase 3 testing" you deferred.)*

```bash
# with LLM_API_KEY set (so the pre-trade veto can return proceed;
# LLM_BASE_URL/LLM_MODEL default to https://api.openai.com/v1 + gpt-5-nano):
fathom scan --db-path data/fathom.db            # refresh the watchlist
fathom execute "EUR_USD:D:BollingerReversion(20,2.0)" --db-path data/fathom.db
python scripts/run_monitor.py --instruments EUR_USD --db-path data/fathom.db   # always-on, separate terminal
fathom reconcile --db-path data/fathom.db
```

**Accept when, over a sustained demo run:** `fathom execute` places a **bracketed**
(SL+TP) demo order through the gate; a **re-run is idempotent** (no double-fill); the
monitor tracks the open position and a **deviation alert lands in Discord**;
`fathom reconcile` matches broker state after a restart; the daily-loss kill switch
halts new entries if the cap trips. No live endpoint touched (INV-07). Record in
[`phases/phase-3-results.md`](phases/phase-03/results.md).

---

## P4-T-06: admin-panel acceptance (#109)

**Goal:** the read-only dashboard shows a coherent picture of the demo system.

```bash
streamlit run panel/app.py -- --db-path data/fathom.db
```

**Accept when:** the 5 views render real demo data — Charts (candles + entry/stop/
target overlays + attribution), Equity curve + drawdown, Blotter (positions / P&L /
risk-in-use vs limit), Watchlist, Deviation log — the **Refresh**
button re-ranks with no order placed, no secret is shown, timestamps are UTC. Confirm
over a sustained demo period. Record in [`phases/phase-4-results.md`](phases/phase-04/results.md).

---

## P5-T-05: live cutover (real money) (#123) ⛔ INV-07-blocked

**Do not start until T-11 + T-06 are closed positive AND the phase-07 pine/analyze
acceptance walk is closed AND the demo P&L is convincingly
positive.** This is a judgment call, not a checkbox — see the honest caveat below.

Follow [`go-live-runbook.md`](go-live-runbook.md) exactly. The hard ordering:

1. set the live token in `.env`; `ENV=live` (leave `LIVE_TRADING_ENABLED` unset).
2. `fathom preflight --attest-track-record --pre-cutover` → **must be GO**.
   *(`--pre-cutover` is what makes this step passable while the flag is still off;
   such a GO is recorded as pre-cutover and never authorizes an order.)*
3. **only after that GO**, set `LIVE_TRADING_ENABLED=true`.
3b. re-run `fathom preflight --attest-track-record` (no `--pre-cutover`) → **must be
   GO**. This writes the `preflight_attestations` row that `fathom execute` checks;
   live execution is refused unless that row is an attested, non-pre-cutover GO for
   the configured account and **less than 24h old** (re-run if it ages out).
4. `fathom execute <instrument>:<timeframe>:<strategy_name>` — one small candidate,
   type the account id to confirm — sizes at `LIVE_RISK_FRACTION` (0.10%).
5. confirm bracketed fill + `run_monitor.py` + `fathom reconcile`; record the dated
   go/no-go decision.
6. **Rollback any time:** `LIVE_TRADING_ENABLED=false` (instant — the gate refuses),
   `ENV=demo`, flatten open positions; the daily-loss kill switch is the automated
   backstop.

**The agent never performs this step.** Create `phases/phase-5-results.md`'s live
section / a go-live decision record when done.

---

## ⚠️ Honest caveat (read before T-05)

INV-07's bar is not "the plumbing works" — it is a **sustained, positive demo
edge**. The backtest found a *thin* edge (10/72 combos approved, several marginal —
see [`phases/phase-1a-results.md`](phases/phase-01.1/results.md)). So T-11, T-06, and
the pine/analyze walk can run
flawlessly and the system can **still** fail to clear the go-live bar if the demo
P&L isn't convincingly positive. That is the system working as designed (demo-first,
INV-07). The polish of a green, fully-built codebase is *not* evidence the edge holds
live — keep T-05 skeptical. (See [`half-cycle-verdict.md`](reference/half-cycle-verdict.md)
Root Cause D.)

---

## If you're an AI resuming this project

Read, in order: this file → [`phases-manifest.json`](phases/phases-manifest.json) (status) →
[`invariants.md`](product/invariants.md) (INV-01…16) → [`architecture-overview.md`](product/architecture.md).
Engineering is complete; do **not** write go-live/live-token code or flip any live
switch — T-05 is operator-only and INV-07-blocked. If asked to "continue the
project," the only remaining work is supporting the operator through the three gates
above (or net-new phases the operator explicitly scopes).
