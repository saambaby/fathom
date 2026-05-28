# Fathom — Feature Index

One line per feature area. Expand each into its own file under `docs/features/` as it is designed or built.

| Feature Area | Summary | Phase | Status |
|---|---|---|---|
| `data-layer` | OANDA REST + HTTP streaming client, candle cache, live price stream, economic calendar/news pull, Parquet + SQLite storage | 1 | Not started |
| `instrument-metadata` | Pip location, min trade size, margin rate, trading hours, typical spread — fetched once, refreshed periodically | 1 | Not started |
| `strategy-interface` | Common `Strategy` base class and `Signal` model that all strategies implement | 2 | Not started |
| `trend-strategies` | MA crossover and Donchian channel breakout strategies | 2 | Not started |
| `mean-reversion-strategies` | Bollinger band / z-score reversion and RSI extremes strategies | 2 | Not started |
| `momentum-strategies` | Rate-of-change / breakout-of-range with volatility confirmation | 2 | Not started |
| `breakout-strategies` | Session and range breakout strategies (intraday, session opens) | 2 | Not started |
| `backtest-engine` | Event-driven backtester with bar-by-bar simulation, intrabar stop/target fills, no look-ahead | 2 | Not started |
| `backtest-costs` | Spread, slippage, commission, overnight swap modelling — required for any valid backtest | 2 | Not started |
| `walk-forward-validation` | Rolling train/test window, approved-set table output with per-(strategy, pair, timeframe) metrics | 2 | Not started |
| `signal-ranker` | Score, filter (spread, liquidity, news), de-duplicate, conflict policy, portfolio correlation limits | 3 | Not started |
| `cli-commands` | `fathom scan \| watchlist \| backtest \| chart` — Hermes calls these as tools | 3 | Not started |
| `chart-generation` | Candle chart with signal markers, proposed entry/stop/target overlays, exported for Discord | 3 | Not started |
| `hermes-job-definitions` | Plain-English Hermes cron job definitions: daily swing run + intraday run | 3 | Not started |
| `news-risk-assessment` | Claude prompt + pydantic response model for per-pair event-risk scoring (high/medium/low) | 3 | Not started |
| `watchlist-narration` | Claude-written one-line rationale per watchlist candidate | 3 | Not started |
| `pretrade-check` | Deterministic pre-trade Claude sanity check via `anthropic` SDK; veto path | 3 | Not started |
| `position-sizing` | Risk-based lot size from stop distance + 0.25% equity cap | 4 | Not started |
| `risk-limits` | Exposure limits, correlation caps, daily loss kill switch | 4 | Not started |
| `execution-engine` | OANDA v20 order placement, bracket stops/targets, idempotency, retries, partial fill capture | 4 | Not started |
| `reconciliation` | Broker-vs-database state reconciliation on startup and periodically | 4 | Not started |
| `deviation-monitor` | Always-on service: adverse path, slippage, volatility spikes, feed health; auto-flatten policy | 4 | Not started |
| `admin-panel` | Streamlit + TradingView Lightweight Charts: charts, equity curve, live blotter, watchlist, deviation log | 5 | Not started |
