# Fathom — Code Map

Area boundaries and safe-parallel rules for orchestrated dispatch. The orchestrator (`runbook-orchestration-kickoff`) uses this to assign one worktree per area and avoid merge collisions. **Rule of thumb: never two parallel workers in the same file.**

Single Python repo (not a monorepo) — "areas" are package directories.

## Areas

| Area | Directory / files | Owns | Status |
|---|---|---|---|
| `config` | `config/settings.py` | pydantic config, demo/live switch | shipped (PoC) |
| `data` | `data/oanda_client.py`, `data/candles.py`, `data/store.py`, `data/stream.py`, `data/calendar.py` | OANDA access, candle fetch/cache, storage, live stream, calendar | partial (PoC: client+candles+store) |
| `strategies` | `strategies/base.py`, `strategies/_indicators.py`, `strategies/trend.py`, `strategies/mean_reversion.py`, `strategies/momentum.py`, `strategies/breakout.py` | strategy interface + shared indicators (`atr()`) + implementations | partial (PoC: base + trend/MACrossover) |
| `backtest` | `backtest/engine.py`, `backtest/costs.py`, `backtest/walkforward.py`, `backtest/metrics.py` | event-driven engine, cost model, walk-forward, metrics | shipped (PoC); `costs.py` extended in Phase 1 |
| `signals` | `signals/ranker.py`, `signals/portfolio.py`, `signals/charts.py`, `signals/correlation.py`, `signals/scan.py` | ranker, portfolio caps, chart PNG; `correlation.py` shared Pearson (Phase 3); `scan.py` = order-free `run_scan` **extracted from `cli.cmd_scan` in Phase 4** | shipped (Phase 2); `correlation.py` P3, `scan.py` P4 |
| `panel` | `panel/app.py`, `panel/data.py` | read-only Streamlit dashboard + view models (charts/equity/blotter/watchlist/deviation log); INV-01 transitive boundary | Phase 4 |
| `hermes_integration` | `hermes_integration/news_risk.py`, `narration.py`, `pretrade_check.py`, `prompts/`, `jobs/` | Claude response models+validators (INV-02); `pretrade_check.py` = in-process pre-trade veto (Phase 3) | shipped (Phase 2); `pretrade_check.py` new in Phase 3 |
| `risk` | `risk/sizing.py`, `risk/limits.py` | stop-derived sizing (INV-05), exposure/correlation caps + daily-loss kill switch | Phase 3 |
| `execution` | `execution/models.py`, `execution/orders.py`, `execution/reconcile.py` | frozen Order/Fill/Position (INV-14), bracket submit + idempotency (INV-04/15), broker reconciliation (INV-16) | Phase 3 |
| `monitoring` | `monitoring/watcher.py`, `monitoring/alerts.py` | always-on deviation detection; `DiscordWebhookClient` alert delivery | Phase 3 |
| `cli` | `cli.py` | `fathom backtest` (P1), `scan\|watchlist\|chart` (P2), `execute\|positions\|reconcile` (P3) | new in Phase 1 |
| `scripts` | `scripts/poc_run.py`, `scripts/run_monitor.py` | one-off runners; `run_monitor.py` = always-on monitor entrypoint (Phase 3) | shipped (PoC); monitor new in Phase 3 |
| `tests` | `tests/`, `tests/integration/` | test suites (per-area files) | shipped (PoC) |
| _future_ | `panel/` | Phase 4+ (admin panel) | not started |

## Shared / coordinator-owned files

Edits to these go through the **coordinator branch** or are **serialized** — never two parallel feature workers. (In the PoC, two parallel workers both edited `pyproject.toml`; git auto-merged identical changes by luck. Don't rely on luck.)

- `pyproject.toml` — any new dependency
- `CLAUDE.md` — stack/commands/doc-map
- `.gitignore`
- `docs/invariants.md`
- `docs/features/INDEX.md`
- `docs/code-map.md` (this file)

**Dispatch rule:** a worker that needs a new dependency declares it; the coordinator applies the `pyproject.toml` + `CLAUDE.md` edits on the coordinator branch (or serializes them), and feature workers rebase onto it.

## Safe-parallel rules

- **Different area / different file → parallel OK.** e.g. `momentum.py` and `breakout.py` workers run concurrently.
- **Same file → NEVER parallel.** Serialize, or merge into one task.
- **Shared file (above) → coordinator branch only.**

## Phase 1 dispatch implications (pre-resolved collisions)

| Collision | Files | Resolution |
|---|---|---|
| `bollinger-zscore-reversion` + `rsi-reversion` | both write `strategies/mean_reversion.py` | **serialize** (one task after the other) or merge into one `mean-reversion-strategies` task |
| `data-layer-expansion` internals | `oanda_client.py` + `candles.py` + `store.py` | **one task** (single owner of the data-layer change set) — do not split across parallel workers |
| every strategy worker + the runner | all add deps to `pyproject.toml` | dep edits via **coordinator branch** |
| `donchian-breakout` | extends `strategies/trend.py` (alongside existing `MACrossover`) | single worker; no parallel edit of `trend.py` |
| shared ATR helper | all 5 strategy specs import `strategies/_indicators.py::atr()` (INV-11) | `_indicators.py` is a **shared prerequisite** — it must be created (extract the existing `trend.py` ATR, `ewm(com=period-1, adjust=False)`) before the strategy tasks can import it. Sequence it first (a small dedicated task, or fold into the donchian/trend task and have the others depend on it). |

**Safe-parallel set for Phase 1 (after `data-layer-expansion` AND `_indicators.py` land):** `{donchian (trend.py)}`, `{bollinger→rsi (mean_reversion.py, serialized)}`, `{roc (momentum.py)}`, `{breakout (breakout.py)}`, `{live-streaming (stream.py)}`, `{economic-calendar (calendar.py)}` can all run concurrently — distinct files, all importing the shared `_indicators.atr()`. `swap-cost-model (costs.py)` is independent of the strategies. The runner (`cli.py`) is the join point and runs last.

## Phase 2 dispatch implications (pre-resolved collisions)

| Collision | Files | Resolution |
|---|---|---|
| `signal-ranker` defines the `Candidate` contract | `signals/ranker.py` | **load-bearing prerequisite** — `Candidate` shape is consumed by portfolio, cli, narration, charts. Ship/lock it first; downstream tasks depend on it. |
| `portfolio-limits` | `signals/portfolio.py` (distinct file from ranker) | parallel-safe with other `signals/` files once `Candidate` is locked; logically sequenced after `signal-ranker`. |
| `chart-generation` | `signals/charts.py` | distinct file → parallel-safe. New dep `matplotlib` → **coordinator** edits `pyproject.toml` + CLAUDE.md. |
| `cli-commands` | `cli.py` (shared with Phase 1 `backtest`) | **ONLY Phase 2 task that edits `cli.py`** — no other Phase 2 worker touches it; it's the join point. Depends on ranker+portfolio+charts. |
| `news-risk-assessment` + `watchlist-narration` | both under `hermes_integration/` but **different files** (`news_risk.py`+`prompts/news_risk.md` vs `narration.py`+`prompts/narration.md`) | parallel-safe (distinct files); `prompts/` dir is shared but the two files within it don't collide. No `anthropic` dep added (D-P2-3). |
| `hermes-job-definitions` | `hermes_integration/jobs/daily.md` (config, not code) | capstone — depends on cli + both Claude specs; runs last. Its live Discord acceptance is a **manual/human-admin** task (D-P2-5). |

**Safe-parallel set for Phase 2 (after `signal-ranker` locks the `Candidate` shape):** `{portfolio-limits (portfolio.py)}`, `{chart-generation (charts.py)}`, `{news-risk-assessment (hermes_integration/news_risk.py)}`, `{watchlist-narration (hermes_integration/narration.py)}` run concurrently — distinct files. `cli-commands (cli.py)` is the join (after ranker+portfolio+charts); `hermes-job-definitions` is the capstone (config + manual acceptance). `matplotlib` dep via coordinator.

## Phase 3 dispatch implications (pre-resolved collisions)

| Collision | Files | Resolution |
|---|---|---|
| **Coordinator pre-step: extract correlation primitive** | `signals/portfolio.py` (shipped) → new `signals/correlation.py` | DRIFT-09. Touches a **shipped, tested** file → **coordinator-serialized**, before `risk-limits`. Move `_pearson_corr` + returns loaders out; `portfolio.py` imports them back (behaviour-preserving; re-run Phase 2 portfolio tests). |
| **Coordinator pre-step: `anthropic` dep** | `pyproject.toml` + `CLAUDE.md` | `pretrade-check` needs it → coordinator edit before that task (mirrors Phase 2 matplotlib). |
| `order-model-and-brackets` defines Order/Fill/Position (INV-14) | `execution/models.py` | **load-bearing prerequisite** — sizing, order-placement, reconcile, monitor build against it. Lock + round-trip test first; do not fan out until it passes. |
| `order-placement` owns the store migration | `execution/orders.py` + `data/store.py` (orders/fills/positions tables) | **ONLY Phase 3 task that migrates those tables.** `reconciliation` adds `account_state`; `monitor-alerts` adds `deviation_log` — distinct tables, no collision. `data/store.py` edits serialized across these 3 (declare table ownership; coordinator watches). |
| `data/oanda_client.py` gains order + account-summary endpoints | `data/oanda_client.py` (shipped) | order-placement + reconciliation both need new endpoints → **serialized** (or one adds both, the other imports). Still the only reader of `env` (INV-09). |
| `cli-commands` → `execution-cli` | `cli.py` (shared with P1/P2) | **ONLY Phase 3 task that edits `cli.py`** — the join. Depends on pretrade-check + sizing + limits + orders + reconcile. |
| `pretrade-check` | `hermes_integration/pretrade_check.py` + `prompts/pretrade.md` | distinct files → parallel-safe with the risk/execution tasks once `Candidate` (shipped) is the only input. |
| monitor pair | `monitoring/watcher.py` (+ `scripts/run_monitor.py`) vs `monitoring/alerts.py` | distinct files → parallel-safe; `watcher` defines `DeviationEvent`, `alerts` consumes it (sequence watcher→alerts logically). |

**Drafting/dispatch order for Phase 3:** coordinator pre-steps (`signals/correlation.py` extract, `anthropic` dep) → `order-model-and-brackets` (lock) → `{position-sizing, risk-limits-kill-switch, pretrade-check}` ∥ → `{order-placement, reconciliation}` (serialize `store.py`/`oanda_client.py` edits) → `{deviation-monitor, monitor-alerts}` ∥ → `execution-cli` (join). `data/store.py` and `data/oanda_client.py` are shipped files touched by multiple Phase 3 tasks → **serialize, don't parallelize** those edits.

## Phase 4 dispatch implications (pre-resolved collisions)

| Collision | Files | Resolution |
|---|---|---|
| **Coordinator pre-step: extract order-free scan** | `cli.py` (shipped) → new `signals/scan.py` | DRIFT-01 / INV-01. `cli.py` imports `execution.orders`/`risk` at module level, so the panel can't import `cmd_scan`. Extract `run_scan(...)` (order-free) into `signals/scan.py`; `cmd_scan` becomes a thin argparse adapter. **Coordinator-serialized** (shipped `cli.py`), before `admin-panel`. |
| **Coordinator pre-step: extract book-risk sum** | `risk/limits.py` (shipped) | DRIFT-02. Extract `book_risk_sum(open_positions)` + `book_risk_budget(equity, cfg)`; `check_limits` calls them back (behaviour-preserving; re-run P3 limits tests). **Coordinator-serialized**, before `panel-data-layer`. |
| **Coordinator pre-step: charts dep** | `pyproject.toml` + `CLAUDE.md` | `streamlit` + a Lightweight Charts Streamlit component → coordinator edit before `admin-panel`. |
| `equity-snapshots` owns the snapshot migration | `data/store.py` + `execution/reconcile.py` (shipped) | additive: `equity_snapshots` table + 2 accessors; reconcile appends after `write_account_state`. **Lands first** on `store.py`. |
| `panel-data-layer` adds `load_fills` | `data/store.py` (shipped) | **Serialized after** `equity-snapshots` (same file — never parallel). Owns `load_fills`; consumes `load_equity_snapshots`. |
| `admin-panel` | `panel/app.py` | the join; depends on `panel-data-layer` + `equity-snapshots` + `run_scan` + the charts dep. Transitive INV-01 boundary test. |

**Safe-parallel set for Phase 4:** minimal parallelism — the chain is mostly serial (`store.py` is touched by `equity-snapshots` then `panel-data-layer`; the two extractions are coordinator pre-steps). `panel/data.py` and `panel/app.py` are new isolated files. **Drafting/dispatch order:** coordinator pre-steps `{run_scan extract, book_risk_sum extract, streamlit+lightweight-charts dep}` → `equity-snapshots` (store + reconcile) → `panel-data-layer` (load_fills + view models) → `admin-panel` (join). `data/store.py` edits serialized across `equity-snapshots` → `panel-data-layer`.
