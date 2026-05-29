# Feature: cli-commands

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Expose the Phase 2 pipeline as CLI subcommands that Hermes invokes as tools: `fathom scan`, `fathom watchlist`, `fathom chart`. These extend the existing `fathom backtest` (Phase 1) in `cli.py`. `scan` runs the deterministic pipeline (refresh data → ranker → portfolio) and persists the resulting watchlist; `watchlist` emits the latest watchlist as structured JSON (the contract Hermes consumes); `chart` renders a candidate's PNG. This is the tool surface at the **Hermes boundary** — these commands produce watchlists and images, never orders (INV-01).

## User-facing behaviour

`cli.py` gains three subcommands (argparse, no new dep):
- `fathom scan [--instruments …] [--timeframes …] [--db-path …]` — refreshes candles (+ calendar), runs [[signal-ranker]] → [[portfolio-limits]], persists the ranked `Candidate` list (a `watchlist` table or run-stamped JSON), prints a summary. Empty approved-set ⇒ empty watchlist, exit 0 (INV-10).
- `fathom watchlist [--db-path …] [--format json]` — the persisted-read accessor: emits the **latest persisted** watchlist (from the `watchlist` table) as **structured JSON** — a list of the pinned `Candidate` wire shape (INV-13): `instrument, timeframe, strategy_name, direction, entry_ref, stop_distance, target_distance, oos_sharpe_mean, quality_score, rank, spread_ok, session_ok, news_flag, generated_at`. (Hermes typically reads `fathom scan`'s stdout JSON directly; `watchlist` is for re-reading without re-running, and for the Phase 5 panel.)
- `fathom chart <instrument> [--timeframe …] [--out-dir …]` — renders the candidate's chart via [[chart-generation]] and prints the PNG path.

## Acceptance criteria

- [ ] `fathom scan` runs end-to-end against cached/refreshed data, produces a ranked watchlist, persists it; exit 0. Empty approved-set ⇒ empty watchlist, clear message, exit 0 (INV-10).
- [ ] `fathom watchlist` emits valid JSON matching the pinned `Candidate` wire shape (snake_case, UTC RFC-3339 times); parseable by a consumer with no Fathom imports.
- [ ] `fathom chart <instrument>` writes a PNG and prints its path.
- [ ] All three are registered alongside the existing `backtest` subcommand without breaking it.
- [ ] No live OANDA/HTTP in tests — mock the client; use cached fixtures (mirrors the Phase 1 runner test pattern).
- [ ] No token/secret in any command's output or logs (INV-08); all timestamps UTC (INV-03).
- [ ] None of these commands can place or size an order (INV-01) — they have no import path to a (non-existent in Phase 2) execution module.

## Component design

Extend `cli.py`'s argparse with three `add_parser` entries + `cmd_scan` / `cmd_watchlist` / `cmd_chart` handlers, reusing the Phase 1 patterns (`_build_date_range`, the data-refresh helpers, structured logging, UTC formatter). `scan` composes `Ranker` + `PortfolioLimiter`; persistence is a `watchlist` table in `Store` (or a run-stamped JSON artefact) — pin the choice in Plan. `watchlist` serialises the stored `Candidate`s to JSON. `chart` calls `render_candidate_chart`.

**Wire-format contract (the Hermes-facing surface):** both `fathom scan`'s stdout JSON and `fathom watchlist`'s output must match the `Candidate` field table pinned in [[signal-ranker]] / **INV-13** exactly (same field names, snake_case, RFC-3339 UTC) — do not re-list or rename fields here. Any drift breaks the Hermes job.

**Persistence (AMBIGUOUS-03 resolution):** `fathom scan` writes the ranked `Candidate` list to a **`watchlist` SQLite table** (run-timestamped, mirroring how `approved_set` is stored) and prints the same JSON to stdout. `fathom watchlist` reads the latest run from that table. The `Candidate`↔table mapping lives at the persistence layer; the `Candidate` pydantic model is unchanged.

## Non-goals

- No Hermes job logic (that's [[hermes-job-definitions]] — these are just the tools it calls).
- No Discord delivery, no Claude calls (those are Hermes-side).
- No `fathom execute` / order command — does not exist in Phase 2 (INV-01).

## Touches

- [INV-01] — the tool surface stops at watchlist + chart; no order path.
- [INV-10] — `scan` returns an empty watchlist on an empty approved-set.
- [INV-03] — UTC timestamps in output. [INV-08] — no secrets logged.

## Depends on

- [[signal-ranker]] (`Ranker`, `Candidate`), [[portfolio-limits]] (`PortfolioLimiter`), [[chart-generation]] (`render_candidate_chart`).
- Phase 1 `cli.py` (the `backtest` subcommand + helpers), `Store` (shipped).

## Approach

Additive to `cli.py` — **the only Phase 2 task that edits `cli.py`** (serialize per code-map; no other Phase 2 worker touches it). Reuse the runner's data-refresh + logging scaffolding. Integration tests run `scan`/`watchlist`/`chart` against cached fixtures with a mocked client, asserting JSON shape, PNG creation, and empty-approved-set behaviour.

## Open questions

- ~~Watchlist persistence~~ **RESOLVED:** `watchlist` SQLite table, run-timestamped (consistent with `approved_set`); `scan` persists + prints, `watchlist` reads it.
- Does `scan` always refresh live data, or honor a `--dry-run` (cache-only) like `backtest`? Lean: yes, mirror `backtest`'s `--dry-run`.

## Out of scope

- Hermes job / Discord ([[hermes-job-definitions]]), Claude assessment/narration ([[news-risk-assessment]], [[watchlist-narration]]).
