# Feature: economic-calendar

**Status.** draft
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

External:
- An economic-calendar/news data source (provider TBD — see Open questions). Could be a free calendar API or a scraped feed.

## Approach

Define `CalendarEvent` + an `EconomicCalendar` interface with a concrete provider implementation behind it. Pull on a schedule (invoked by the Phase 2 Hermes job later; for Phase 1 a manual/CLI refresh suffices), normalise impact + currency, store to SQLite. Keep the provider behind the interface so the (uncertain) data source can change without touching consumers.

## Open questions

- **Provider choice** — this is the main unknown. Options: a free economic-calendar API (rate-limited), a paid feed, or scraping (fragile). OANDA's v20 API does *not* provide an economic calendar (it has a separate, deprecated labs calendar endpoint). Decide the provider before drafting the Plan; it determines auth, rate limits, and the impact-level mapping. **Blocking for this spec's implementation, not for the rest of Phase 1.**
- Headline/news feed: bundle with the calendar provider, or separate source? (Lean: calendar first; headlines are a softer Phase 2 input.)

## Out of scope

- Live streaming ([[live-streaming]]).
- Phase 2 news-risk scoring and watchlist veto.
