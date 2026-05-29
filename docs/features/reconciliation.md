# Feature: reconciliation

**Status.** ready
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The truth-keeper: on startup and periodically, fetch the broker's view of open
trades and the account summary, and reconcile them against Fathom's `positions`
table. **The broker is the source of truth** — local state is corrected to match,
and any drift (a position we think is open but the broker closed, a fill we missed,
an unexpected broker position) is logged and surfaced. This is what makes a crashed
process or a missed fill recoverable rather than silently wrong.

## User-facing behaviour

Backend module `execution/reconcile.py`. `reconcile(*, client, store, now) -> ReconcileReport`:

1. Fetch open trades + account summary (equity, realized day P&L) from OANDA v20.
2. Diff against the store `positions`:
   - **broker-only** position (we missed a fill / restarted) → adopt it into the store.
   - **store-only** position (we think open, broker closed it — stop/target hit) →
     mark it closed in the store, record the realized P&L.
   - **matched** → refresh `unrealized_pl`, stop/target, units.
3. Update the persisted `account_state` row used by the kill switch (DRIFT-02/05):
   - `day_pl` ← the **account-summary's realized day P&L** (the broker's figure —
     authoritative per INV-16; the store column is a cached mirror, not an
     independent sum-of-closed-trades).
   - `start_of_day_equity` ← snapshotted **once, on the first reconcile after the
     UTC-day boundary** (00:00 UTC, INV-03-consistent with the kill-switch reset).
     A mid-day process restart re-reads the persisted snapshot — it does **not**
     re-snapshot (so the kill-switch threshold is stable across restarts).
4. Return a `ReconcileReport` (`adopted`, `closed`, `matched`, `drift_flags`).

Invoked at monitor startup and every N minutes; also callable via a read-only
`fathom reconcile` operator command (optional, in [[execution-cli]]).

## Acceptance criteria

- [ ] A broker-open position absent from the store is **adopted** (inserted) — the broker wins. Verified against a mocked v20 open-trades response.
- [ ] A store-open position the broker has closed is **marked closed** with `realized_pl` written to the `positions` row; the `account_state.day_pl` (from account-summary) reflects the day's loss. Verified.
- [ ] `start_of_day_equity` is snapshotted once per UTC day (first reconcile after 00:00 UTC) and is stable across a mid-day restart (re-read, not re-snapshotted). Verified with a fixture crossing the day boundary.
- [ ] A matched position has its `unrealized_pl`/stop/target refreshed from the broker.
- [ ] `start_of_day_equity` and `day_pl` are updated from the account summary so the kill switch reads true figures.
- [ ] Drift (counts/ids that don't line up) is recorded in `drift_flags` and logged at WARNING — never silently dropped.
- [ ] Reconciliation is idempotent: running it twice with no broker change is a no-op (no duplicate adoptions).
- [ ] Practice endpoint only (INV-07); UTC timestamps (INV-03); no secrets logged (INV-08).

## Component design

`execution/reconcile.py` reads broker state via `oanda_client.py` (open-trades +
account-summary endpoints) and the store `positions`/`fills`. The diff is a pure
function over `(broker_state, store_state)` returning a set of corrective actions,
so it is unit-testable without a broker; a thin wrapper applies the actions and
fetches state. "Broker is truth" is the one-line rule that resolves every conflict.
→ opus (a reconciliation bug corrupts the very state the kill switch trusts).

## Non-goals

- No order submission — corrective adoption inserts/updates store rows, it does not place trades.
- No alerting on drift beyond logging + the report — alert routing is [[monitor-alerts]] / [[deviation-monitor]].

## Touches

- [INV-07] — practice endpoint only.
- [INV-03] — UTC timestamps.
- [INV-08] — no secrets logged.
- [INV-05] — supplies the true `day_pl`/equity the kill switch depends on.

## Depends on

- [[order-model-and-brackets]], [[order-placement]] (store `positions`/`fills` schema), `data/oanda_client.py` (open-trades + account-summary endpoints).

## Approach

`execution/reconcile.py`. Pure diff function + apply wrapper. Mock v20 for tests.
Run on monitor startup and on a timer; expose a read-only operator command.

## Open questions

- **Cadence** — propose **startup + every 5 minutes**; operator-configurable.
- Adoption policy for an *unexpected* broker position (one Fathom never placed):
  adopt-and-flag, or alert-only? Propose adopt-and-flag (broker is truth) + a loud
  drift alert.

**Resolved at cross-spec audit (2026-05-29):** DRIFT-05 — realized `day_pl` source
is the **account-summary** figure (broker-truth, INV-16); the store column mirrors
it. Caveat noted: the broker's day boundary may differ from UTC-midnight (which the
kill switch uses for reset) — `account_state` records both the broker figure and the
UTC-day `start_of_day_equity` so the kill switch reasons in UTC. DRIFT-02 — the
`account_state` table (`start_of_day_equity`, `day_pl`, `as_of` UTC) is owned by
this spec's migration; restart re-reads, never re-snapshots.

## Out of scope

- Submission ([[order-placement]]), monitoring/alerts ([[deviation-monitor]], [[monitor-alerts]]).
