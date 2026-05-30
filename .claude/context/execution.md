# Execution context

## P5-T-02 — live-trading-gate — 2026-05-30 (feat/p5-T-02-livegate)

**What was done:**

The real-money safety gate (defense-in-depth). Nothing connects live; the demo
path is byte-identical to Phase 3.

- `execution/live_gate.py` (NEW, PURE — no I/O, no clock, no network):
  - `LiveTradingBlocked(Exception)` — the refuse signal.
  - `assert_live_allowed(*, settings, preflight_report, confirmed) -> None` —
    demo (`env != "live"`) is a **no-op**; live raises `LiveTradingBlocked`
    (naming the FIRST failing gate) unless ALL four hold:
    `env == "live"` AND `live_trading_enabled is True` AND
    `isinstance(preflight_report, PreflightReport)` with `.go is True` AND
    `confirmed is True`. **B-1 default-refuse:** `preflight_report` that is
    `None` / not a `PreflightReport` / `.go` not exactly `True` → refuse.
    `preflight_report` typed as `object` so a malformed value refuses (never a
    `TypeError`).
  - `effective_risk_fraction(settings) -> float` — `live_risk_fraction` on live,
    else `DEFAULT_RISK_FRACTION` (0.0025). The ONLY env-aware fraction selector.

- `cli.py::cmd_execute` wiring (the live gate runs as "Step 2.5", after reconcile,
  before pretrade/sizing/submit):
  - On `settings.env == "live"`: `run_preflight(...)` wrapped so **any exception →
    refuse** (non-zero exit, no order, never GO); then a **typed account-id
    confirmation** (operator types `oanda_account_id`) that is **NOT** guarded by
    `--yes`/`skip_confirm` (N-3); then `assert_live_allowed(...)`; any
    `LiveTradingBlocked` → print reason, exit non-zero, no order.
  - The existing `[y/N]` confirm is now **demo-only**:
    `if settings.env != "live" and not skip_confirm:`.
  - The single `size_position(...)` call now passes
    `risk_fraction=effective_risk_fraction(settings)` instead of the hard-coded
    `DEFAULT_RISK_FRACTION` (B-2). Demo → 0.0025 (numerically unchanged).
  - Dropped the now-unused `DEFAULT_RISK_FRACTION` from the cli import.

**INV-09 (operator-boundary clause):** `live_gate.py` + the `cmd_execute`/`preflight`
cli wiring are the sanctioned readers of `settings.env` / `live_trading_enabled` /
`live_risk_fraction`. Mechanics (`risk/sizing.py`, `execution/orders.py`,
`execution/reconcile.py`, `monitoring/watcher.py`) contain NO env branch — pinned
by an enforcement test that token-scans those files (comments/strings stripped via
`tokenize`, so a docstring saying "never reads env" does not trip it).

**Tests** `tests/test_live_gate.py` (54 tests): full 16-row truth table; B-1 rows
(None / non-PreflightReport incl. duck-typed `.go==True` / `.go` non-True);
demo no-op; `effective_risk_fraction` live/demo; INV-05 settings `Field(gt=0, le=0.0025)`
bounds; demo `cmd_execute` unchanged (no preflight/confirm, size gets 0.0025); live
`--yes` still prompts typed confirm + threads 0.001; wrong/empty confirm → refuse no
order; `run_preflight` exception → refuse no order never GO; flag-false → refuse;
INV-09 source-scan. No live token/endpoint anywhere (INV-07/08). `Settings` stub is a
`cast("Settings", SimpleNamespace(...))`.

**Results:** whole-repo `mypy` 0 errors; full `pytest` 1190 passed (1136 pre-existing
+ 54 new). Settings live fields (`live_trading_enabled`, `live_risk_fraction`) already
landed in P5-T-01/T-03 — no settings.py change needed here.
