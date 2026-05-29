# Feature: data-layer-expansion

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Broaden the PoC data layer from 3 hardcoded pairs to the full OANDA FX universe, add the instrument-metadata fetch that downstream cost modelling and sizing depend on, and split storage into a Parquet candle archive (research scans) plus SQLite operational state. This is the foundation the swap-cost model and the full-universe backtest runner build on; it is one cohesive change set owned by a single worker (it touches `oanda_client.py`, `candles.py`, and `store.py` together — see [[code-map]]).

## User-facing behaviour

Backend module surface (no human UI). Consumed programmatically:

- `OandaClient.list_instruments() -> list[InstrumentMeta]` — fetches the tradeable FX universe for the configured account via `GET /v3/accounts/{accountID}/instruments`.
- `InstrumentMeta` (new pydantic model): `name` (e.g. `EUR_USD`), `pip_location` (int exponent, e.g. −4), `min_trade_size`, `margin_rate`, `display_precision`, and the financing fields (`long_rate`, `short_rate`, `financing_days_of_week`) used by [[swap-cost-model]]. **This model owns the canonical financing field names** (`long_rate`/`short_rate`); swap-cost-model maps them into `CostParams.swap_long_rate`/`swap_short_rate` at the engine boundary (audit AMBIGUOUS-01).
- `fetch_and_cache(...)` unchanged in signature; gains a Parquet write path so large multi-pair scans don't thrash SQLite.
- `Store` gains: `write_parquet(instrument, granularity, df)` / `load_parquet(...)` for the candle archive (partitioned by instrument + date); existing SQLite `candles` table is retained for operational/cache state and gap detection.

## Acceptance criteria

- [ ] `list_instruments()` returns every tradeable FX instrument for the demo account, each as a validated `InstrumentMeta`; metadata cached to SQLite and refreshable.
- [ ] `pip_location` is correctly derived per instrument (JPY pairs −2, most majors −4) and exposed so callers never hardcode pip value.
- [ ] Candle archive writes to Parquet partitioned by `instrument` and `date`; a round-trip (`write_parquet` → `load_parquet`) preserves `datetime64[ns, UTC]` timestamps and float64/int64 dtypes — identical contract to `Store.load_candles` (INV-03).
- [ ] SQLite remains the source of truth for gap detection (`get_cached_times`) and operational state; Parquet is the bulk archive.
- [ ] A multi-pair fetch over the full universe is gap-aware (no re-fetch of cached ranges) and does not exceed OANDA rate limits (sequential or throttled).
- [ ] No OANDA token appears in any log line (INV-08).

## Component design

New model `InstrumentMeta` in `data/oanda_client.py` (alongside `CandleRow`). `OandaClient` gains `list_instruments()` using `oandapyV20.endpoints.accounts.AccountInstruments`. `Store` gains Parquet methods using `pyarrow`; archive path layout `archive/{instrument}/{YYYY-MM-DD}.parquet`. The existing `candles` SQLite table and `load_candles`/`get_cached_times`/`upsert` are unchanged in contract — Parquet is additive. `fetch_and_cache` writes through to both SQLite (gap state) and Parquet (archive) but its return contract (the `pd.DataFrame` shape) is unchanged.

**Wire-format note:** OANDA returns `pipLocation` as an integer exponent and financing rates as decimal strings; coerce at the client boundary (`InstrumentMeta` validators) — never leak raw strings downstream.

## Non-goals

- No live streaming (that is [[live-streaming]]).
- No change to the `CandleRow` schema or the `load_candles` DataFrame contract.
- No PostgreSQL/TimescaleDB migration (deferred; SQLite + Parquet is sufficient for Phase 1).

## Touches

- [INV-03] — all archived/loaded timestamps UTC RFC 3339 / `datetime64[ns, UTC]`.
- [INV-08] — token never logged; metadata fetch uses the same `SecretStr` path.
- [INV-09] — instrument list is account-scoped via `settings.env`; no demo/live branch in logic.

## Depends on

- PoC data layer (`oanda_client.py`, `candles.py`, `store.py`) — exists on `main`.

External:
- `oandapyV20` (`AccountInstruments` endpoint), `pyarrow` (Parquet) — `pyarrow` is a new dependency.

## Approach

Extend, don't rewrite. `list_instruments()` is a thin new endpoint wrapper mirroring the existing `get_candles` pagination/error pattern (`OandaAPIError` on 4xx/5xx). Parquet methods sit beside the SQLite ones in `Store`; the dual-write keeps gap detection cheap (SQLite) while making full-universe scans fast (columnar Parquet). Metadata is cached so the universe list and pip locations aren't re-fetched every run.

## Open questions

- Parquet partition granularity — by date (daily files) vs by month? Daily is simpler for gap reasoning; month reduces file count for D-granularity. (Lean: by date for H1/H4, by month for D — decide in Plan.)
- Full universe is ~68–70 FX pairs; confirm the demo account's tradeable set and whether any are illiquid enough to exclude up front (vs letting the ranker deprioritise later — Phase 2 concern, so include all here).

## Out of scope

- The backtest runner's parallelism (that's [[full-universe-backtest-runner]]).
- Swap-rate *application* (this spec only *fetches* the financing metadata; [[swap-cost-model]] applies it).
