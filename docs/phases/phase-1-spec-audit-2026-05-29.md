# Fathom Phase 1 — Cross-Spec Audit (2026-05-29)

Run per `runbook-cross-spec-audit` by a fresh, independent auditor (read-only, no specs edited during audit). Fixes applied by the lead afterward — each finding annotated with its resolution.

## Scope

10 Phase 1 specs: data-layer-expansion, swap-cost-model, donchian-breakout, bollinger-zscore-reversion, rsi-reversion, roc-momentum, session-range-breakout, live-streaming, economic-calendar, full-universe-backtest-runner. Cross-checked against `invariants.md`, `code-map.md`, `INDEX.md`, `phase-1.md`, and shipped PoC contracts (`base.py`, `costs.py`, `walkforward.py`, `store.py`, `oanda_client.py`).

## Summary

14 shared concepts audited · 7 consistent · 4 drift · 3 ambiguous · 2 invariant-promotion candidates. 3 blocking, 4 non-blocking.

## Findings & resolutions

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| DRIFT-01 | blocking | Bollinger spec left an unresolved midline-vs-fixed-RR target fork in its AC; RSI did not — inconsistent target semantics in the same file | **Fixed** — promoted **INV-11** (ATR stop + fixed RR target for all strategies); bollinger AC now states `stop × rr_ratio` unconditionally |
| DRIFT-02 | blocking | swap-cost-model's `apply_costs` signature change underspecified: fate of `swap_pips` field, exact new signature, both guard-removal sites | **Fixed** — swap-cost spec now commits: remove `swap_pips` field + both guards (pydantic validator + inline check at `costs.py:159`), add `swap_long_rate`/`swap_short_rate` + `holding_days: int` |
| DRIFT-03 | non-blocking | runner's persisted schema used `timeframe` vs shipped `ApprovedSetEntry.granularity`; `run_timestamp` field fate unclear | **Fixed** — runner spec uses `granularity`; `run_timestamp` is DB-table-only, `ApprovedSetEntry` unchanged |
| DRIFT-04 | non-blocking | session-range-breakout left a stop fork (ATR vs range-width) in its AC | **Fixed** — stop committed to ATR(14) via INV-11; range-width demoted to a future-enhancement note |
| AMBIGUOUS-01 | blocking | `InstrumentMeta` financing fields (`long_rate`/`short_rate`) vs `CostParams` (`swap_long_rate`/`swap_short_rate`) — no canonical naming authority | **Fixed** — data-layer-expansion owns `long_rate`/`short_rate` on `InstrumentMeta`; swap-cost-model maps them into `CostParams.swap_long_rate`/`swap_short_rate` (mapping documented) |
| AMBIGUOUS-02 | non-blocking | ATR formula stated fully only in rsi spec; others say "Wilder/consistent" — duplication + drift hazard | **Fixed** — mandated shared `strategies/_indicators.py::atr()` (`ewm(com=period-1, adjust=False)`, the shipped `trend.py` formula); all strategy specs reference it |
| AMBIGUOUS-03 | non-blocking | runner's SQLite write concurrency left as an open question | **Fixed** — promoted **INV-12** (single-writer, parent-serialized); runner Component design commits to parent-collects-then-writes in one transaction |

## Invariant-promotion candidates (both promoted)

- **INV-11** — every strategy Signal carries an ATR(14)-derived positive stop and an RR-multiple target. Closes DRIFT-01/-04 structurally.
- **INV-12** — approved-set table writes are single-writer, parent-serialized. Closes AMBIGUOUS-03 structurally.

## Other notes fixed

- `phase-1.md` "Done When" said "all four baseline strategies" then enumerated six — corrected to the full baseline set.
- economic-calendar `Touches` missing INV-09 (env-scoped provider config) — added.
- DAG confirmed acyclic; code-map collision table (bollinger+rsi on `mean_reversion.py`; donchian on `trend.py`) consistent with specs; INDEX matches files. The shared `_indicators.py` is added to code-map's `strategies` area.

*Audit verdict after fixes: spec corpus coherent; ready for taskgraph generation.*
