# Fathom Phase 1B — Results

**Date:** 2026-05-29 · **Verdict:** ✅ acceptance PASSED — Phase 1 (1A + 1B) complete.

## What was built

- **`data/stream.py`** — `PriceStream`: long-lived OANDA v20 pricing stream (chunked HTTP, not WebSocket), single long-lived reader thread + queue, heartbeat-timeout detection, capped exponential backoff + jitter reconnect, `gap_detected` on reconnect, clean shutdown. INV-03/08/09.
- **`data/calendar.py`** — `EconomicCalendar` ABC + `FairEconomyCalendar`: free ForexFactory/FairEconomy weekly XML feed (no auth), DST-aware feed-TZ→UTC conversion (`America/New_York`), impact + currency tagging, idempotent SQLite upsert. Next-week feed is best-effort (its URL 404s live).

## Live acceptance (2026-05-29)

- **Stream:** connected to the live practice endpoint; received real ticks (EUR_USD 1.16478, USD_JPY 159.251, GBP_USD 1.34288) — all UTC-aware, `gap_detected=False`; clean shutdown.
- **Calendar:** fetched the live FF weekly feed → **97 events stored**; 10 upcoming USD/EUR/GBP/JPY events with correct UTC times, impact, and currency (e.g. `GBP 12:20Z [high] BOE Gov Bailey Speaks`, `USD 14:50Z FOMC Member Schmid Speaks`). Next-week 404 handled gracefully (best-effort).

## Acceptance-gate findings (both caught live, both fixed)

1. **Streaming daemon-thread leak** — `_next_with_timeout` spawned a thread per 0.5s poll and abandoned it; fixed to a single long-lived reader thread (PR #47).
2. **Calendar next-week 404** — `ff_calendar_nextweek.xml` is dead; `refresh()` hard-failed and lost this-week events; fixed to best-effort + default off (PR #48).

(Unit tests used mocks/fixtures and passed both; only the live acceptance surfaced these — the gate doing its job again.)

## Scope note

Both features are **groundwork for Phase 2** (live signal evaluation, deviation monitor, news-risk assessment) — they do not feed the Phase 1 approved-set. Phase 1's research deliverable was the 1A approved-set (see `phase-1a-results.md`).

## Status

Phase 1 complete. Next: **Phase 2** (watchlist → Discord), which consumes the 1A approved-set and the calendar/stream from 1B.
