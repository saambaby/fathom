# Feature: economic-calendar

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Add a scheduled pull of the upcoming economic calendar (rate decisions, CPI, NFP, etc.) and per-currency headline feed (`data/calendar.py`). Each event is tagged with currency, UTC time, and expected impact (high/medium/low). This is the raw material Claude's news/event-risk assessment reasons over in Phase 2 — it does **not** feed the Phase 1 approved-set. Independent branch; candidate for epic **1B** if Phase 1 is split.

## User-facing behaviour

Backend module. `EconomicCalendar(...)` exposing `upcoming_events(currencies, window) -> list[CalendarEvent]` and a refresh method. `CalendarEvent` (pydantic): `currency`, `event_name`, `time` (UTC-aware, INV-03), `impact` (enum high/medium/low), optional `actual`/`forecast`/`previous`. Stored in SQLite operational state for later consumption.

## Acceptance criteria

- [ ] Pulls upcoming economic events for the relevant FX currencies and stores them with UTC timestamps (INV-03).
- [ ] Each event carries a normalised `impact` level (high/medium/low) via a documented mapping from the source.
- [ ] Events are tagged with their currency (USD, EUR, GBP, JPY, …) so a per-pair query can union both legs' currencies.
- [ ] A refresh is idempotent (re-pulling the same window updates, doesn't duplicate).
- [ ] The source/provider is pluggable behind the `EconomicCalendar` interface (so a provider swap doesn't ripple).
- [ ] No secrets/keys for the calendar provider are committed (INV-08); any API key lives in `.env`.

## Non-goals

- No LLM reasoning over the calendar (that is Phase 2's news-risk assessment — this feature only supplies the data).
- No down-ranking/veto logic (Phase 2 ranker).
- No real-time news streaming — a scheduled pull is sufficient.

## Touches

- [INV-03] — event times UTC RFC 3339. [INV-08] — any provider API key in `.env` only.
- [INV-09] — provider config is read via the same env-scoped `Settings` path; no demo/live branch in logic.
- [INV-02] — *not* this feature, but noted: when Phase 2 feeds these events to Claude, that output must be structured JSON with safe defaults. This feature just provides clean, typed input.

## Depends on

- `config/settings.py` (for any provider key), `data/store.py` (operational persistence) — exist on `main`.

External (provider decided 2026-05-29):
- **FairEconomy / ForexFactory weekly calendar XML feed** — `https://nfs.faireconomy.media/ff_calendar_thisweek.xml` (and the `nextweek` variant). Free, **no auth/key**. Each `<event>` carries `<title>`, `<country>` (currency code: USD/EUR/GBP/JPY/…), `<date>`, `<time>`, `<impact>` (High/Medium/Low/Holiday), `<forecast>`, `<previous>`. Fits demo-first / self-hosted / no-paid-services. Built behind the `EconomicCalendar` interface so it can be swapped for a paid/official provider later without rippling.

## Approach

Define `CalendarEvent` + an `EconomicCalendar` ABC, with a concrete `FairEconomyCalendar` provider behind it (fetch the weekly XML via `httpx`, parse with the stdlib XML parser). Normalise `<impact>` → the high/medium/low enum (map `Holiday`→low or skip), tag `<country>` → currency. Pull on demand (Phase 2's Hermes job schedules it later; a manual/CLI refresh suffices for Phase 1). Store to SQLite operational state, idempotent upsert keyed on (currency, event_name, time). Keep the provider behind the interface.

**INV-03 is the sharp edge here:** the FF feed publishes times in a fixed display timezone (historically US Eastern / a feed-configured TZ), **not** UTC. The provider MUST convert each event's date+time from the feed's timezone to UTC before constructing `CalendarEvent.time`. Parse defensively (missing/`All Day`/tentative times) and document the source-TZ assumption; a wrong TZ conversion silently shifts every event.

## Open questions

- Feed timezone: confirm the exact TZ the FF XML emits (it has historically been US Eastern; verify against a known event at fetch time). The provider must convert to UTC regardless (INV-03).
- Headline/news feed: deferred — calendar only for Phase 1; headlines are a softer Phase 2 input.

## Out of scope

- Live streaming ([[live-streaming]]).
- Phase 2 news-risk scoring and watchlist veto.
