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
| `cli` | `cli.py` | `fathom backtest` (Phase 1), `scan\|watchlist\|chart` (Phase 2) | new in Phase 1 |
| `scripts` | `scripts/poc_run.py` | one-off runners | shipped (PoC) |
| `tests` | `tests/`, `tests/integration/` | test suites (per-area files) | shipped (PoC) |
| _future_ | `signals/`, `risk/`, `execution/`, `monitoring/`, `hermes_integration/`, `panel/` | Phase 2+ | not started |

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
