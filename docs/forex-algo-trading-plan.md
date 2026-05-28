# Fathom — Forex Algorithmic Trading System

**A complete design and build plan for an OANDA-based, multi-strategy forex trading system, orchestrated by the Hermes Agent platform.**

Version 0.4 · Demo-first · Both intraday and swing horizons

> **Naming note:** **Fathom** is this project — the Python codebase we build (with Claude Code) that does the data, strategy, backtest, risk, execution, and monitoring work. **Hermes** refers throughout to **Hermes Agent**, the open-source agent platform from Nous Research — the scheduling, memory, and messaging layer that wraps Claude and *invokes* Fathom. Hermes is configured, not built; Fathom is built, not autonomous. Naming them distinctly also keeps Fathom's repo clear of Hermes' own config directory (`~/.hermes/`).

---

## 0. Read this first — honest framing

Before any architecture, three things that determine whether this project makes or loses money:

1. **The edge does not come from an LLM predicting price.** Large language models — including Claude — are not reliable predictors of currency direction from price data. Anyone who tells you otherwise is selling something. The durable edge in this system comes from *quantitative strategies that survive rigorous backtesting* combined with *disciplined risk management*. Claude's job is everything **around** the signal: writing and testing the code, reading the economic calendar and news, sanity-checking setups for event risk, and producing readable analysis. Claude is the engineer and the analyst, never the oracle.

2. **"High probability and low risk" is a contradiction in its strongest form.** Probability and reward trade off against each other. What we *can* engineer is a system that ranks setups by tested expectancy, filters out low-quality conditions (bad spread, illiquid hours, imminent high-impact news), and sizes positions so no single trade can hurt you badly. "Profitable on average, with bounded downside" is the realistic and correct goal.

3. **Demo first, for a long time.** The large majority of retail algo systems lose money in their first live iterations. We run on an OANDA practice account until the strategies show a positive, stable edge out-of-sample *and* the execution/monitoring plumbing has proven reliable on fake money. Going live is a later milestone, not a starting condition.

*This document is system design, not financial advice. I am not a financial advisor.*

---

## 1. System overview

The system is a set of cooperating services. At a high level:

- A **data layer** pulls historical candles for research and maintains a live price stream for execution and monitoring.
- A **strategy layer** holds a library of interchangeable strategies (trend, mean-reversion, momentum, breakout) behind one common interface.
- A **backtesting and validation engine** decides which strategies are allowed to trade, on which pairs, at which timeframes — based on out-of-sample evidence, not hope.
- **Hermes Agent** (the Nous Research platform) is the orchestration, scheduling, memory, and delivery layer. On its built-in scheduler it runs the daily job in an isolated session: it invokes our pipeline to scan every tradable pair and rank setups, uses Claude to assess per-pair news/event risk and narrate the result, and delivers a **ranked watchlist** with charts and a one-line rationale per pair to your **Discord** server. We don't build this layer — we configure it.
- A **risk and execution layer** (deterministic Python, *not* under Hermes' autonomous discretion) turns approved watchlist entries into live orders with proper stop-loss and take-profit brackets and correct position sizing.
- An always-on **monitoring service** holds the live feed, tracks open trades against their predicted path, and raises alerts on deviation (adverse moves, slippage, volatility spikes, news, or connection loss).
- A **dashboard** visualizes candles, signals, trades, equity curve, and live P&L.

A note on OANDA specifically: the v20 API does **not** use WebSockets. Its live feed is a long-lived HTTP streaming connection (`GET v3/accounts/{accountID}/pricing/stream`) that emits JSON ticks plus heartbeats, capped at roughly four prices per second per instrument. We consume it with a streaming HTTP client (the `oandapyV20` library wraps this cleanly). Order placement, account queries, and historical candles are ordinary REST calls on the same v20 API.

---

## 2. Component deep-dives

### 2.1 Data layer

**Purpose:** supply clean, consistent market data to both research (backtests) and production (live signals and monitoring).

**Historical candles.** OANDA's `instruments/{instrument}/candles` endpoint returns standard timeframes (M1, M5, M15, H1, H4, D, etc.) with bid/ask/mid OHLC. We pull and cache these locally for backtesting. Important caveat: OANDA's candle history is server-aggregated and convenient, but it is **not** a deep tick archive. For true tick-level research we must capture the live stream ourselves over time and accumulate our own archive.

**Live pricing stream.** A persistent HTTP streaming connection subscribed to all instruments we care about. It must handle heartbeats, automatic reconnection with backoff, and gap detection. This single connection feeds both signal evaluation (for intraday strategies) and trade monitoring.

**Economic calendar and news.** A scheduled pull of the upcoming economic calendar (rate decisions, CPI, NFP, etc.) and a headline feed per currency. This is the raw material the Claude sub-agents reason over. We tag each event with currency, time, and expected impact.

**Storage.**
- Candle archive and tick captures: Parquet files (columnar, compact, fast to scan) partitioned by instrument and date.
- Operational state (open trades, signals, watchlists, run logs): SQLite to start, with a clean migration path to PostgreSQL/TimescaleDB once volume or multi-process access demands it.
- All timestamps stored in UTC, RFC 3339, no exceptions.

**Instrument metadata.** Pip location, minimum trade size, margin rate, trading hours, and typical spread per instrument — pulled once and refreshed periodically. The risk and execution layers depend on this.

### 2.2 Strategy layer

**Purpose:** a pluggable library of strategies that all speak the same language, so the engine can run, combine, backtest, and select among them without special-casing.

**Common interface.** Every strategy implements the same contract: given a window of market data (and optionally indicators), it returns zero or more **Signal** objects. A Signal is a structured record: instrument, direction (long/short/flat), entry reference, suggested stop distance, suggested target distance, the strategy that produced it, timeframe, and a confidence/quality score. Standardizing this means ranking, sizing, and execution never need to know which strategy produced a signal.

**Initial strategy set** (all classic, all backtestable, none exotic):
- **Trend-following:** moving-average crossover and Donchian channel breakout. Catches sustained directional moves; loses in chop.
- **Mean-reversion:** Bollinger-band / z-score reversion and RSI extremes. Profits when price overshoots and snaps back; dangerous in strong trends.
- **Momentum:** rate-of-change / breakout-of-range with volatility confirmation.
- **Breakout:** session or range breakout (useful intraday around session opens).

These are deliberately well-understood. The point is not novelty; it's having honest, testable baselines we can measure, combine, and improve. New ideas get added behind the same interface.

**Both horizons.** Because you want intraday *and* swing, every strategy is parameterized by timeframe. The backtester (next section) decides empirically which strategy/timeframe/pair combinations actually work, rather than us guessing. Some strategies will earn their place on H1 intraday; others only on the daily chart. That is a result we *discover*, not a decision we make up front.

### 2.3 Backtesting and validation engine

**This is the most important component. A strategy is unproven — and therefore not allowed to trade real or even demo money in the main pipeline — until it survives this.**

**Engine type.** Start with a fast vectorized/prototyping backtester (e.g. `backtesting.py` or `vectorbt`) to triage ideas quickly, then promote survivors to a higher-fidelity **event-driven** backtester that processes data bar-by-bar (or tick-by-tick) in time order. The event-driven engine is the honest one: it cannot accidentally "see the future," and it can model intrabar stop/target fills realistically.

**Cost modeling — non-negotiable.** A backtest that ignores costs is fiction. We model:
- **Spread** (use OANDA bid/ask, not mid).
- **Slippage**, especially on stops and during news.
- **Commission**, if your account type charges it.
- **Financing / swap** on positions held overnight (matters a lot for swing trades).

**Validation methodology.**
- **Out-of-sample split:** optimize on one period, test on a held-out period the strategy never saw.
- **Walk-forward analysis:** roll the train/test window forward through time, re-fitting periodically, to simulate how the strategy would have adapted in real life.
- **Overfitting guards:** prefer robust parameter *regions* over single magic numbers; penalize strategies with too many parameters; be suspicious of equity curves that are too smooth.

**Metrics reported per strategy/pair/timeframe:** total and annualized return, Sharpe and Sortino ratios, maximum drawdown and its duration, win rate, profit factor, average win vs average loss, expectancy per trade, and trade count (a beautiful result over 12 trades means nothing).

**Output:** an **approved-set** table — which (strategy, pair, timeframe) combinations passed, with their measured edge and risk numbers. The pipeline only ever generates signals from this approved set.

### 2.4 Signal generation and ranking

**Purpose:** turn "all the approved strategies running across all pairs" into a short, ranked, filtered watchlist.

Each scheduled run, the engine evaluates every approved (strategy, pair, timeframe) against current data and collects all live Signals. Then it:
- **Scores** each signal by its strategy's backtested expectancy plus its current quality score.
- **Filters out** signals where conditions are poor: spread too wide right now, instrument in an illiquid session, or a high-impact news event imminent for that currency.
- **De-duplicates and resolves conflicts** — if trend says long EUR/USD and mean-reversion says short, a documented policy decides (e.g. trend wins on the higher timeframe, or the conflict suppresses the trade entirely).
- **Applies portfolio-level filters** — correlation limits so we don't take five correlated EUR longs that are really one big bet.

The result is a ranked candidate list handed to Hermes for the news/context pass.

### 2.5 Hermes — the orchestration layer (Nous Research platform)

**What it is.** Hermes Agent is an open-source platform from Nous Research that wraps a model like Claude and adds the things a raw model lacks: persistent memory across sessions, a skills system, messaging gateways (Telegram, Discord, Slack, WhatsApp), a built-in cron scheduler, tool execution, and always-on autonomous operation. The mental model: **Claude is the brain; Hermes is the body, the schedule, the memory, and the communication channels.** We do not build this — you've already connected Claude to it. We configure a job and give Hermes our pipeline as a tool to call.

**How our pipeline plugs in.** The Python system we build with Claude Code (the `fathom/` codebase) exposes clean CLI commands — e.g. `fathom scan`, `fathom watchlist`, `fathom backtest`, `fathom chart <pair>`. Hermes' scheduler runs the daily job, and within that job Hermes invokes these commands as tools, reasons over their output, and delivers the result. Hermes' scheduler ticks frequently, runs due jobs in isolated sessions, and survives restarts; jobs are defined in plain English (or via its `/cron` command) rather than raw cron expressions.

**The daily job, as Hermes runs it:**
1. Trigger on schedule (e.g. weekday, after a major session close, in your timezone).
2. Call `fathom scan` → the pipeline refreshes data, runs the approved strategies across all pairs, scores/filters/de-duplicates, applies portfolio limits, and returns ranked candidates (deterministic Python).
3. For each candidate, Claude (inside the Hermes session) assesses current news and economic-calendar event risk and writes a short rationale; material event risk down-ranks or vetoes the candidate.
4. Call `fathom chart <pair>` per surviving candidate to render candles + indicators + proposed entry/stop/target.
5. Persist the final **watchlist** (to the pipeline's database and to Hermes' memory) and deliver it to you via your chosen messaging gateway.

**Cadence.** A daily run handles swing setups. For intraday strategies, a second Hermes job on a faster schedule (e.g. hourly during active sessions) does the same against the intraday approved set. Hermes supports multiple jobs natively, so this is configuration, not new code.

**The execution boundary — the most important rule here.** Hermes is an *autonomous, always-on agent that reads untrusted text from the internet*, which is exactly the profile that must never hold direct order authority. Hermes' role ends at producing and delivering the watchlist. Placing trades is done by the deterministic execution service (§2.8) behind the hard risk gate (§2.7) — and on demo, ideally with you approving the watchlist first. This means the worst a prompt-injected headline can do is produce a bad *suggestion* that the deterministic layer can reject, never a bad *trade*. Hermes is also model-agnostic (it can route to other models via OpenRouter), but here we point it at Claude.

### 2.6 LLM / Claude layer — exact boundaries

**What Claude does in this system:**
- **News & event-risk assessment:** inside the Hermes daily session, given the upcoming calendar and recent headlines for a pair's currencies, Claude summarizes the risk and produces a structured verdict — e.g. `{event_risk: high|medium|low, reason: "...", suggest_action: proceed|reduce_size|skip}`. (If we ever need this independently of Hermes — e.g. for the pre-trade check below — the same logic runs via the `anthropic` SDK from inside the pipeline.)
- **Pre-trade sanity check:** immediately before an order fires, a final quick check — "has anything broken since the watchlist was built that contradicts this trade?" A veto here blocks the trade. This runs in the deterministic execution path (via the API), *not* as a free-floating autonomous agent.
- **Watchlist narration:** turning the quantitative ranking into human-readable rationale so you understand *why* each pair is on the list.
- **Engineering (via Claude Code):** writing, refactoring, and testing the system's own code.

**What Claude / Hermes must NOT do:**
- Predict price direction or generate the primary trade signal from price data.
- Size positions or override risk limits.
- Place or modify orders autonomously without the deterministic risk checks passing first. **Because Hermes is autonomous and internet-facing, this rule applies to the whole Hermes layer, not just to individual prompts.**

All Claude outputs that feed automated decisions are **structured (JSON) and validated**; a malformed or low-confidence response defaults to the safe action (skip/reduce), never to "trade anyway."

### 2.7 Risk management and position sizing

**Purpose:** make sure no single trade, and no single day, can do serious damage. This layer has veto power over everything.

- **Per-trade risk:** a small fixed fraction of account equity per trade. Per your preference we start conservative — **~0.25%** per trade — and only revisit upward after a real demo track record. Position size is *derived* from the stop distance and this risk budget — never a fixed lot size.
- **Stop-loss and take-profit:** every trade carries both as bracket orders at submission. No naked positions.
- **Exposure limits:** maximum number of concurrent trades; maximum total risk on the book at once; correlation-aware limits so correlated pairs count as shared exposure.
- **Daily loss limit / kill switch:** if cumulative loss for the day crosses a threshold, the system stops opening new trades and alerts you.
- **Volatility-aware sizing:** wider stops in high-volatility regimes mean smaller size, automatically.

These rules are deterministic Python, fully unit-tested, and sit between signal and execution. Nothing reaches the broker without passing them.

### 2.8 Execution engine

**Purpose:** translate approved, sized signals into correct live orders, reliably.

- **Order placement** via the v20 REST API, as market or limit orders with attached stop-loss and take-profit.
- **Idempotency:** client-supplied order IDs and dedup logic so a retry never double-fills.
- **Retries and error handling** with backoff for transient failures; clear distinction between "order rejected" and "network hiccup."
- **Partial fills and slippage capture:** record the actual fill price and compare to the intended entry (this feeds deviation monitoring).
- **Reconciliation:** on startup and periodically, fetch the broker's view of open trades and reconcile against our database; the broker is the source of truth.

Demo and live use the same code path against different OANDA endpoints/tokens, so promoting from demo to live is a config change, not a rewrite.

### 2.9 Live monitoring and deviation detection

**Purpose:** the always-on Python backend that watches reality and shouts when it diverges from the plan. This is your "track unexpected deviations" requirement.

It consumes the live price stream and, for every open trade, checks:
- **Adverse path:** price moving against the trade faster or further than the strategy's assumption (without yet hitting the stop) — an early-warning signal.
- **Slippage:** fill or stop execution materially worse than expected.
- **Volatility / news spikes:** sudden range expansion, or a high-impact event firing while a position is open.
- **Stale data / connection health:** heartbeat gaps, reconnects, or feed silence — itself a risk condition.

On any trigger it alerts you and, per policy, may auto-flatten or tighten stops for the most severe cases (a configurable response). Alert delivery rides on Hermes' Discord gateway, so monitor alerts land in the same Discord channel as the daily watchlist. It also logs everything for post-hoc review, which is how the strategies actually improve over time.

### 2.10 Admin panel and visualization

**Purpose:** a minimal, self-hosted admin panel — your single screen for everything the system is doing.

**What it shows:**
- **Per-pair charts:** candles with your overlays — the proposed/active entry, stop, and target, plus the signal markers. Rendered with **TradingView's Lightweight Charts™** (free, open-source under Apache 2.0, ~45 KB, client-side) fed directly from the pipeline's data. This gives the familiar TradingView look while showing *your* trades and signals, which TradingView's own widgets cannot. Note the license's attribution requirement (a TradingView notice/link, satisfied by the built-in attribution logo option), and that the library ships no indicators — we draw our own overlays.
- **Equity curve and drawdown** across backtest and live/demo.
- **Live blotter:** open positions, current P&L, today's realized P&L, risk-in-use vs limits.
- **Watchlist view:** ranked candidates with their scores and Claude's rationale, mirroring what Hermes delivered to Discord.
- **Deviation log:** the monitor's alerts and the trades that triggered them.

**Build it; don't run on TradingView.** TradingView isn't where the system lives — the brain (strategies), execution (OANDA), and orchestration (Hermes) all sit elsewhere. We use only its *charting library* inside our own panel. The heavier TradingView libraries (Advanced Charts / Trading Platform) aren't an option anyway — TradingView only licenses those to companies, not for personal projects.

**Stack:** start with a Streamlit panel plus a Lightweight Charts component (fastest path to something genuinely useful), backed by the same database. If you later want a true terminal feel, graduate to a small FastAPI backend with a plain-JS or React front end embedding Lightweight Charts directly. Either way it's self-hosted on your own server.

---

## 3. Technology stack

- **Language:** Python 3.11+ (you asked for turnkey; I'll keep the code clean, documented, and typed).
- **OANDA access:** `oandapyV20` (wraps streaming + REST) or `httpx` directly.
- **Data:** `pandas` / `numpy`, optionally `polars` for large scans; Parquet via `pyarrow`.
- **Backtesting:** `backtesting.py` or `vectorbt` for prototyping; a custom event-driven engine for validation.
- **Orchestration / scheduling / memory / messaging:** **Hermes Agent** (Nous Research, open-source) — pointed at Claude. Provides the cron scheduler, isolated agent sessions, persistent memory, tool execution, and messaging gateways; we use the **Discord** gateway for delivery. Runs always-on on your own private server.
- **LLM:** Claude — reached as Hermes' model for the daily reasoning, and via the official `anthropic` SDK from inside the pipeline for the deterministic pre-trade check.
- **Config & models:** `pydantic` for typed config and validated Signal/Order objects; secrets in environment variables / a `.env` excluded from git.
- **Storage:** SQLite → PostgreSQL/TimescaleDB; Parquet for market-data archives.
- **Admin panel:** Streamlit + TradingView **Lightweight Charts™** (Apache 2.0) to start; FastAPI + JS/React option later. Self-hosted.
- **Logging & alerts:** structured logging (`structlog`); alerts delivered to Discord via Hermes.
- **Quality:** `pytest`, type checking, and CI so changes don't silently break the trading logic.

---

## 4. Proposed repository structure

```
fathom/
├── README.md
├── pyproject.toml
├── cli.py                       # exposes `fathom scan|watchlist|backtest|chart` for Hermes to call
├── .env.example                 # template; real secrets never committed
├── config/
│   └── settings.py              # pydantic config, demo/live switch
├── data/
│   ├── oanda_client.py          # REST + streaming wrappers
│   ├── candles.py               # historical fetch + cache
│   ├── stream.py                # live pricing stream w/ reconnect
│   ├── calendar.py              # economic calendar + news
│   └── store.py                 # Parquet + SQLite persistence
├── strategies/
│   ├── base.py                  # Strategy interface + Signal model
│   ├── trend.py
│   ├── mean_reversion.py
│   ├── momentum.py
│   └── breakout.py
├── backtest/
│   ├── engine.py                # event-driven backtester
│   ├── costs.py                 # spread/slippage/commission/swap
│   ├── walkforward.py
│   └── metrics.py
├── signals/
│   ├── ranker.py                # scoring, filtering, conflict policy
│   └── portfolio.py             # correlation & exposure limits
├── hermes_integration/
│   ├── prompts/                 # news/event-risk + narration prompt templates
│   ├── jobs/                    # plain-English Hermes job definitions (daily, intraday)
│   └── pretrade_check.py        # deterministic pre-trade Claude check (via anthropic SDK)
├── risk/
│   ├── sizing.py                # position sizing from stop distance
│   └── limits.py                # exposure, daily loss, kill switch
├── execution/
│   ├── orders.py                # order placement, brackets, retries
│   └── reconcile.py             # broker-vs-db reconciliation
├── monitoring/
│   ├── watcher.py               # always-on deviation detection
│   └── alerts.py                # Telegram / email
├── panel/
│   └── app.py                   # Streamlit admin panel + Lightweight Charts
├── scripts/
│   └── run_monitor.py           # entrypoint for the always-on deviation monitor
│   # (the daily pipeline is invoked by Hermes via cli.py, not a standalone runner)
└── tests/
    └── ...                      # heavy coverage on risk + execution
```

---

## 5. Where Claude Code fits

Claude Code is your **development environment** for the `fathom/` pipeline — not part of the live trading loop, and separate from Hermes. Use it to:
- Scaffold the repo above and implement each module.
- Build the `cli.py` commands (`scan`, `watchlist`, `backtest`, `chart`) that Hermes will invoke as tools.
- Write and iterate on strategies and their tests.
- Build and debug the backtester and interpret its output.
- Author the prompt templates and the deterministic pre-trade check.
- Maintain and refactor as the system grows.

**Hermes itself is configured, not coded:** you set up the daily/intraday jobs in plain English (or via its `/cron` command), point it at the `fathom` CLI as a tool, and choose your messaging gateway. The only runtime LLM call that lives *inside* the pipeline (via the `anthropic` SDK) is the deterministic pre-trade check; the rest of the Claude reasoning happens within Hermes' own sessions.

---

## 6. Phased build roadmap

**Phase 1 — Foundation & data.** Repo scaffold, config, OANDA client (REST + streaming), candle fetch/cache, live stream with reconnect, storage. *Exit criteria:* we can pull history and watch live prices reliably on the demo account.

**Phase 2 — Strategies & backtesting.** Strategy interface + the four baseline strategies; the prototyping and event-driven backtesters with full cost modeling; walk-forward validation and the metrics report. *Exit criteria:* an honest approved-set table showing which (strategy, pair, timeframe) combos have a real out-of-sample edge.

**Phase 3 — Signals, ranking & Hermes integration.** Scoring/filtering/conflict policy, portfolio limits, the `cli.py` commands, chart generation, the prompt templates, and the Hermes job definition. Wire the `fathom` CLI into a Hermes cron job that runs daily, reasons over news/event risk, and delivers the watchlist. *Exit criteria:* a daily ranked watchlist with charts and rationale lands in your Telegram/Discord, on schedule, from Hermes.

**Phase 4 — Risk, execution & monitoring (demo).** Position sizing, risk limits, kill switch, the deterministic execution engine with brackets and reconciliation (gated, *not* under Hermes' autonomous discretion — watchlist approval in the loop on demo), and the always-on deviation monitor that alerts through Hermes' gateways. *Exit criteria:* the full loop runs on demo, places bracketed trades through the deterministic gate, and alerts on deviation, for a sustained period.

**Phase 5 — Admin panel & hardening.** The self-hosted admin panel (Streamlit + Lightweight Charts) for charts, blotter, equity curve, watchlist, and deviation log; logging/alerting polish; test coverage; and a demo track record long enough to evaluate. *Exit criteria:* you can see everything on your own panel and trust the plumbing.

**Phase 6 — Go-live decision.** Only if Phases 2 and 4 produced a stable positive edge and reliable execution on demo. Going live is a deliberate, reviewed step with small size, not an automatic graduation.

---

## 7. Risks and honest caveats

- **Most retail algo systems lose money.** Backtests overstate performance; live markets have costs, slippage, and regime changes backtests miss. Treat every promising backtest with suspicion until demo confirms it.
- **Overfitting is the default failure mode.** A great backtest is easy to produce by accident. Walk-forward testing and parameter robustness are the antidotes.
- **OANDA is not built for sub-millisecond HFT** and its historical tick depth is limited; our strategies target tick/minute and bar horizons, which is a good fit, not microsecond scalping.
- **News and gaps can blow through stops.** Stops are not guarantees; weekend gaps and high-impact events can fill far past your level. Position sizing and event-risk filtering exist precisely because of this.
- **Operational risk is real risk.** A crashed monitor, a dropped stream, or a double-fill can cost money independent of strategy quality. Reconciliation, idempotency, and health checks are first-class features, not afterthoughts.
- **This is not financial advice.** It's an engineering plan. You own the trading decisions and the capital risk.

---

## 8. Decisions

Confirmed:

1. **Alerts & delivery:** ✅ **Discord**, via Hermes' Discord gateway — watchlist and monitor alerts in the same channel.
2. **Hosting:** ✅ your own **private server**, always-on (runs Hermes, the deviation monitor, and the admin panel; no laptop dependency).
3. **Admin panel:** ✅ self-hosted, Streamlit + TradingView Lightweight Charts to start; FastAPI + JS later if you want a terminal feel.
4. **Pair universe:** ✅ **all FX pairs OANDA offers** in your region. We scan the full set; the ranking/filter naturally deprioritizes illiquid exotics (wide spreads, thin liquidity), so "scan everything" doesn't mean "trade everything." (OANDA's non-FX instruments — metals, indices, commodities — can be added later if you want.)
5. **Per-trade risk:** ✅ **conservative — ~0.25% of equity per trade**, plus a daily loss cap, until a demo track record justifies otherwise.

Still open (default chosen; change if you like):

6. **Intraday cadence:** start swing/daily, add a faster intraday Hermes run once a strategy earns it. (Alternative: build both cadences from day one.)

---

## 9. Immediate next step

When you're ready, we start **Phase 1**: I scaffold the repo and build the OANDA data layer (REST client, candle cache, and the live streaming connection) so we have a reliable foundation to test everything else against — all pointed at your demo account. You'll need to have your demo API token and account ID ready, and we'll keep them in a local `.env` that never gets committed.
