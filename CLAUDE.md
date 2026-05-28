# Fathom

Forex algorithmic trading system — OANDA-based, multi-strategy, orchestrated by Hermes Agent. Demo-first.

---

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/product-spec.md`](docs/product-spec.md) | Scope, confirmed decisions, build phases, honest caveats |
| [`docs/architecture-overview.md`](docs/architecture-overview.md) | Container diagram, key boundaries, data flows, repo layout, stack |
| [`docs/invariants.md`](docs/invariants.md) | 10 non-negotiable rules (execution boundary, JSON+safe-defaults, UTC, brackets, 0.25% cap, …) |
| [`docs/features/INDEX.md`](docs/features/INDEX.md) | One-line summary per feature area with phase and status |
| [`docs/forex-algo-trading-plan.md`](docs/forex-algo-trading-plan.md) | Original design narrative (full rationale and deep-dives) |

**Phase docs (current scope):**

| Phase | Doc | Status |
|---|---|---|
| PoC | [`docs/phases/poc.md`](docs/phases/poc.md) | Not started — **start here** |
| Phase 1 | [`docs/phases/phase-1.md`](docs/phases/phase-1.md) | Stub |
| Phase 2 | [`docs/phases/phase-2.md`](docs/phases/phase-2.md) | Stub |

**Read before starting any session:** `docs/architecture-overview.md` (boundaries) + `docs/invariants.md` (rules) + the active phase doc.

---

## Stack at a Glance

Python 3.11+ · oandapyV20>=0.6 · pydantic>=2 · pydantic-settings>=2 · python-dotenv>=1.0 · pandas>=2.0 · custom event-driven backtest engine · Hermes Agent (Nous Research) · anthropic SDK · SQLite→PostgreSQL · Parquet · Streamlit + TW Lightweight Charts

**Dev deps (optional group):** pytest>=7.4 · mypy>=1.8 · responses>=0.25 (HTTP mock for OANDA unit tests) · hypothesis>=6.0 (property-based tests for the backtest engine — no-look-ahead / fill / cost invariants)

**mypy:** `[tool.mypy]` enables `plugins = ["pydantic.mypy"]` so strict mode understands pydantic v2 model construction with defaulted fields.

---

## Common Commands

```bash
# PoC (current)
python scripts/poc_run.py     # end-to-end PoC: fetch candles → backtest → approved-set table

# Phase 2+ (not yet built)
fathom backtest               # run full backtest suite, write approved-set
fathom scan                   # refresh data, run approved strategies, rank candidates
fathom watchlist              # output ranked watchlist (called by Hermes)
fathom chart <pair>           # render candle chart with overlays

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
