# Fathom Phase 4 — Cross-Spec Audit (2026-05-29)

Run per `runbook-cross-spec-audit` by a fresh, independent, read-only auditor (no
prior session context). Fixes applied afterward by the lead — each finding
annotated with its resolution. Audit + fixes landed together in one PR.

## Scope

The 3 Phase 4 specs (`equity-snapshots`, `panel-data-layer`, `admin-panel`),
cross-checked against `invariants.md`, `phase-4.md`, `code-map.md`, `INDEX.md`, and
the shipped contracts (`data/store.py` loaders + tables, `execution/reconcile.py`
NAV/day_pl, `risk/limits.py` book-risk, `signals/ranker.py::Candidate`,
`execution/models.py`, `cli.py::cmd_scan`).

## Summary

11 shared concepts · 5 consistent · **3 blocking** · 4 non-blocking · 1
invariant-promotion candidate. The headline finding (D-01) is a real INV-01
import-chain breach in the shipped code path the panel would call; the other
blockers are two missing extractions (mirroring the precedented T-02 pattern) and
under-specified loader/overlay contracts. No deeper rework.

## Drift findings & resolutions

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| D-01 | blocking | `cli.py` imports `execution.orders.submit_order`/`build_bracket`/`risk.*` at **module level**, so a panel doing `from cli import cmd_scan` would transitively pull the order path into its import graph — breaking the panel's own INV-01 boundary test. Also `cmd_scan(args: Namespace)` is argparse-coupled, not kwargs-callable. | **Fixed** — `admin-panel` now refreshes via an **order-free** `signals/scan.py::run_scan(...)` (a coordinator pre-step extraction; `cmd_scan` becomes a thin argparse adapter), never imports `cli`, and the INV-01 boundary test is **transitive**. Added to phase-4 coordinator pre-steps + code-map. |
| D-02 | blocking | `panel-data-layer` assumes a reusable book-risk **sum** in `risk/limits.py`; only the per-position `position_risk` is reusable — the sum is **inlined** in `check_limits` (line 400). | **Fixed** — a coordinator pre-step extracts `book_risk_sum(open_positions)` + `book_risk_budget(equity, cfg)`; `check_limits` calls them back (behaviour-preserving). `panel-data-layer` reuses them; the blotter surfaces `risk_in_use` + `risk_budget = max_book_risk × equity`. (Mirrors T-02.) |
| D-03 | blocking | `load_fills` doesn't exist + had no signature; `equity-snapshots` and `panel-data-layer` both edit shipped `data/store.py` with no serialization declared. | **Fixed** — `panel-data-layer` pins `load_fills(*, limit=None) -> list[Fill]` (newest-first, reconstructs the frozen `Fill`); store.py edits **serialized**: `equity-snapshots` owns the `equity_snapshots` table + 2 accessors and lands first, then `panel-data-layer` adds `load_fills`. Per-loader timestamp keys noted. |
| D-04 | non-blocking | `equity-snapshots` prose says "equity = nav" / "~line 535"; the shipped value is `broker.nav` (no bare `nav` local); append ordering vs `write_account_state` ambiguous. | **Fixed** — prose → `equity = broker.nav`; the append is pinned **strictly after `write_account_state`** (so a snapshot-write failure can't delay the kill-switch truth row); non-fatal guard kept. Additivity verdict: SOUND. |
| D-05 | non-blocking | `panel-data-layer` reads as if it computes `unrealized_pl`; it's a reconciled passthrough. Dimension-name `timeframe` vs store's `granularity`. | **Fixed** — `unrealized_pl` documented as a reconciled passthrough (no recompute, no live price — INV-16/read-only); `timeframe` kept end-to-end, mapped to `granularity` only at the `load_candles` call. |

## Ambiguity findings & resolutions

| ID | Finding | Resolution |
|---|---|---|
| A-01 | `equity_series` drawdown semantics (absolute vs fraction vs signed; value at peak) undefined | **Fixed** — `drawdown = (running_peak − equity)/running_peak` (fraction ≥ 0, **0 at a new peak**), pinned in `panel-data-layer` + `admin-panel`. |
| A-02 | `chart_data` overlay when an open position AND a watchlist candidate coexist for one pair — which wins? | **Fixed (lead ruling)** — draw **both**: position = "active" overlay, candidate = "proposed" overlay, distinct styling; neither hides the other. |

## Invariant promotion

- **IPC-01 → applied as an INV-01 enforcement clause** (not a new INV number): "No
  always-on UI/monitoring surface (`panel/`, monitor, future dashboards) may reach
  order-placement or risk sizing/placement — directly or transitively; the
  scan-refresh path uses the order-free `signals/scan.py::run_scan`, not
  `cli.cmd_scan`. Enforced by a transitive-import boundary test." Added to
  `docs/invariants.md` under INV-01.

## Consistent (no action)

`Candidate`/INV-13 shape (watchlist view renders it unchanged); `load_deviation_log`
signature (already newest-first, takes `limit`); `Position`/`Fill` frozen shapes
(INV-14) the blotter/chart read; `account_state` carries `day_pl` +
`start_of_day_equity`; the equity-snapshots reconcile-append additivity claim
(verified sound).

## Action plan — status

All 3 blocking + 2 non-blocking drifts and both ambiguities **Fixed** in this PR;
the IPC-01 INV-01 clause added. **Two coordinator pre-step extractions** surfaced for
the taskgraph: `signals/scan.py::run_scan` (from `cli.py`) and
`book_risk_sum`/`book_risk_budget` (from `risk/limits.py`) — both behaviour-preserving,
coordinator-serialized, before the panel fan-out.

**Sequencing:** coordinator pre-steps `{run_scan extraction, book_risk_sum
extraction, streamlit+lightweight-charts dep}` → `equity-snapshots` (store + reconcile
append) → `panel-data-layer` (`load_fills` + view models) → `admin-panel` (join).

**Verdict after fixes:** spec corpus coherent; **ready for taskgraph generation.**
