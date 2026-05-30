# Operator Acceptance — Start Here to Finish Fathom

**This is the resume point.** All engineering is done: PoC + Phases 1–5 are
code-complete (`mypy .` clean, 1179 tests green, 0 open PRs). What remains is **four
human/operator acceptance gates** that no code can close — they require external
services and judgment. This doc threads them into one ordered checklist with the
exact commands and prerequisites, so you can pick up cold.

> Status snapshot: see [`PLAYBOOK.md`](PLAYBOOK.md) Part 1. Method retrospective:
> see [`half-cycle-verdict.md`](half-cycle-verdict.md).

---

## The 4 gates, in order

| # | Gate | Issue | Blocks |
|---|---|---|---|
| 1 | **P2-T-08** daily watchlist → Discord | #59 | nothing (start here) |
| 2 | **P3-T-11** live demo execution loop | #86 | the go-live track record |
| 3 | **P4-T-06** admin-panel acceptance | #109 | the go-live track record |
| 4 | **P5-T-05** live cutover (real money) | #123 | gates 1–3 closed **positive** (INV-07) |

Gates 1–3 build the **demo track record** INV-07 requires. Gate 4 is the deliberate
go-live decision and must not happen until 1–3 are not just *run* but *convincingly
positive*.

---

## Prerequisites you must supply

From the current `.env` (audited 2026-05-30):

| Item | State | Needed for |
|---|---|---|
| `OANDA_API_TOKEN` + `OANDA_ACCOUNT_ID` + `ENV=demo` | ✅ set | everything |
| `data/fathom.db` (seeded approved-set + watchlist) | ✅ present | everything |
| `ANTHROPIC_API_KEY` | ❌ **you add** | gate 1 (news-risk/narration), gate 2 (pre-trade veto) |
| `DISCORD_WEBHOOK_URL` | ❌ **you add** | gate 1 (watchlist), gate 2 (deviation alerts) |
| a running **Hermes** instance (cron + Discord gateway) | ❌ **you set up** | gate 1 (the scheduled job) |
| `LIVE_TRADING_ENABLED` / `LIVE_RISK_FRACTION` / live token | ❌ **gate 4 only** | the cutover — leave unset until then |

**One-time setup before gate 1:** stand up Hermes, and set `ANTHROPIC_API_KEY` +
`DISCORD_WEBHOOK_URL` in the env. (Re-seed anytime with `fathom backtest` then
`fathom scan` if the DB goes stale.)

---

## Gate 1 — P2-T-08: daily watchlist → Discord (#59)

**Goal:** Hermes posts a ranked, Claude-enriched watchlist to Discord on schedule,
placing no orders (INV-01).

1. Register `hermes_integration/jobs/daily.md` in Hermes; grant it the Fathom CLI
   tools **`scan` / `watchlist` / `chart` only** (never `execute`).
2. Wire the prompts (`hermes_integration/prompts/news_risk.md`, `narration.md`) and
   the parsers (`parse_news_risk`, `should_use_fallback`).
3. Schedule `0 22 * * 1-5` (or your session-close hour).
4. **Accept when:** a coherent ranked watchlist + charts + Claude rationale lands in
   Discord over **≥5 consecutive weekday runs**; empty days post "no candidates"; a
   `skip` verdict vetoes; **no order is ever placed**; no secret in output.
5. Record the run log in [`phases/phase-2-results.md`](phases/phase-2-results.md).

Full procedure: `hermes_integration/jobs/daily.md → Operator runbook`.

---

## Gate 2 — P3-T-11: live demo execution loop (#86)

**Goal:** the deterministic execution gate runs end-to-end on the **demo** account.
*(This is the "Phase 3 testing" you deferred.)*

```bash
# with ANTHROPIC_API_KEY set (so the pre-trade veto can return proceed):
fathom scan --db-path data/fathom.db            # refresh the watchlist
fathom execute EUR_USD:D:BollingerReversion(20,2.0) --db-path data/fathom.db
scripts/run_monitor.py --instruments EUR_USD --db-path data/fathom.db   # always-on, separate terminal
fathom reconcile --db-path data/fathom.db
```

**Accept when, over a sustained demo run:** `fathom execute` places a **bracketed**
(SL+TP) demo order through the gate; a **re-run is idempotent** (no double-fill); the
monitor tracks the open position and a **deviation alert lands in Discord**;
`fathom reconcile` matches broker state after a restart; the daily-loss kill switch
halts new entries if the cap trips. No live endpoint touched (INV-07). Record in
[`phases/phase-3-results.md`](phases/phase-3-results.md).

---

## Gate 3 — P4-T-06: admin-panel acceptance (#109)

**Goal:** the read-only dashboard shows a coherent picture of the demo system.

```bash
streamlit run panel/app.py -- --db-path data/fathom.db
```

**Accept when:** the 5 views render real demo data — Charts (candles + entry/stop/
target overlays + attribution), Equity curve + drawdown, Blotter (positions / P&L /
risk-in-use vs limit), Watchlist (mirrors Discord), Deviation log — the **Refresh**
button re-ranks with no order placed, no secret is shown, timestamps are UTC. Confirm
over a sustained demo period. Record in [`phases/phase-4-results.md`](phases/phase-4-results.md).

---

## Gate 4 — P5-T-05: live cutover (real money) (#123) ⛔ INV-07-blocked

**Do not start until gates 1–3 are closed AND the demo P&L is convincingly
positive.** This is a judgment call, not a checkbox — see the honest caveat below.

Follow [`go-live-runbook.md`](go-live-runbook.md) exactly. The hard ordering:

1. set the live token in `.env`; `ENV=live`.
2. `fathom preflight --attest-track-record` → **must be GO**.
3. **only after GO**, set `LIVE_TRADING_ENABLED=true`. *(The flag IS the attestation
   record — never set it before a passing attested preflight.)*
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

## ⚠️ Honest caveat (read before gate 4)

INV-07's bar is not "the plumbing works" — it is a **sustained, positive demo
edge**. The backtest found a *thin* edge (10/72 combos approved, several marginal —
see [`phases/phase-1a-results.md`](phases/phase-1a-results.md)). So gates 1–3 can run
flawlessly and the system can **still** fail to clear the go-live bar if the demo
P&L isn't convincingly positive. That is the system working as designed (demo-first,
INV-07). The polish of a green, fully-built codebase is *not* evidence the edge holds
live — keep gate 4 skeptical. (See [`half-cycle-verdict.md`](half-cycle-verdict.md)
Root Cause D.)

---

## If you're an AI resuming this project

Read, in order: this file → [`PLAYBOOK.md`](PLAYBOOK.md) (status + method) →
[`invariants.md`](invariants.md) (INV-01…16) → [`architecture-overview.md`](architecture-overview.md).
Engineering is complete; do **not** write go-live/live-token code or flip any live
switch — gate 4 is operator-only and INV-07-blocked. If asked to "continue the
project," the only remaining work is supporting the operator through the four gates
above (or net-new phases the operator explicitly scopes).
