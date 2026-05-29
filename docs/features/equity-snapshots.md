# Feature: equity-snapshots

**Status.** draft
**Phase.** Phase 4
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The backend enabler for the panel's equity curve: a new `equity_snapshots` table
that records a timestamped `(equity, day_pl)` point on every reconcile pass. Today
`account_state` is a singleton row (the *current* day's figures, overwritten each
reconcile) — there is no history to plot. This feature has the already-periodic
`reconcile` append one immutable snapshot per pass, giving the panel a true
broker-sourced equity time series with no new moving parts.

## User-facing behaviour

No CLI surface of its own (the panel reads it; `fathom reconcile` writes it as a
side effect). Two pieces:

- `data/store.py` gains an `equity_snapshots` table and:
  - `write_equity_snapshot(*, as_of: str, equity: float, day_pl: float) -> None` — append-only insert.
  - `load_equity_snapshots(*, since: str | None = None) -> list[dict]` — ordered by `as_of` ascending, optional lower bound.
- `execution/reconcile.py` — **after `store.write_account_state(...)`** (reconcile.py
  ~line 536-540, i.e. once the kill-switch truth row is written), it appends one
  snapshot `(as_of=now (RFC-3339 Z), equity=broker.nav, day_pl=day_pl)`. Note: the
  reconcile value is `broker.nav` (there is no bare `nav` local). Placing the append
  *after* `write_account_state` guarantees a snapshot-write failure can never delay
  or interpose before the kill-switch's `account_state` write. Purely additive — it
  does not change the reconcile diff, the `account_state` update, or the
  `ReconcileReport`.

## Acceptance criteria

- [ ] `equity_snapshots` columns: `as_of` (TEXT, UTC RFC-3339, INV-03), `equity` (REAL), `day_pl` (REAL). Append-only (no overwrite); migration is additive (`CREATE TABLE IF NOT EXISTS`, consistent with the existing store).
- [ ] `reconcile` appends exactly **one** snapshot per pass, with `equity == broker.nav` and `day_pl == nav − start_of_day_equity` (the same figures it writes to `account_state`). Verified against a mocked v20 reconcile.
- [ ] The snapshot append is **additive and non-fatal**: it does not alter the reconcile diff/adopt/close/refresh behaviour, the `account_state` update, or the `ReconcileReport`. All existing Phase 3 reconciliation tests still pass unchanged.
- [ ] `load_equity_snapshots()` returns rows ordered by `as_of` ascending; `since` filters to `as_of >= since`. A two-reconcile fixture yields two ordered points.
- [ ] All timestamps UTC RFC-3339 (INV-03); practice-only context (INV-07); no secret persisted (INV-08).

## Component design

A minimal extension. The table + the two store methods mirror the existing store
migration/accessor style. In `reconcile`, the append is a single
`store.write_equity_snapshot(...)` call after `day_pl` is computed (reconcile.py
~line 535), guarded so a write failure logs WARNING but never aborts the reconcile
(the reconcile's broker-truth job is more important than the snapshot). Because the
write reuses already-computed `nav`/`day_pl`, the snapshot can never disagree with
`account_state`.

## Non-goals

- No retention/pruning policy (keep-all for demo; revisit later).
- No equity-curve rendering — that is [[admin-panel]] via [[panel-data-layer]].
- No new broker calls — reuses the figures reconcile already fetched.

## Touches

- [INV-16] — the snapshot is the broker-truth `nav` reconcile already trusts.
- [INV-03] — UTC RFC-3339 `as_of`.
- [INV-07] — demo/practice context only.

## Depends on

- `execution/reconcile.py` + `data/store.py` (shipped, Phase 3) — additive edits to both (coordinator-serialized — touches shipped files). **Store-edit ownership (D-03):** this spec owns the `equity_snapshots` table + `write_equity_snapshot`/`load_equity_snapshots` and **lands first** on `data/store.py`; [[panel-data-layer]] then adds `load_fills` (serialized after, not parallel).

## Approach

Add the table + accessors to `data/store.py`; add the one-line append (with a
non-fatal guard) to `reconcile`. Re-run the Phase 3 reconciliation suite to prove
the append is behaviour-preserving for everything else.

## Open questions

- Snapshot cadence = reconcile cadence (startup + 5 min). Retention: keep-all for
  demo (a row every 5 min is ~288/day — fine for SQLite); add pruning later if needed.

## Out of scope

- The panel data layer ([[panel-data-layer]]) and the app ([[admin-panel]]).
