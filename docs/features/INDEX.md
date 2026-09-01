# Fathom — Feature Index

One row per feature. Scannable in a single read — the cross-feature-consistency anchor. A spec belongs to the product, not to the phase that shipped it — the section headings carry the phase linkage, keyed by canonical id (`phase-00`, `phase-01.1`, …; see [`docs/phases/phases-manifest.json`](../phases/phases-manifest.json)). Spec files live beside this one under `docs/features/`.

## phase-00 — PoC (shipped)

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

## phase-01 — research engine (specs ready; cross-spec audit passed 2026-05-29)

**`phase-01.1` (research engine → approved-set):**

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

**`phase-01.2` (live-data groundwork — off the critical path to the approved-set):**

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| live-streaming | OANDA pricing stream, reconnect/backoff/gap | [live-streaming.md](live-streaming.md) | ready |
| economic-calendar | calendar/news pull, currency+impact tags | [economic-calendar.md](economic-calendar.md) | draft — blocked on provider choice |

## phase-02 — watchlist → Discord (specs ready; cross-spec audit passed 2026-05-29)

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| signal-ranker | gate (INV-10) → filter → news → conflict → rank (by oos_sharpe_mean); emits the pinned `Candidate` (INV-13) | [signal-ranker.md](signal-ranker.md) | ready |
| portfolio-limits | correlation-aware exposure, per-currency + max-concurrent caps | [portfolio-limits.md](portfolio-limits.md) | ready |
| chart-generation | candle chart + entry/stop/target overlays → PNG (matplotlib) | [chart-generation.md](chart-generation.md) | ready |
| news-risk-assessment | Claude `{event_risk,reason,suggest_action}` model + validator (INV-02, malformed→skip) | [news-risk-assessment.md](news-risk-assessment.md) | ready |
| watchlist-narration | Claude one-line rationale + deterministic fallback (cosmetic, NOT INV-02) | [watchlist-narration.md](watchlist-narration.md) | ready |
| cli-commands | `fathom scan \| watchlist \| chart` (Hermes tools; the Hermes boundary) | [cli-commands.md](cli-commands.md) | ready |
| hermes-job-definitions | plain-English daily Hermes job → Discord (configured not coded; capstone, INV-01) | [hermes-job-definitions.md](hermes-job-definitions.md) | ready |

## phase-03 — risk, execution & monitoring, demo only (specs ready; cross-spec audit passed 2026-05-29)

Maps to product-spec Phase 4. The phase where Fathom gains order authority — kept on the deterministic side of INV-01 (operator-run `fathom execute`, never a Hermes tool). See [phase-3.md](../phases/phase-03/phase.md).

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| order-model-and-brackets | frozen `Order`/`Fill`/`Position` models + `build_bracket` (INV-04); prerequisite hub | [order-model-and-brackets.md](order-model-and-brackets.md) | ready |
| position-sizing | units from stop distance + 0.25% equity cap; rejects on no valid stop (INV-05/11) | [position-sizing.md](position-sizing.md) | ready |
| risk-limits-kill-switch | exposure + correlation caps + daily-loss kill switch (UTC-day reset) | [risk-limits-kill-switch.md](risk-limits-kill-switch.md) | ready |
| pretrade-check | in-process Claude veto; pydantic verdict; malformed→abort (INV-02); stubbable adapter | [pretrade-check.md](pretrade-check.md) | ready |
| order-placement | atomic bracket submit to v20 practice; client-id idempotency; retries; slippage capture (INV-04/07/09) | [order-placement.md](order-placement.md) | ready |
| reconciliation | broker-vs-db; broker is source of truth; startup + periodic | [reconciliation.md](reconciliation.md) | ready |
| deviation-monitor | always-on adverse-path/slippage/vol/feed-health detection on open positions | [deviation-monitor.md](deviation-monitor.md) | ready |
| monitor-alerts | format + deliver `DeviationEvent` to Discord via Hermes gateway; durable deviation log | [monitor-alerts.md](monitor-alerts.md) | ready |
| execution-cli | `fathom execute <candidate>` operator join (the INV-01 enforcement point); `positions`/`reconcile` helpers | [execution-cli.md](execution-cli.md) | ready |

## phase-04 — admin panel & hardening, demo only (specs ready; cross-spec audit passed 2026-05-29)

Maps to product-spec Phase 5. A **read-only** Streamlit dashboard over the existing store + TradingView Lightweight Charts; the only action is a scan-refresh (no order/execute — INV-01, transitive-import enforced; execution stays the CLI). See [phase-4.md](../phases/phase-04/phase.md) + [phase-4-spec-audit-2026-05-29.md](../phases/phase-04/spec-audit-2026-05-29.md). Two coordinator pre-step extractions surfaced: `signals/scan.py::run_scan` (order-free scan) and `risk/limits.py::book_risk_sum`/`book_risk_budget`.

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| equity-snapshots | `equity_snapshots` table + reconcile appends a timestamped `(equity, day_pl)` point (backend enabler for the equity curve) | [equity-snapshots.md](equity-snapshots.md) | ready |
| panel-data-layer | `panel/data.py` read-only accessors + view models (blotter incl. risk-in-use, equity series + drawdown, watchlist, deviation log, chart data); the tested seam | [panel-data-layer.md](panel-data-layer.md) | ready |
| admin-panel | Streamlit app: 5 views + Lightweight Charts overlays + scan-refresh button; INV-01 transitive read-only boundary | [admin-panel.md](admin-panel.md) | ready |

## phase-05 — go-live decision, real money (specs ready; cross-spec audit passed 2026-05-30)

Maps to product-spec Phase 6. **Go-live safety guardrails only — the live cutover is INV-07-blocked** (no demo track record yet) and operator-only; nothing here flips live or wires the live token. See [phase-5.md](../phases/phase-05/phase.md).

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| preflight-check | `fathom preflight` GO/NO-GO readiness (account/kill-switch/brackets/env consistency + operator track-record attestation); read-only | [preflight-check.md](preflight-check.md) | ready |
| live-trading-gate | defense-in-depth live gate (ENV=live + `live_trading_enabled` + preflight pass + typed confirm) + reduced `live_risk_fraction` (0.10%); pure, default-refuse | [live-trading-gate.md](live-trading-gate.md) | ready |
| go-live-runbook | the deliberate reviewed cutover procedure (INV-07 prerequisite, gate sequence, small-size start, rollback) — doc/config artifact | [go-live-runbook.md](go-live-runbook.md) | ready |

## phase-07 — standalone platform: de-Hermes, analyze, Pine (spec sprint in progress)

See [phase-07/phase.md](../phases/phase-07/phase.md). Pine generation is the
riskiest-assumption probe and implements first.

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| pine-generation | watchlist → Pine v6 indicator (level lines + labels, `syminfo.ticker` scoped); clipboard/stdout; deterministic, no LLM | [pine-generation.md](pine-generation.md) | ready |
| ai-package-migration | `hermes_integration/` → `ai/`; news-risk + narration LLM calls in-process on `OpenAICompatClient`; parsers/prompts unchanged | [ai-package-migration.md](ai-package-migration.md) | draft |
| analyze-command | `fathom analyze` on-demand pipeline: scan → veto → brief → narration → Pine; `analysis_log` table; offline fail-safe | [analyze-command.md](analyze-command.md) | draft |
| market-brief | brief + regime tag + session verdict models/prompts; advisory ⇒ fallback-text posture (not INV-02 veto) | [market-brief.md](market-brief.md) | draft |
| hermes-teardown | delete chart/PNG + daily job + Discord contract; retire T-08; docs re-baseline | [hermes-teardown.md](hermes-teardown.md) | draft |

## phase-08 — trader companion commands (spec sprint in progress)

See [phase-08/phase.md](../phases/phase-08/phase.md). All commands are INV-01-safe
read-only analysis; LLM outage degrades to "analysis unavailable", never a wrong action.

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| companion-core | context-pack builder + shared call shape + offline fallbacks + AST boundary test | [companion-core.md](companion-core.md) | ready |
| review-command | positions/reconcile/deviation context → anomaly flags; deviation explainer | [review-command.md](review-command.md) | draft |

## phase-09 — counterfactual veto ledger (spec sprint in progress)

See [phase-09/phase.md](../phases/phase-09/phase.md). Recording + tracker ready; report still to spec.

| Feature | Summary | Spec file | Status |
|---|---|---|---|
| veto-ledger | append-only news-risk / pretrade / operator-declined rows; failure-isolated demo-only hooks (INV-09 Phase-9) | [veto-ledger.md](veto-ledger.md) | ready |
| counterfactual-tracker | replay ledger rows via engine fill rules; `veto-report --refresh`; sticky terminals | [counterfactual-tracker.md](counterfactual-tracker.md) | ready |
