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

- `equity_series(since=None) -> list[EquityPoint]` — from `load_equity_snapshots`; each point `(as_of, equity, day_pl)` + a computed running `drawdown` (peak-to-current).
- `blotter() -> BlotterView` — open positions (`load_open_positions`), each with `unrealized_pl`; plus `day_pl` + `start_of_day_equity` (`load_account_state`) and **risk-in-use vs the limit** (the book-risk sum, read-only-reused from `risk/limits.py`, against `max_book_risk`).
- `watchlist() -> list[Candidate]` — the latest persisted `load_watchlist()` (INV-13 shape, unchanged).
- `deviation_log(limit=...) -> list[DeviationRow]` — from `load_deviation_log`.
- `chart_data(instrument, timeframe) -> ChartData` — candles (`load_candles`) + the active/proposed entry/stop/target + signal marker for that instrument (from the open position and/or the watchlist candidate), shaped for the Lightweight Charts component.

Also adds any missing store loaders this needs: `load_fills` (recent fills, for the
blotter's fill/slippage context) and consumes the new `load_equity_snapshots`
([[equity-snapshots]]).

## Acceptance criteria

- [ ] Every accessor is **read-only** — `panel/data.py` performs no writes and imports no order/execution/sizing/placement capability (no `execution.orders`, no `risk.sizing`, no `submit_order`/`build_bracket`). A test asserts this.
- [ ] `equity_series` returns points ordered by `as_of` with a correct running `drawdown` (peak-to-current); verified on a seeded snapshot fixture (incl. a drawdown after a new peak).
- [ ] `blotter()` returns open positions with `unrealized_pl`, today's `day_pl`, and `risk_in_use` computed from the same stop-distance book-risk sum the kill switch uses (read-only reuse), plus the `max_book_risk` limit for the vs-limit display.
- [ ] `watchlist()` returns the latest run's `Candidate[]` unchanged (INV-13 shape; a round-trip against a seeded watchlist).
- [ ] `deviation_log()` returns rows newest-first with UTC timestamps; `chart_data()` returns candles plus the entry/stop/target/signal overlay values for the instrument.
- [ ] All timestamps surfaced are UTC RFC-3339 (INV-03); no secret is read into a view model or logged (INV-08); reads the demo store only (INV-07).
- [ ] Unit-tested against a **seeded SQLite store** (no Streamlit, no live HTTP) — the view models are the tested seam.

## Component design

`panel/data.py` depends only on `data/store.py` (loaders), `signals/ranker.Candidate`,
`execution/models` (`Position`/`Fill`), and a **read-only** call into
`risk/limits.py` for the book-risk sum (no `check_limits` order construction — just
the exposure figure). View models are small frozen dataclasses/pydantic. Pure
functions over loaded rows so each is unit-testable with a seeded store fixture.
Missing loaders (`load_fills`, and `load_equity_snapshots` from [[equity-snapshots]])
are added to `data/store.py` in the store's existing style.

## Non-goals

- No Streamlit / rendering — that is [[admin-panel]].
- No writes, no scan trigger (the app owns the refresh button), no order path (INV-01).

## Touches

- [INV-01] — read-only; no order/execution capability.
- [INV-03] — UTC timestamps in every view model.
- [INV-13] — renders the frozen `Candidate` unchanged.
- [INV-14/16] — reads the frozen `Position`/`Fill` + reconciled equity as broker-truth.

## Depends on

- [[equity-snapshots]] (the `equity_snapshots` table + `load_equity_snapshots`), shipped store loaders (`load_open_positions`, `load_account_state`, `load_watchlist`, `load_deviation_log`, `load_candles`), `risk/limits.py` (read-only book-risk sum), `signals/ranker.Candidate`, `execution/models`.

## Approach

Build `panel/data.py` + the two missing store loaders; unit-test every view model
against a seeded store. The drawdown computation and the risk-in-use reuse are the
two bits with real logic — test them hardest.

## Open questions

- Risk-in-use: extract the book-risk sum from `risk/limits.py` into a small shared
  read-only helper, or recompute in `panel/data.py`? Propose a read-only reuse of
  the existing sum so the panel figure matches the kill switch exactly.

## Out of scope

- The Streamlit app + charts component ([[admin-panel]]).
