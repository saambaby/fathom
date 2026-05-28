# Fathom — Product Specification

**Version:** 0.4 · Demo-first · Both intraday and swing horizons
**Source of truth for:** scope, goals, decisions, build phases, honest caveats

---

## 1. Purpose

Fathom is a Python forex algorithmic trading system that:

- Pulls and caches market data from OANDA's v20 API
- Runs a library of quantitative strategies and validates them via rigorous backtesting
- Generates and ranks trading signal candidates, filtered by spread, session liquidity, and news/event risk
- Delivers a daily ranked watchlist (with charts and Claude-written rationale) to Discord via Hermes Agent
- Executes approved trades on OANDA through a deterministic, risk-gated engine
- Monitors live positions for deviation and alerts on adverse conditions
- Presents everything through a self-hosted admin panel

**What Fathom is not:** an LLM price-prediction system. The trading edge comes from quantitative strategies that survive rigorous backtesting. Claude's role is everything *around* the signal — news assessment, narration, engineering, pre-trade sanity checks.

---

## 2. Confirmed Decisions

| # | Decision | Value |
|---|---|---|
| 1 | Alert & delivery channel | Discord, via Hermes' Discord gateway |
| 2 | Hosting | Own private server (always-on; runs Hermes, monitor, panel) |
| 3 | Admin panel | Streamlit + TradingView Lightweight Charts (Apache 2.0) to start; FastAPI + JS/React later |
| 4 | Pair universe | All FX pairs OANDA offers in region; scan everything, rank naturally filters illiquid exotics |
| 5 | Per-trade risk | ~0.25% of equity, plus daily loss cap; revisit only after positive demo track record |
| 6 | Intraday cadence | Start swing/daily; add intraday Hermes run once a strategy earns it |

---

## 3. Scope

### In scope
- OANDA v20 data: historical candles, live HTTP stream, instrument metadata
- Economic calendar and news headline pull (input to Claude's news-risk assessment)
- Strategy library: trend (MA crossover, Donchian), mean-reversion (Bollinger/z-score, RSI), momentum (ROC/breakout), breakout (session/range)
- Vectorised prototyping backtester + event-driven validation backtester with full cost modelling
- Walk-forward analysis and approved-set table
- Signal scoring, filtering, de-duplication, conflict policy, portfolio correlation limits
- Hermes integration: CLI commands as tools, chart generation, prompt templates, daily + intraday job definitions
- Deterministic pre-trade Claude check (via `anthropic` SDK)
- Position sizing, exposure limits, daily kill switch — all deterministic Python
- Execution engine: order placement, bracket stops/targets, idempotency, reconciliation
- Always-on deviation monitor: adverse path, slippage, volatility spikes, feed health
- Admin panel: candle charts with overlays, equity curve, live blotter, watchlist, deviation log

### Out of scope (initially)
- OANDA non-FX instruments (metals, indices, commodities) — addable later
- Tick-level HFT or sub-minute strategies
- Multi-broker support
- Mobile app or external-facing interface

---

## 4. Build Phases

### Phase 1 — Foundation & Data
Repo scaffold, config (pydantic), OANDA REST client, candle fetch/cache, live stream with reconnect/backoff, economic calendar + news pull, Parquet + SQLite storage, instrument metadata.

**Exit criteria:** pull history and watch live prices reliably on the demo account.

### Phase 2 — Strategies & Backtesting
Strategy interface + Signal model, four baseline strategies, vectorised prototyping backtester, event-driven validation engine with full cost modelling (spread, slippage, commission, swap), walk-forward validation, metrics report.

**Exit criteria:** an honest approved-set table showing which (strategy, pair, timeframe) combos have a real out-of-sample edge.

### Phase 3 — Signals, Ranking & Hermes Integration
Signal scoring/filtering/conflict policy, portfolio limits, CLI commands (`fathom scan|watchlist|backtest|chart`), chart generation, Hermes prompt templates, daily + intraday job definitions, Hermes wired up delivering watchlist to Discord.

**Exit criteria:** a daily ranked watchlist with charts and Claude rationale lands in Discord on schedule.

### Phase 4 — Risk, Execution & Monitoring (demo only)
Position sizing, risk limits, kill switch, execution engine with brackets and reconciliation (not under Hermes' autonomous discretion — watchlist approval gated), always-on deviation monitor.

**Exit criteria:** full loop runs on demo, places bracketed trades through the deterministic gate, alerts on deviation.

### Phase 5 — Admin Panel & Hardening
Self-hosted admin panel (Streamlit + Lightweight Charts): charts, blotter, equity curve, watchlist, deviation log; structured logging; alert polish; test coverage; sustained demo track record.

**Exit criteria:** everything visible on own panel; plumbing trusted.

### Phase 6 — Go-Live Decision
Only if Phases 2 and 4 produced stable positive edge and reliable execution on demo. Small size. Deliberate, reviewed step.

---

## 5. Honest Caveats

- Most retail algo systems lose money in their first live iterations. Backtests overstate performance.
- Overfitting is the default failure mode — walk-forward testing and parameter robustness are the antidotes.
- OANDA is not built for sub-millisecond HFT; strategies target minute and bar horizons.
- Stops are not guarantees; weekend gaps and high-impact events can fill past them.
- Operational risk is real: a crashed monitor, a dropped stream, or a double-fill can cost money.
- This is engineering documentation, not financial advice.

---

## 6. Cross-References

- Architecture and component deep-dives: [`docs/architecture-overview.md`](architecture-overview.md)
- Non-negotiable rules and invariants: [`docs/invariants.md`](invariants.md)
- Feature index: [`docs/features/INDEX.md`](features/INDEX.md)
- Original design narrative: [`docs/forex-algo-trading-plan.md`](forex-algo-trading-plan.md)
