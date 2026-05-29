# Fathom — Feature Index

One row per feature. Scannable in a single read — the cross-feature-consistency anchor. Phases follow the carved scheme: **PoC** (shipped) → **Phase 1** (research engine) → **Phase 2** (watchlist→Discord) → later. Spec files live beside this one under `docs/features/`.

## PoC — shipped

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| config/settings | pydantic config, demo/live switch | _(built from taskgraph; no spec file)_ | shipped |
| oanda-client | OANDA v20 REST, candle endpoint, pagination, typed errors | _(taskgraph)_ | shipped |
| candle-store | SQLite candle cache, gap-aware fetch, UTC round-trip | _(taskgraph)_ | shipped |
| strategy-interface | `Strategy` ABC + `Signal` model + `Direction` | _(taskgraph)_ | shipped |
| ma-crossover | MA crossover trend strategy | _(taskgraph)_ | shipped |
| backtest-engine | event-driven, intrabar fills, no look-ahead | _(taskgraph)_ | shipped |
| backtest-costs | spread + slippage (swap deferred D-03) | _(taskgraph)_ | shipped |
| walk-forward | rolling train/test, per-window approved-set gate | _(taskgraph)_ | shipped |
| poc-runner | end-to-end PoC runner | _(taskgraph)_ | shipped |

## Phase 1 — research engine (specs ready; cross-spec audit passed 2026-05-29)

**Epic 1A (research engine → approved-set):**

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| data-layer-expansion | full pair universe + instrument metadata + Parquet archive | [data-layer-expansion.md](data-layer-expansion.md) | ready |
| swap-cost-model | overnight financing + commission; INV-06 fully satisfied | [swap-cost-model.md](swap-cost-model.md) | ready |
| donchian-breakout | Donchian channel breakout (trend family) | [donchian-breakout.md](donchian-breakout.md) | ready |
| bollinger-zscore-reversion | Bollinger / z-score mean reversion | [bollinger-zscore-reversion.md](bollinger-zscore-reversion.md) | ready |
| rsi-reversion | RSI extremes mean reversion (shares mean_reversion.py) | [rsi-reversion.md](rsi-reversion.md) | ready |
| roc-momentum | rate-of-change momentum + volatility confirmation | [roc-momentum.md](roc-momentum.md) | ready |
| session-range-breakout | session / rolling-range breakout (UTC sessions) | [session-range-breakout.md](session-range-breakout.md) | ready |
| full-universe-backtest-runner | `fathom backtest` CLI, scaled walk-forward, persisted approved-set | [full-universe-backtest-runner.md](full-universe-backtest-runner.md) | ready |

**Epic 1B (live-data groundwork — off the critical path to the approved-set):**

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| live-streaming | OANDA pricing stream, reconnect/backoff/gap | [live-streaming.md](live-streaming.md) | ready |
| economic-calendar | calendar/news pull, currency+impact tags | [economic-calendar.md](economic-calendar.md) | draft — blocked on provider choice |

## Phase 2 — watchlist → Discord (not yet specced)

| Feature | Summary | Status |
|---|---|---|
| signal-ranker | score, filter (spread/liquidity/news), dedupe, conflict policy, portfolio correlation | planned |
| cli-commands | `fathom scan \| watchlist \| chart` (Hermes tools) | planned |
| chart-generation | candle chart + signal/entry/stop/target overlays for Discord | planned |
| hermes-job-definitions | plain-English Hermes cron jobs (daily + intraday) | planned |
| news-risk-assessment | Claude structured event-risk scoring (INV-02) | planned |
| watchlist-narration | Claude one-line rationale per candidate | planned |

## Later — execution, monitoring, panel (Phase 3+)

| Feature | Summary | Status |
|---|---|---|
| pretrade-check | deterministic pre-trade Claude sanity check (INV-02) | planned |
| position-sizing | lot size from stop distance + 0.25% cap (INV-05) | planned |
| risk-limits | exposure, correlation caps, daily kill switch | planned |
| execution-engine | OANDA orders, brackets (INV-04), idempotency, retries | planned |
| reconciliation | broker-vs-DB state reconciliation | planned |
| deviation-monitor | always-on adverse-path/slippage/feed-health alerts | planned |
| admin-panel | Streamlit + Lightweight Charts dashboard | planned |
