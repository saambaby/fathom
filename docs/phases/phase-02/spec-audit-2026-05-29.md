# Fathom Phase 2 — Cross-Spec Audit (2026-05-29)

Run per `runbook-cross-spec-audit` by a fresh, independent auditor (read-only). Fixes applied by the lead afterward — each finding annotated with its resolution.

## Scope

7 Phase 2 specs: signal-ranker, portfolio-limits, chart-generation, news-risk-assessment, watchlist-narration, cli-commands, hermes-job-definitions. Cross-checked against `invariants.md`, `code-map.md`, `INDEX.md`, `phase-2.md`, and shipped contracts (`Signal`, `load_approved_set`, `ApprovedSetEntry`, `CalendarEvent`/`upcoming_events`, `cli.py`).

## Summary

14 shared concepts · 7 consistent · 3 blocking (drift/ambiguous) · 4 non-blocking · 2 invariant-promotion candidates.

## Findings & resolutions

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| DRIFT-02 | blocking | `Candidate` wire shape never pinned (prose only); `rank` field inconsistent across specs; Signal nest-vs-flatten unresolved | **Fixed** — signal-ranker now pins an explicit **flat** `Candidate` field table; cli/chart/narration aligned to it; promoted to **INV-13** (frozen Hermes-facing contract) |
| DRIFT-01 | blocking | `granularity` (approved-set/DB) vs `timeframe` (Signal) join-key equivalence undocumented — INV-10 gate footgun | **Fixed** — signal-ranker documents the join `signal.timeframe == row['granularity']` |
| AMBIGUOUS-01 | blocking | news-flagged candidates: dropped by `rank()` or passed through? deterministic gate vs Claude veto relationship undefined | **Fixed (lead ruling)** — high-impact event in-window ⇒ **dropped** (hard pre-filter); medium-impact ⇒ kept with `news_flag=True`; Claude news-risk is the finer veto on survivors. signal-ranker AC + hermes-job sequence aligned |
| DRIFT-03 | non-blocking | `expectancy` = `oos_sharpe_mean` (misleading; "expectancy" usually means EV) | **Fixed** — field renamed `oos_sharpe_mean` throughout; the misleading `expectancy`/composite `score` names removed |
| AMBIGUOUS-02 | non-blocking | chart title `score` vs `Signal.quality_score` ambiguity | **Fixed** — chart references `Candidate` fields (`oos_sharpe_mean`, `rank`) explicitly |
| AMBIGUOUS-03 | non-blocking | watchlist persistence undecided; `fathom watchlist` absent from Hermes sequence | **Fixed** — persistence = `watchlist` SQLite table; `fathom scan` persists + prints JSON; `fathom watchlist` is the persisted-read accessor; hermes-job sequence notes it uses `scan`'s JSON directly |
| AMBIGUOUS-04 | non-blocking | `quality_score` not cross-strategy comparable, yet multiplied into the ranking score | **Fixed (lead ruling)** — rank by `oos_sharpe_mean` primary (INV-11-comparable), `quality_score` **tie-break only**; no multiplicative cross-strategy composite. Closes CANDIDATE-INV-B by removing the comparability dependency |

## Invariant-promotion candidates

- **CANDIDATE-INV-A → promoted as INV-13:** the `Candidate` model is the frozen Hermes-facing wire contract.
- **CANDIDATE-INV-B → resolved in-spec** (quality_score demoted to tie-break) — no new invariant needed; the incommensurability dependency is removed rather than enforced.

## Consistent (no action)

INV-02 (news-risk fails safe / narration fails cosmetic — distinction explicit), INV-01 (cli + hermes-job both bound to no-order-path), INV-10 (empty approved-set → empty watchlist, consistent across ranker/cli/hermes-job), `NewsRiskVerdict` schema (identical across specs), conflict policy D-P2-1 (ranker-internal), dependency graph (acyclic, matches code-map), no `anthropic` dep (D-P2-3), INV-03/INV-08, INDEX matches the 7 files, matplotlib via coordinator, cli.py single-writer.

*Verdict after fixes: spec corpus coherent; ready for taskgraph generation.*
