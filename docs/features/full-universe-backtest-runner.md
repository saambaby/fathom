# Feature: full-universe-backtest-runner

**Status.** ready
**Phase.** Phase 1
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The Phase 1 integration capstone: a `fathom backtest` CLI command (`cli.py`) that runs walk-forward validation across **every** (strategy, pair, timeframe) combination in the full universe and writes the resulting approved-set table to the database. This is where all the Phase 1 pieces join — the expanded data layer, the swap-aware cost model, and all five new strategies plus the existing MA crossover. Its output (the approved-set) is the gate Phase 2's ranker consumes (INV-10). It also designs in the two lessons the PoC surfaced: **daily-timeframe starvation** and the **per-window approval gate**.

## User-facing behaviour

CLI command:

```
fathom backtest [--instruments ALL|EUR_USD,...] [--timeframes H1,H4,D]
                [--strategies all|macrossover,donchian,...]
                [--workers N] [--db-path PATH]
```

Runs every requested combination through `WalkForwardValidator`, prints a summary of approved entries (strategy, pair, timeframe, mean OOS Sharpe, total OOS trades, `swap_modelled`), and **persists the approved-set table to the database** so Phase 2 can read it. Empty approved-set is a valid, exit-0 result (carried from the PoC).

## Acceptance criteria

- [ ] `fathom backtest` discovers the full FX universe via `list_instruments()` (or an explicit `--instruments`) and runs all (strategy, pair, timeframe) combos.
- [ ] Walk-forward uses the swap-aware cost model ([[swap-cost-model]]) — every result is INV-06-valid (`swap_modelled=True` where financing applies).
- [ ] **Approved-set table is persisted to the DB** (new table), not just printed — Phase 2's ranker reads it (INV-10). Schema mirrors the shipped `ApprovedSetEntry`: `strategy_name`, `instrument`, **`granularity`** (the shipped field name — not "timeframe"), `oos_sharpe_mean`, `oos_trade_count_total`, `swap_modelled`, plus a DB-table-only `run_timestamp` (UTC RFC 3339). `ApprovedSetEntry` itself is unchanged — `run_timestamp` is added at the persistence layer only (audit DRIFT-03).
- [ ] **Per-timeframe window sizing** so the daily timeframe is not structurally starved (the PoC's daily combos got 0–2 trades on 3-month windows). Longer test windows for D (and H4); documented per-timeframe `train_months`/`test_months`.
- [ ] **Approval gate** stays strict per-window (Sharpe>0 AND ≥5 trades every OOS window — carried from the PoC ruling) unless the per-timeframe sizing makes a different gate appropriate; the chosen gate is documented and applied uniformly.
- [ ] Multi-process execution (`--workers N`) so ~1,700 combos complete in reasonable wall-clock; results are deterministic regardless of worker count.
- [ ] All run timestamps UTC RFC 3339 (INV-03); no credentials logged (INV-08).
- [ ] Empty approved-set exits 0 with a clear message (PoC behaviour preserved).

## Component design

New `cli.py` with a `backtest` subcommand (argparse or click — see Open questions). It composes existing pieces: `list_instruments()` → for each (strategy, pair, timeframe), construct `BacktestEngine(store, CostParams(...with swap...))` + the strategy, run `WalkForwardValidator.run(...)`, collect `ApprovedSetEntry` objects. A new `approved_set` SQLite table persists them. Multi-process via `concurrent.futures.ProcessPoolExecutor` over the combo list (each combo is independent → embarrassingly parallel); the engine already takes a defensive DataFrame copy, so workers don't share mutable state. Per-timeframe window config is a small table/dict consulted per run.

**Write concurrency (INV-12, resolving audit AMBIGUOUS-03):** worker processes return `ApprovedSetEntry` objects to the parent; **all** `approved_set` inserts happen in the parent process, after every future is collected, in a single `BEGIN…COMMIT` transaction. No worker writes to the DB directly — this is a committed design point, not an open question.

**Persistence contract:** the `approved_set` table is the INV-10 gate. Phase 2's ranker loads it at startup and refuses to operate if empty. Schema (mirroring `ApprovedSetEntry` + a DB-only `run_timestamp`) and the "empty = no signals, not all signals" semantics are owned here.

## Non-goals

- No vectorised pre-screen backtester (D-P1-1: skip — one engine, done right).
- No signal ranking / portfolio logic (Phase 2).
- No live trading (INV-07).

## Touches

- [INV-06] — all results swap-aware (depends on [[swap-cost-model]]).
- [INV-10] — owns the approved-set table that gates Phase 2 signals.
- [INV-11] — consumes strategy Signals whose ATR/RR stops make OOS Sharpe comparable across strategies.
- [INV-12] — single-writer, parent-serialized approved-set writes.
- [INV-03] — UTC timestamps. [INV-08] — no logged secrets. [INV-09] — account-scoped via `settings.env`.

## Depends on

- [[data-layer-expansion]] — universe discovery + metadata (pip values, financing rates).
- [[swap-cost-model]] — INV-06-valid costs.
- [[donchian-breakout]], [[bollinger-zscore-reversion]], [[rsi-reversion]], [[roc-momentum]], [[session-range-breakout]] — the strategies it validates (plus the existing `MACrossover`).
- PoC `backtest/walkforward.py`, `engine.py`, `metrics.py` — exist on `main`.

## Approach

A thin orchestration layer over already-tested components — most of the risk is in the *composition* (correct per-timeframe windows, deterministic parallelism, the persisted gate schema), not new algorithmic code. Build the combo list, fan out over a process pool, collect `ApprovedSetEntry` results, write the `approved_set` table. This is the join point of the Phase 1 DAG and should be the **last** spec drafted and the last task dispatched.

## Open questions

- **D-P1-2 / D-P1-3 (window sizing + gate):** what `train_months`/`test_months` per timeframe, given daily starvation? Candidates: H1 = 12/3 (PoC); H4 = 18/6; D = 24/6 or longer. And does the strict per-window gate survive for D, or do low-frequency timeframes get a per-timeframe trade floor? **Needs a ruling in Plan** — this directly determines what gets approved.
- **D-P1-5 (scale):** ~70 pairs × 6 strategies × 3 timeframes ≈ 1,260–1,700 combos. Confirm `ProcessPoolExecutor` worker count. (Write serialization is settled — parent-only writes per INV-12; see Component design.)
- CLI framework: argparse (stdlib, no new dep) vs click (nicer UX). (Lean: argparse — the PoC runner already uses it; no new dependency.)

## Out of scope

- The `scan|watchlist|chart` CLI subcommands (Phase 2).
- Re-architecting the engine for speed beyond process-level parallelism.
