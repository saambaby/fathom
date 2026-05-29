# Feature: reconciliation

**Status.** draft
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
3. Update `start_of_day_equity` / `day_pl` used by the kill switch.
4. Return a `ReconcileReport` (`adopted`, `closed`, `matched`, `drift_flags`).

Invoked at monitor startup and every N minutes; also callable via a read-only
`fathom reconcile` operator command (optional, in [[execution-cli]]).

## Acceptance criteria

- [ ] A broker-open position absent from the store is **adopted** (inserted) — the broker wins. Verified against a mocked v20 open-trades response.
- [ ] A store-open position the broker has closed is **marked closed** with realized P&L recorded; the kill-switch `day_pl` reflects it. Verified.
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
- Source of truth for realized day P&L: account summary vs summing closed trades.
  Propose account summary.

## Out of scope

- Submission ([[order-placement]]), monitoring/alerts ([[deviation-monitor]], [[monitor-alerts]]).
