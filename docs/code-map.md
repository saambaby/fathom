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
| `signals` | `signals/ranker.py`, `signals/portfolio.py`, `signals/charts.py` | ranker (score/filter/dedup/conflict), portfolio correlation+exposure caps, chart PNG rendering | Phase 2 (not started) |
| `hermes_integration` | `hermes_integration/news_risk.py`, `hermes_integration/narration.py`, `hermes_integration/prompts/`, `hermes_integration/jobs/` | Claude response models+validators (INV-02), prompt templates, plain-English Hermes job (configured, not coded) | Phase 2 (not started) |
| `cli` | `cli.py` | `fathom backtest` (Phase 1), `scan\|watchlist\|chart` (Phase 2) | new in Phase 1 |
| `scripts` | `scripts/poc_run.py` | one-off runners | shipped (PoC) |
| `tests` | `tests/`, `tests/integration/` | test suites (per-area files) | shipped (PoC) |
| _future_ | `risk/`, `execution/`, `monitoring/`, `panel/` | Phase 3+ | not started |

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
