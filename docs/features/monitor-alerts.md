# Feature: monitor-alerts

**Status.** draft
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The delivery layer for the deviation monitor: turn a `DeviationEvent` into a
concise, human-readable alert and post it to the **same Discord channel** as the
daily watchlist, via Hermes' Discord gateway. Alerts ride the existing Hermes
gateway (delivery only — this is outbound notification, not order authority, so
INV-01 is untouched). Delivery is best-effort with retry; an event is always
persisted to the store's deviation log regardless of delivery success.

## User-facing behaviour

Backend module `monitoring/alerts.py`:

- `Alerter(gateway, store)` with `send(event: DeviationEvent) -> None`.
- Formats a one-line alert: `⚠️ <instrument> <type> | <detail> | <UTC time>`
  (e.g. `⚠️ EUR_USD adverse | −0.6×stop excursion | 2026-05-29T15:10:00Z`).
- Persists the event to the store `deviation_log` table first (durable), then posts
  to Discord via the Hermes gateway.
- Delivery failure → retry per gateway policy; the persisted log is the durable
  record for the panel (impl-Phase 4) and post-hoc review.
- Debounce/severity is decided upstream in [[deviation-monitor]]; the alerter
  formats and ships.

## Acceptance criteria

- [ ] Each `DeviationEvent` is persisted to `deviation_log` **before** the delivery attempt (durable even if Discord is down). Verified.
- [ ] The formatted alert is one line, includes instrument, deviation type, a short detail, and a UTC RFC-3339 timestamp (INV-03); no secret appears (INV-08).
- [ ] Delivery goes through the Hermes Discord gateway to the configured channel; a delivery failure is retried and does not raise into the monitor loop (a down Discord never crashes the watcher).
- [ ] Alerts are outbound-only — the module exposes no order/execution capability and is not registered as a Hermes tool (INV-01).
- [ ] A duplicate event id is not double-logged (idempotent persistence).

## Component design

`monitoring/alerts.py` takes an injectable `gateway` (the Hermes Discord webhook /
gateway client) and the store. Formatting is a pure function over `DeviationEvent`
(unit-testable); persistence-then-deliver ordering guarantees the durable record.
Mirrors Phase 2's "delivery is best-effort, the store is the durable truth"
posture. → sonnet.

## Non-goals

- No rule evaluation / debounce — that is [[deviation-monitor]].
- No watchlist delivery — that is the Phase 2 Hermes job (this is the monitor's alert channel, same destination).

## Touches

- [INV-01] — outbound delivery only; never order authority; not a Hermes tool.
- [INV-03] — UTC timestamps on alerts + log rows.
- [INV-08] — no secrets in alert text or logs.

## Depends on

- [[deviation-monitor]] (`DeviationEvent`), the Hermes Discord gateway (config), `data/store.py` (`deviation_log` table — this spec owns it).

## Approach

`monitoring/alerts.py`. Pure formatter + persist-then-deliver wrapper with retry.
Inject the gateway for testing (no live Discord in tests). The live webhook is
exercised at the acceptance gate alongside the watcher.

## Open questions

- Gateway interface: reuse the same Discord webhook the Phase 2 watchlist uses, or a
  dedicated alerts webhook? Propose **same channel** (per the design — watchlist +
  alerts together), one webhook.
- Rate-limit/coalesce repeated alerts for the same position — propose a short
  cooldown window; debounce primarily lives in [[deviation-monitor]].

## Out of scope

- Rule logic ([[deviation-monitor]]), the panel deviation-log view (impl-Phase 4).
