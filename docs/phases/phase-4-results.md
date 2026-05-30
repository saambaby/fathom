# Fathom Phase 4 — Results

**Date:** 2026-05-29
**Verdict:** ✅ **Code-complete — all 5 code/config units merged; the admin panel boots
headless against the demo store and serves cleanly, read-only (INV-01 enforced).**
⏳ **One residual:** P4-T-06 (the operator running the panel in a browser against
live demo data over a **sustained track record**) is a human-admin gate.

This is the admin-panel phase (product-spec Phase 5) — a **read-only** Streamlit
dashboard over the existing store + TradingView Lightweight Charts, with a
scan-refresh button. It adds **no** order authority: the panel cannot reach the
order path (a transitive-import boundary test enforces it), and execution stays the
operator CLI (`fathom execute`).

---

## What shipped (all merged to `main`)

| Task | Unit | Model | PR |
|---|---|---|---|
| C-A | `streamlit` + `streamlit-lightweight-charts` dependency (coordinator) | — | #103 |
| P4-T-01 | `signals/scan.py::run_scan` — order-free scan extracted from `cli.cmd_scan` (INV-01) | sonnet | #110 |
| P4-T-02 | `risk/limits.py::book_risk_sum`/`book_risk_budget` — extracted (behaviour-preserving) | opus | #111 |
| P4-T-03 | `equity_snapshots` table + reconcile append (additive) | opus | #112 |
| P4-T-04 | `panel/data.py` — read-only view models + `load_fills` (the tested seam) | sonnet | #113 |
| P4-T-05 | `panel/app.py` — Streamlit dashboard, 5 views + Lightweight Charts + scan-refresh | sonnet | #114 |
| P4-T-06 | panel acceptance (operator, sustained demo) | n/a | ⏳ operator gate |

Health on assembled `main`: `mypy .` strict = **0 errors (87 files)**; `pytest` =
**1040 passed**; the panel boots headless (`/_stcore/health` → `200 ok`).

Every PR passed a fresh, independent read-only reviewer. Three reviews returned
FAIL/WARN and the findings were fixed before merge:
- **T-01** — `fathom scan --instruments ALL` lost its live universe discovery when the
  scan core moved into the order-free `run_scan`. Fixed: `cmd_scan` (the argparse
  adapter, allowed to use `cli` helpers) does the live discovery and passes the
  resolved list to `run_scan`, which stays order-free.
- **T-04** — `equity_series` raised `ZeroDivisionError` on a negative first equity.
  Fixed: initialise `running_peak` to the first equity + guard the division.
- **T-05** — the clean-subprocess INV-01 boundary test was a **null test** (mocked
  `streamlit` but not `streamlit.runtime`, so it crashed and the assertion never
  ran). Fixed + verified it now genuinely detects a leak.

---

## Stack-assembly verification (2026-05-29)

The runbook's mandatory stack-assembly gate (Step 7) — beyond per-task unit tests.

- **The panel boots headless** against the seeded demo store:
  `streamlit run panel/app.py --server.headless true -- --db-path data/fathom.db`
  → `/_stcore/health` returns `200 ok`, the Streamlit page serves, **no tracebacks**
  in the log.
- **INV-01 read-only boundary verified live:** `import panel.app` in a clean
  subprocess → `execution.orders` **absent** from `sys.modules`; the UI exposes no
  execute/approve/order action; the only mutation is the order-free scan-refresh
  (`signals.scan.run_scan`). The boundary test (now functional) asserts this and was
  shown to flag a deliberately-injected `import execution.orders` as a LEAK.
- **The view layer is thin over the tested `panel/data.py`** — the data/query logic
  (drawdown, risk-in-use reuse of `book_risk_sum`/`book_risk_budget`, the frozen
  `Candidate`/`Fill`/`Position` reads) is unit-tested against a seeded store
  (45 + tests); the Streamlit app is verified by an `AppTest` smoke + the boundary
  test.

What is **not** machine-verifiable here: a human confirming the five views render a
coherent, useful picture of *real* demo trading over a sustained period — that is
T-06.

---

## Residual: P4-T-06 panel acceptance (operator gate)

A human-admin gate. **To close it,** over a sustained demo period:
1. Seed the store with live demo activity (run `fathom backtest`/`scan`; execute a
   demo trade or two via `fathom execute`; let `scripts/run_monitor.py` run).
2. `streamlit run panel/app.py -- --db-path data/fathom.db` and confirm in the
   browser: charts render candles + entry/stop/target overlays + attribution; the
   equity curve + drawdown plot; the blotter shows positions / P&L / risk-in-use vs
   limit; the watchlist mirrors the Discord watchlist; the deviation log lists the
   monitor's alerts.
3. Confirm the refresh button re-ranks (no order placed), no secret is shown, and
   timestamps are UTC.
4. Confirm over the **sustained demo track record** (product-spec Phase 5 exit).
   Append the run notes here.

---

## Bottom line

Phase 4 is **code-complete and assembled** — the read-only admin panel (5 views +
Lightweight Charts + scan-refresh) is merged, type-clean, tested, and boots cleanly
against the demo store with the INV-01 read-only boundary enforced and verified.
What remains is an operator/ops acceptance (run it in a browser over demo days), not
engineering. Go-live (impl-Phase 5 / product-spec Phase 6) is a deliberate, separate
decision gated on the sustained demo track record (INV-07) and does not begin until
this acceptance is recorded.
