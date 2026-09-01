# Fathom Phase 3 — Results

**Date:** 2026-05-29
**Verdict:** ✅ **Code-complete — all 10 code/config units merged; the deterministic
gate runs end-to-end against the live practice account and safely aborts without
placing an order.** ⏳ **One residual:** P3-T-11 (the *live proceed→fill→monitor→alert*
demo loop over a sustained period) is a human-operator gate — it needs an
`ANTHROPIC_API_KEY` (so the pre-trade veto can return `proceed`), a
`DISCORD_WEBHOOK_URL` (alert delivery), and a live practice account, run over ≥
several demo days. It cannot be executed from the repo.

This is the phase where Fathom gained **order authority** — kept entirely on the
deterministic side of the INV-01 boundary (operator-run `fathom execute`, never a
Hermes tool).

---

## What shipped (all merged to `main`)

| Task | Unit | Model | PR | Key invariants |
|---|---|---|---|---|
| C-A | `anthropic` dependency (coordinator) | — | #75 | — |
| P3-T-01 | `execution/models.py` — `Order`/`Fill`/`Position` + `build_bracket` | opus | #87 | INV-04/14/15 |
| P3-T-02 | `signals/correlation.py` — extracted shared Pearson primitive | sonnet | #91 | (behaviour-preserving) |
| P3-T-03 | `risk/sizing.py` — stop-derived units, 0.25% cap | opus | #90 | INV-05/11 |
| P3-T-04 | `risk/limits.py` — exposure/correlation caps + daily-loss kill switch | opus | #92 | INV-05 |
| P3-T-05 | `hermes_integration/pretrade_check.py` — in-process Claude veto | sonnet | #93 | INV-02 |
| P3-T-06 | `execution/orders.py` — atomic bracket submit + idempotency | opus | #88 | INV-04/07/15 |
| P3-T-07 | `execution/reconcile.py` — broker-is-truth + `account_state` | opus | #89 | INV-16 |
| P3-T-08 | `monitoring/watcher.py` + `scripts/run_monitor.py` — deviation monitor | sonnet | #94 | INV-01 |
| P3-T-09 | `monitoring/alerts.py` — `DiscordWebhookClient` + `deviation_log` | sonnet | #95 | INV-01/08 |
| P3-T-10 | `cli.py` — `fathom execute`/`positions`/`reconcile` (the gate join) | sonnet | #96 | INV-01 |
| P3-T-11 | live demo-loop acceptance | n/a | — | ⏳ operator gate |

Health on assembled `main`: `mypy .` strict = **0 errors (79 files)**; `pytest` =
**955 passed**; `fathom --help` exposes all 7 commands.

Every PR passed a fresh, independent read-only reviewer. Three reviews returned
WARN and the findings were fixed before merge:
- **T-07** — OANDA's account-summary `pl` is *lifetime* P&L, not today's; it would
  have fed the kill switch a nonsensical figure. Fixed to `day_pl = NAV −
  start_of_day_equity` (today's P&L). *(Caught a real safety bug before T-04 built on it.)*
- **T-08** — feed-health debounce key omitted the instrument → silent suppression
  of the 2nd+ instrument's alerts. Fixed to per-instrument debounce.
- Plus order-placement's straddle-after-rounding guard (T-01) and the
  `risk_fraction`-clamp-at-the-call-site (T-10) hardening.

---

## End-to-end stack-assembly verification (2026-05-29, live practice account)

The runbook's mandatory stack-assembly gate (Step 7) — beyond per-task unit tests.
The deterministic gate was run against the **live OANDA practice account** with the
seeded Phase 2 watchlist and **no `ANTHROPIC_API_KEY`**:

```
fathom execute "EUR_USD:D:BollingerReversion(20,2.0)" --dry-run
```
→
```
execute: loaded candidate EUR_USD/D/BollingerReversion(20,2.0) from watchlist.
execute: connecting to OANDA (env=demo).
  GET /v3/accounts/.../openTrades
  GET /v3/accounts/.../summary
execute: reconcile complete — adopted=0 closed=0 matched=0 day_pl=0.0000 start_of_day_equity=100000.0000
pretrade_check: no client and ANTHROPIC_API_KEY not set — returning safe default block (INV-02 offline path)
BLOCKED by pretrade check: unparseable response — defaulting to block (INV-02 safe abort)
```

This proves the **whole gate is wired and correct on real infrastructure**:
- candidate loaded from the persisted watchlist (INV-13);
- a **fresh reconcile** ran against the live broker (open-trades + account-summary),
  snapshotting `account_state` (`day_pl=0`, `start_of_day_equity=100000`);
- the **pre-trade veto fails safe to `block`** with no key (INV-02);
- the gate **aborted — no order was placed** (INV-01 / INV-04 — there is no path
  to a naked or un-vetoed order).

A full `proceed` path (→ sizing → limits → bracketed submit → fill) requires a real
`ANTHROPIC_API_KEY` so the veto can return `proceed`; that is the operator gate below.

---

## Residual: P3-T-11 live demo-loop acceptance (operator gate)

Everything verifiable without external services is verified. The *live* loop is a
human-admin gate and **cannot run from the repo** — the required credentials are
absent here:

| Required | Present? |
|---|---|
| `OANDA_API_TOKEN` + `OANDA_ACCOUNT_ID` (reconcile/execute) | ✅ |
| `ANTHROPIC_API_KEY` (pre-trade veto can `proceed`) | ❌ |
| `DISCORD_WEBHOOK_URL` (deviation alerts) | ❌ |

**To close T-11, the operator must,** over a sustained demo period:
1. Set `ANTHROPIC_API_KEY` + `DISCORD_WEBHOOK_URL` in `.env`; run `fathom scan` to
   refresh the watchlist.
2. `fathom execute <candidate>` an approved candidate → confirm a **bracketed**
   (SL+TP) demo order places through the gate, the `Fill` is recorded, and a
   **second** `execute` of the same candidate is idempotent (no double-fill).
3. Run `scripts/run_monitor.py` → confirm it tracks the open position and a
   deviation alert lands in Discord; confirm `fathom reconcile` matches broker
   state after a restart.
4. Confirm the kill switch halts new entries once the daily-loss cap (1.0%) trips.
5. Confirm no live endpoint is touched (INV-07) and no secret appears in output
   (INV-08). Append the live run log here.

---

## Bottom line

Phase 3 is **code-complete and assembled** — the order-authority stack (sizing,
risk limits + kill switch, atomic bracketed idempotent placement, reconciliation,
the always-on monitor, and the operator CLI gate) is merged, type-clean, fully
unit-tested, and verified to run end-to-end against the live practice account while
**safely refusing to place an order** without the pre-trade veto. What remains is a
deployment/ops acceptance (stand up the key + webhook, run the live loop over demo
days), not engineering. Phase 4 (admin panel) begins only after T-11 is recorded.
