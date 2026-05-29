# Feature: panel-data-layer

**Status.** draft
**Phase.** Phase 4
**Owner.** saambaby

**Last updated.** 2026-05-29

## Summary

The read-only query + view-model layer the admin panel renders. `panel/data.py`
composes the store's loaders into the exact shapes each panel view needs — blotter
rows, the equity series, watchlist rows, deviation-log rows, and per-pair chart
data. Keeping this separate from the Streamlit app gives the data logic real unit
tests (the Streamlit view code is hard to test), and makes the app a thin view over
tested view models. **Read-only** — it never writes, never places orders, never
imports `execution/orders.py` or `risk/` sizing/placement (it may *read* the limits
book-risk sum).

## User-facing behaviour

Backend module `panel/data.py`. A `PanelData(store)` (or module functions) exposing
pydantic/dataclass view models + loaders:

- `equity_series(since=None) -> list[EquityPoint]` — from `load_equity_snapshots`; each point `(as_of, equity, day_pl, drawdown)`. **`drawdown = (running_peak − equity) / running_peak`** (a fraction ≥ 0; **0 at a new peak**) — A-01 resolution.
- `blotter() -> BlotterView` — open positions (`load_open_positions`), each surfacing its **reconciled** `unrealized_pl` (a **passthrough** from the `Position`; NOT recomputed and NO live-price call — INV-16/read-only, D-05); plus `day_pl` + `start_of_day_equity` (`load_account_state`) and **risk-in-use vs limit**: `risk_in_use = book_risk_sum(open_positions)` and `risk_budget = book_risk_budget(equity, cfg)` (= `max_book_risk × equity`), **both read-only-reused from `risk/limits.py`** (the extracted helpers — see D-02 below), so the panel figure matches the kill switch exactly.
- `watchlist() -> list[Candidate]` — the latest persisted `load_watchlist()` (INV-13 shape, unchanged).
- `deviation_log(limit=...) -> list[DeviationRow]` — from the shipped `load_deviation_log(*, limit=None)` (already newest-first).
- `chart_data(instrument, timeframe) -> ChartData` — candles (`load_candles`) + overlays for that instrument, shaped for the Lightweight Charts component. **Overlay precedence (A-02):** if an open `Position` exists → the **"active"** overlay (entry/stop/target from the position); if a watchlist `Candidate` exists → the **"proposed"** overlay; when both exist, include **both** with distinct styling. Keep the dimension name `timeframe` end-to-end (INV-13); map to `load_candles`'s `granularity` argument only at the store call (D-05).

**Store loaders this adds (DRIFT-03 — ownership + serialization).** This spec owns
**`load_fills(*, limit: int | None = None) -> list[Fill]`** — newest-first by
`filled_at`, reconstructing the frozen INV-14 `Fill` (mirroring
`get_fill_by_client_order_id`). It **consumes** `load_equity_snapshots` (owned by
[[equity-snapshots]]). Both specs edit the shipped `data/store.py` → the edits are
**serialized, not parallel**: [[equity-snapshots]] lands first (it owns the
`equity_snapshots` table + `write_equity_snapshot`/`load_equity_snapshots`), then
this spec adds `load_fills`. Note loaders use different timestamp keys
(`equity_snapshots.as_of` vs `fills.filled_at` vs `deviation_log.created_at`) — the
view models must not assume one uniform key.

## Acceptance criteria

- [ ] Every accessor is **read-only** — `panel/data.py` performs no writes and imports no order/execution/sizing/placement capability (no `execution.orders`, no `risk.sizing`, no `submit_order`/`build_bracket`). A test asserts this.
- [ ] `equity_series` returns points ordered by `as_of` with `drawdown = (running_peak − equity)/running_peak` (fraction ≥ 0, **0 at a new peak**); verified on a seeded fixture incl. a drawdown after a new peak.
- [ ] `blotter()` returns open positions with the **reconciled** `unrealized_pl` (passthrough, not recomputed), today's `day_pl`, `risk_in_use = book_risk_sum(open_positions)`, and `risk_budget = book_risk_budget(equity, cfg)` (= `max_book_risk × equity`) — both reused from the extracted `risk/limits.py` helpers so the figure matches the kill switch exactly.
- [ ] `watchlist()` returns the latest run's `Candidate[]` unchanged (INV-13 shape; a round-trip against a seeded watchlist).
- [ ] `deviation_log()` returns rows newest-first with UTC timestamps; `chart_data()` returns candles plus the entry/stop/target/signal overlay values for the instrument.
- [ ] All timestamps surfaced are UTC RFC-3339 (INV-03); no secret is read into a view model or logged (INV-08); reads the demo store only (INV-07).
- [ ] Unit-tested against a **seeded SQLite store** (no Streamlit, no live HTTP) — the view models are the tested seam.

## Component design

`panel/data.py` depends only on `data/store.py` (loaders), `signals/ranker.Candidate`,
`execution/models` (`Position`/`Fill`), and **read-only** calls into `risk/limits.py`
for `book_risk_sum`/`book_risk_budget` (the exposure figures — **never**
`check_limits`, which constructs an order decision). View models are small frozen
dataclasses/pydantic. Pure functions over loaded rows so each is unit-testable with
a seeded store fixture. `panel/data.py` must NOT import `execution.orders`,
`execution.models.build_bracket`, `risk.sizing`, or `cli` — directly or transitively
(INV-01 transitive boundary; a test asserts it).

**DRIFT-02 — `book_risk_sum`/`book_risk_budget` must be extracted first.** Today
`risk/limits.py` has the reusable per-position `position_risk(position)`, but the
**sum** is inlined inside `check_limits` (`current_book_risk = sum(position_risk(p)
…)`, no standalone function) and the budget is the local `max_book_risk × equity`. A
**coordinator pre-step** extracts `book_risk_sum(open_positions) -> float` and
`book_risk_budget(equity, cfg) -> float` into `risk/limits.py`, with `check_limits`
calling them back (behaviour-preserving; re-run the Phase 3 limits tests) — directly
analogous to the T-02 correlation-primitive extraction. This spec depends on that
extraction.

## Non-goals

- No Streamlit / rendering — that is [[admin-panel]].
- No writes, no scan trigger (the app owns the refresh button), no order path (INV-01).

## Touches

- [INV-01] — read-only; no order/execution capability.
- [INV-03] — UTC timestamps in every view model.
- [INV-13] — renders the frozen `Candidate` unchanged.
- [INV-14/16] — reads the frozen `Position`/`Fill` + reconciled equity as broker-truth.

## Depends on

- [[equity-snapshots]] (the `equity_snapshots` table + `load_equity_snapshots`; lands first on `data/store.py`), the **`book_risk_sum`/`book_risk_budget` extraction** in `risk/limits.py` (coordinator pre-step), shipped store loaders (`load_open_positions`, `load_account_state`, `load_watchlist`, `load_deviation_log`, `load_candles`), `signals/ranker.Candidate`, `execution/models`.

## Approach

Build `panel/data.py` + the new `load_fills` loader (serialized after
[[equity-snapshots]]'s store edits); unit-test every view model against a seeded
store. The drawdown formula and the `book_risk_sum`/`book_risk_budget` reuse are the
two bits with real logic — test them hardest.

## Open questions

**Resolved at cross-spec audit (2026-05-29):** D-02 — `risk_in_use` reuses the
extracted `book_risk_sum`/`book_risk_budget` (no recompute, matches the kill switch);
D-03 — `load_fills(*, limit=None) -> list[Fill]` (newest-first), store.py edits
serialized after [[equity-snapshots]]; D-05 — `unrealized_pl` is a reconciled
passthrough (no live price), `timeframe` dimension kept end-to-end; A-01 drawdown
formula + A-02 overlay precedence pinned above.

## Out of scope

- The Streamlit app + charts component ([[admin-panel]]).
