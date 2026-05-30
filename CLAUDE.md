# Fathom

Forex algorithmic trading system — OANDA-based, multi-strategy, orchestrated by Hermes Agent. Demo-first.

---

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/PLAYBOOK.md`](docs/PLAYBOOK.md) | **Start here.** Phase status (what's done / what's next) + the reproducible build method: kickoff prompt, half-cycle layers, runbook flow, orchestration pattern, copy-paste prompts |
| [`docs/product-spec.md`](docs/product-spec.md) | Scope, confirmed decisions, build phases, honest caveats |
| [`docs/architecture-overview.md`](docs/architecture-overview.md) | Container diagram, key boundaries, data flows, repo layout, stack |
| [`docs/invariants.md`](docs/invariants.md) | 16 non-negotiable rules (execution boundary, JSON+safe-defaults, UTC, brackets, 0.25% cap, approved-set gate, frozen `Candidate` + `Order`/`Fill`/`Position` contracts, client-order-id idempotency, broker-is-truth, …) |
| [`docs/features/INDEX.md`](docs/features/INDEX.md) | One-line summary per feature area with phase and status |
| [`docs/forex-algo-trading-plan.md`](docs/forex-algo-trading-plan.md) | Original design narrative (full rationale and deep-dives) |
| [`docs/go-live-runbook.md`](docs/go-live-runbook.md) | **Go-live runbook** — deliberate operator cutover procedure (INV-07 hard gate, cutover sequence, small-size start + ramp, rollback, monitoring, go/no-go decision record) |

**Phase docs (current scope):** — full status table in [`docs/PLAYBOOK.md`](docs/PLAYBOOK.md)

| Phase | Doc | Status |
|---|---|---|
| PoC | [`docs/phases/poc.md`](docs/phases/poc.md) | ✅ Done — 0/36 approved (honest negative) |
| Phase 1 | [`docs/phases/phase-1.md`](docs/phases/phase-1.md) | ✅ Done — 1A 10/72 approved + 1B live stream/calendar |
| Phase 2 | [`docs/phases/phase-2.md`](docs/phases/phase-2.md) | ✅ Code merged · ⏳ T-08 live-Discord acceptance is an operator gate |
| Phase 3 | [`docs/phases/phase-3.md`](docs/phases/phase-3.md) | ✅ Code merged (10/10 units) · ⏳ T-11 live demo-loop acceptance is an operator gate |
| Phase 4 | [`docs/phases/phase-4.md`](docs/phases/phase-4.md) | ✅ Code merged (5/5 units) · ⏳ T-06 panel acceptance is an operator gate |
| Phase 5 | _not yet carved_ | ◻ Not started — go-live decision (product-spec Phase 6, INV-07) |

**Read before starting any session:** `docs/PLAYBOOK.md` (status + method) + `docs/architecture-overview.md` (boundaries) + `docs/invariants.md` (rules) + the active phase doc.

---

## Stack at a Glance

Python 3.11+ · oandapyV20>=0.6 · pydantic>=2 · pydantic-settings>=2 · python-dotenv>=1.0 · pandas>=2.0 · python-dateutil>=2.8 · pyarrow>=14 · httpx>=0.27 · matplotlib>=3.7 · custom event-driven backtest engine · walk-forward validator · Hermes Agent (Nous Research) · anthropic SDK · SQLite→PostgreSQL · Parquet · Streamlit + TW Lightweight Charts

**Dev deps (optional group):** pytest>=7.4 · mypy>=1.8 · responses>=0.25 (HTTP mock for OANDA unit tests) · hypothesis>=6.0 (property-based tests for the backtest engine — no-look-ahead / fill / cost invariants)

**mypy:** `[tool.mypy]` enables `plugins = ["pydantic.mypy"]` so strict mode understands pydantic v2 model construction with defaulted fields.

---

## Common Commands

```bash
# Phase 1A
fathom backtest               # full-universe walk-forward → persist approved_set table (P1A-T-08)
#   fathom backtest [--instruments ALL|EUR_USD,...] [--timeframes H1,H4,D]
#                   [--strategies all|macrossover,donchian,bollinger,rsi,roc,session]
#                   [--workers N] [--db-path PATH] [--history-years N] [--dry-run]

# Phase 2 (current) — P2-T-07
fathom scan                   # refresh candles, rank approved strategies → PortfolioLimiter,
                              # persist watchlist table, print Candidate[] JSON
#   fathom scan [--instruments ALL|EUR_USD,...] [--timeframes H1,H4,D]
#               [--db-path PATH] [--history-years N] [--dry-run]

fathom watchlist              # output latest persisted watchlist as Candidate[] JSON (INV-13)
#   fathom watchlist [--db-path PATH]

fathom chart <instrument>     # render candidate chart PNG, print path (Hermes tool)
#   fathom chart EUR_USD [--timeframe H1] [--db-path PATH] [--out-dir DIR] [--history-years N]

# Phase 3 (current) — P3-T-10 — INV-01 gate (operator-only, NEVER Hermes tools)
fathom execute <candidate-ref>  # run full Phase 3 gate (pretrade → sizing → limits → submit)
#   fathom execute EUR_USD:H1:macrossover_10_50 [--db-path PATH] [--dry-run] [--yes]
#   candidate-ref format: instrument:timeframe:strategy_name (must be on latest watchlist)
#   --dry-run: runs gate steps 1-5, prints would-be order without any v20 submission
#   --yes: skip the interactive confirm prompt

fathom positions              # print open Position[] JSON from the store
#   fathom positions [--db-path PATH]

fathom reconcile              # run one broker-truth reconcile pass, print ReconcileReport JSON
#   fathom reconcile [--db-path PATH]

# Phase 4 — admin panel (P4-T-05)
streamlit run panel/app.py    # launch the read-only Streamlit dashboard
#   streamlit run panel/app.py [-- --db-path PATH]
#   5 views: Charts (Lightweight Charts + overlays), Equity, Blotter, Watchlist, Deviation Log
#   Refresh button → signals.scan.run_scan (order-free); never fathom execute

# PoC (superseded by `fathom backtest`)
python scripts/poc_run.py     # end-to-end PoC: fetch candles → backtest → approved-set table

pytest                        # run test suite
```

---

## Context Maintenance

Three surfaces — route updates to the right one:

- **`CLAUDE.md`** (this file, in git): commands, stack, doc map
- **`.claude/context/`** (in git): architecture changes, new patterns, gotchas the team needs
- **`~/.claude/projects/-home-sam-baby-development-fathom/memory/`** (local only): account names, deploy URLs, API tokens by name, debugging stories

### Trigger table

| Change | Update |
|---|---|
| `pyproject.toml` dep added/removed | CLAUDE.md → Stack |
| New CLI command | CLAUDE.md → Commands |
| New doc file | CLAUDE.md → Documentation table |
| Invariant added or changed | `docs/invariants.md` |
| New feature area | `docs/features/INDEX.md` |
| Architectural decision | `docs/architecture-overview.md` |
| Secret / account name / URL | memory folder only |
