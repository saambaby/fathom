# Feature: monitor-alerts

**Status.** ready
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The delivery layer for the deviation monitor: turn a `DeviationEvent` into a
concise, human-readable alert and post it to the **same Discord channel** as the
daily watchlist. **Delivery mechanism (DRIFT-06 resolution):** the always-on
monitor is a standalone Python process (`scripts/run_monitor.py`), **not** a
Hermes job — there is no Python-callable "Hermes gateway." It therefore posts
directly via a small `DiscordWebhookClient` that POSTs to `DISCORD_WEBHOOK_URL`
(the same webhook the Phase 2 watchlist channel uses, from `.env` — INV-08). This
is outbound notification only, not order authority, so INV-01 is untouched and no
Hermes job is involved. Delivery is best-effort with retry; an event is always
persisted to the `deviation_log` table first, regardless of delivery success.

## User-facing behaviour

Backend module `monitoring/alerts.py`:

- `Alerter(webhook_client, store)` with `send(event: DeviationEvent) -> None`.
- Formats a one-line alert: `⚠️ <instrument> <type> | <detail> | <UTC time>`
  (e.g. `⚠️ EUR_USD adverse | −0.6×stop excursion | 2026-05-29T15:10:00Z`).
- Persists the event to the `deviation_log` table first (durable), then POSTs to
  `DISCORD_WEBHOOK_URL` via the `DiscordWebhookClient`.
- Delivery failure → retry with backoff; the persisted log is the durable record
  for the panel (impl-Phase 4) and post-hoc review.
- Debounce/severity is decided upstream in [[deviation-monitor]]; the alerter
  formats and ships.

**`deviation_log` table (this spec owns the migration):**
`event_id` (PK, text), `instrument`, `deviation_type` (text enum:
`adverse`|`slippage`|`vol`|`feed_health`), `detail` (text), `broker_trade_id`
(nullable — feed-health events have no position), `severity` (text), `created_at`
(UTC RFC-3339), `delivered` (bool, set true after a successful POST). `event_id` is
unique → re-persisting the same event is a no-op (idempotent).

## Acceptance criteria

- [ ] Each `DeviationEvent` is persisted to `deviation_log` **before** the delivery attempt (durable even if Discord is down). Verified.
- [ ] The formatted alert is one line, includes instrument, deviation type, a short detail, and a UTC RFC-3339 timestamp (INV-03); no secret appears (INV-08).
- [ ] Delivery POSTs to `DISCORD_WEBHOOK_URL` (the shared watchlist channel); a delivery failure is retried with backoff and does not raise into the monitor loop (a down Discord never crashes the watcher).
- [ ] Alerts are outbound-only — the module exposes no order/execution capability and is not registered as a Hermes tool (INV-01).
- [ ] A duplicate event id is not double-logged (idempotent persistence).

## Component design

`monitoring/alerts.py` takes an injectable `DiscordWebhookClient` (a thin `httpx`
POST wrapper around `DISCORD_WEBHOOK_URL`) and the store. Formatting is a pure
function over `DeviationEvent` (unit-testable); persistence-then-deliver ordering
guarantees the durable record. Mirrors Phase 2's "delivery is best-effort, the
store is the durable truth" posture. Tests inject a stub client (no live HTTP).
→ sonnet.

## Non-goals

- No rule evaluation / debounce — that is [[deviation-monitor]].
- No watchlist delivery — that is the Phase 2 Hermes job (this is the monitor's alert channel, same destination).

## Touches

- [INV-01] — outbound delivery only; never order authority; not a Hermes tool.
- [INV-03] — UTC timestamps on alerts + log rows.
- [INV-08] — no secrets in alert text or logs.

## Depends on

- [[deviation-monitor]] (`DeviationEvent`), `DISCORD_WEBHOOK_URL` (`.env`, the shared watchlist webhook), `httpx` (shipped dep), `data/store.py` (`deviation_log` table — this spec owns it).

## Approach

`monitoring/alerts.py`. Pure formatter + persist-then-deliver wrapper with retry.
Inject a stub webhook client for testing (no live Discord in tests). The live webhook is
exercised at the acceptance gate alongside the watcher.

## Open questions

- Rate-limit/coalesce repeated alerts for the same position — propose a short
  cooldown window; debounce primarily lives in [[deviation-monitor]].

**Resolved at cross-spec audit (2026-05-29):** DRIFT-06 — the monitor is a
standalone Python process, not a Hermes job; it posts directly to the shared
`DISCORD_WEBHOOK_URL` via `DiscordWebhookClient` (one webhook, same channel as the
watchlist). DRIFT-08 — `DeviationEvent` is defined in [[deviation-monitor]]; the
`deviation_log` columns are pinned above.

## Out of scope

- Rule logic ([[deviation-monitor]]), the panel deviation-log view (impl-Phase 4).
