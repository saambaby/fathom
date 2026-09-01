# Fathom

Forex algorithmic trading system — OANDA-based, multi-strategy, orchestrated by Hermes Agent. Demo-first.

---

## Documentation

Layout follows the halfcycle 0.2.0 doc contract: `docs/product/` is the durable product layer,
`docs/features/` is cross-phase, `docs/phases/<phase-NN>/` holds everything for one execution
unit, `docs/reference/` is imported-but-unmaintained.

| Doc | What's in it |
|---|---|
| [`docs/phases/phases-manifest.json`](docs/phases/phases-manifest.json) | **Start here.** Canonical phase index — id, status, outcome, open gate. The single source of truth for "where are we". |
| [`docs/operator-acceptance.md`](docs/operator-acceptance.md) | **Resume here.** The 4 remaining operator gates (T-08 → T-11 → T-06 → T-05) as one ordered checklist with exact commands + the credentials you must supply |
| [`docs/product/spec.md`](docs/product/spec.md) | Scope, confirmed decisions, build phases, honest caveats |
| [`docs/product/architecture.md`](docs/product/architecture.md) | Container diagram, key boundaries, data flows, repo layout, stack |
| [`docs/product/invariants.md`](docs/product/invariants.md) | 16 non-negotiable rules (execution boundary, JSON+safe-defaults, UTC, brackets, 0.25% cap, approved-set gate, frozen `Candidate` + `Order`/`Fill`/`Position` contracts, client-order-id idempotency, broker-is-truth, …) |
| [`docs/product/code-map.md`](docs/product/code-map.md) | Area → path → safe-parallel rules |
| [`docs/features/INDEX.md`](docs/features/INDEX.md) | One-line summary per feature area, grouped by the phase that shipped it |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | **Master implementation plan (2026-08-31)** — audit fixes, LLM provider swap, TradingView posture, capture/breadth work, AI research loop (trial ledger + deflation gate), consolidated sequencing |
| [`docs/go-live-runbook.md`](docs/go-live-runbook.md) | **Go-live runbook** — deliberate operator cutover procedure (INV-07 hard gate, cutover sequence, small-size start + ramp, rollback, monitoring, go/no-go decision record) |
| [`docs/reference/`](docs/reference/) | Archived: the original [design narrative](docs/reference/forex-algo-trading-plan.md) and the [half-cycle verdict](docs/reference/half-cycle-verdict.md) retrospective. (The build method now lives in the `halfcycle` plugin; phase status lives in the manifest.) |

**Phases** — canonical ids, zero-padded so folder order is execution order. Status below mirrors
[`phases-manifest.json`](docs/phases/phases-manifest.json); the manifest wins if they disagree.

| Phase | Doc | Status |
|---|---|---|
| `phase-00` | [PoC](docs/phases/phase-00/phase.md) | ✅ completed — 0/36 approved (honest negative) |
| `phase-01.1` | [research engine](docs/phases/phase-01.1/phase.md) | ✅ completed — 10/72 approved |
| `phase-01.2` | [live-data groundwork](docs/phases/phase-01.2/phase.md) | ✅ completed — stream + calendar accepted live |
| `phase-02` | [watchlist → Discord](docs/phases/phase-02/phase.md) | 🔵 in_progress — code merged · ⏳ T-08 operator gate |
| `phase-03` | [risk, execution & monitoring](docs/phases/phase-03/phase.md) | 🔵 in_progress — 10/10 units merged · ⏳ T-11 operator gate |
| `phase-04` | [admin panel](docs/phases/phase-04/phase.md) | 🔵 in_progress — 5/5 units merged · ⏳ T-06 operator gate |
| `phase-05` | [go-live decision](docs/phases/phase-05/phase.md) | ⛔ blocked — guardrails merged · T-05 operator-only + **INV-07-blocked** |
| `phase-06` | [WS0: audit fixes, portability, CI](docs/phases/phase-06/phase.md) | 🔵 in_progress — 9/11 merged · open: #139, #143 (blocked on human) |

**Read before starting any session:** [`phases-manifest.json`](docs/phases/phases-manifest.json)
(status) + [`docs/product/architecture.md`](docs/product/architecture.md) (boundaries) +
[`docs/product/invariants.md`](docs/product/invariants.md) (rules) + the active phase doc.

---

## Stack at a Glance

Python 3.11+ · oandapyV20>=0.6 · pydantic>=2 · pydantic-settings>=2 · python-dotenv>=1.0 · pandas>=2.0 · python-dateutil>=2.8 · pyarrow>=14 · httpx>=0.27 · matplotlib>=3.7 · custom event-driven backtest engine · walk-forward validator · Hermes Agent (Nous Research) · OpenAI-compatible LLM adapter over httpx (`LLM_*` env: OpenAI / Groq / NIM / OpenRouter / Ollama) · SQLite→PostgreSQL · Parquet · Streamlit + TW Lightweight Charts

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
#   fathom execute "EUR_USD:D:BollingerReversion(20,2.0)" [--db-path PATH] [--dry-run] [--yes]
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

# Phase 5 — go-live guardrails (P5-T-03) — go-live is operator-only + INV-07-blocked
fathom preflight              # GO/NO-GO live-readiness check; NO-GO without --attest
                              # persists a preflight_attestations row (live execute reads it)
#   fathom preflight [--db-path PATH] [--attest-track-record] [--pre-cutover]
#   --pre-cutover: runbook Step 2 (ENV=live, flag still off) — never authorizes an order
#   Live cutover: see docs/go-live-runbook.md. ENV=live + LIVE_TRADING_ENABLED +
#   preflight GO + typed account-id confirm all required; live sizes at LIVE_RISK_FRACTION (0.10%)

# PoC (superseded by `fathom backtest`)
python scripts/poc_run.py     # end-to-end PoC: fetch candles → backtest → approved-set table

pytest                        # run test suite
```

---

## Context Maintenance

Three surfaces — route updates to the right one:

- **`CLAUDE.md`** (this file, in git): commands, stack, doc map
- **`.claude/context/`** (in git): architecture changes, new patterns, gotchas the team needs
- **`~/.claude/projects/-Users-sambaby-Development--saam-baby-arp-fathom/memory/`** (local only): account names, deploy URLs, API tokens by name, debugging stories

### Trigger table

| Change | Update |
|---|---|
| `pyproject.toml` dep added/removed | CLAUDE.md → Stack |
| New CLI command | CLAUDE.md → Commands |
| New doc file | CLAUDE.md → Documentation table |
| Invariant added or changed | `docs/product/invariants.md` |
| New feature area | `docs/features/INDEX.md` |
| Architectural decision | `docs/product/architecture.md` |
| Phase status / outcome / gate change | `docs/phases/phases-manifest.json` (then mirror in the CLAUDE.md phase table) |
| New phase carved | `docs/phases/phase-NN/phase.md` + manifest entry (canonical zero-padded id) |
| Secret / account name / URL | memory folder only |
