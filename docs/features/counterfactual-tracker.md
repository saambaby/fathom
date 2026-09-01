# Feature: counterfactual-tracker

**Status:** ready
**Phase:** phase-09
**Owner:** saambaby
**Last updated:** 2026-09-01

## Summary

Replay each [[veto-ledger]] row as a would-be bracket trade over **already stored**
candles, using the backtest engine's fill and cost rules — including the
stop-before-target within-bar tie-break. The ledger itself stays append-only;
outcomes live in a sibling table that may upsert. Without this replay, veto
hit-rate is fiction: a `block` that would have been a stop-out is a save, a
`block` that would have been a target is opportunity cost. Aggregation is
[[veto-report]]; this feature only resolves outcomes.

## User-facing behaviour

- `fathom veto-report --refresh [--db-path PATH]` walks ledger rows as in
  Component design. The **aggregate printer** is [[veto-report]] (this
  command’s default path). `--refresh` only runs `refresh_counterfactuals`;
  stdout of counts is owned by [[veto-report]] (`refresh` JSON field).
  Standalone `--refresh` JSON from this spec is used only in tracker unit
  tests / as the `RefreshCounts` return value — the CLI handler after
  [[veto-report]] lands must not exit 2 when `--refresh` is omitted.
- No OANDA fetch. Missing candles → `outcome="unknown"`, never a live pull.
- `--refresh` is idempotent: a second run with the same candle set leaves
  terminal rows unchanged; `unknown` may upgrade to a terminal outcome when
  candles later cover the horizon.
- Operator-declined ledger rows **are** replayed (same `simulate_bracket` as
  pretrade/news-risk). Recording them was cheap; replaying them is the same
  helper. Phase-09's "if cheap, it rides along" scoping assumption applies.

## Acceptance criteria

1. `BacktestEngine.simulate_bracket` (new public method) opens at the **next
   bar's open after** the candidate's `generated_at` (engine entry convention
   at `backtest/engine.py:249-256`, not `Candidate.entry_ref` / not
   `build_bracket` which anchors to `entry_ref` at `execution/models.py:319-324`).
   Stop and target are distances from that fill (`_open_from_signal`,
   `backtest/engine.py:366-388`). Within a bar, if both levels breach, the stop
   wins (`_resolve_fill`, `backtest/engine.py:399-426`). A test feeds a fixture
   bar that breaches both and asserts `outcome="stop"` (same as
   `test_known_short_trade_stop_wins_tie`, `tests/test_backtest_engine.py:687-718`)
   and a clamped fill inside `[low, high]` (`test_stops_fill_within_bar`,
   `tests/test_backtest_engine.py:741`).
2. Horizon is **`HORIZON_BARS = 20` fill-check bars including the entry bar**
   (`k ∈ [0, 19]`; entry bar is `k=0` and is fill-checked after the next-bar-open
   entry, same as `backtest/engine.py:249-260`). If stop or target hits on any
   of those 20 bars, persist `outcome` `stop`|`target`. If the store has **20
   session rows from the entry bar onward** (weekend calendar holes are just
   missing timestamps in `load_candles`; they are not an extra `unknown` cause)
   and neither level hits → `timeout` (`Trade.exit_reason="end_of_data"`). If
   fewer than 20 rows exist from the entry bar onward → `unknown`. No ad hoc
   fetch. A "gap" is only a short row count, not a detected hole inside a
   complete 20-row slice.
3. Refresh is idempotent on terminal rows: a test writes a `stop` outcome, mutates
   nothing in candles, re-runs `--refresh`, and asserts the outcomes row is
   byte-equal on `outcome`, `r_multiple`, `exit_reason`, timestamps. `unknown`
   rows are retried. `INSERT OR REPLACE` / UPSERT is allowed **only** on
   `veto_ledger_outcomes`, never on `veto_ledger` ([[veto-ledger]] AC 4).
4. `r_multiple` is `pnl_net_pips / stop_distance_pips` where
   `stop_distance_pips = candidate.stop_distance / pip_value` and `pip_value`
   comes from `InstrumentMeta.pip_location` via `_pip_value_from_location`
   (`cli.py:424-426`). Costs use the same documented defaults as the universe
   runner (`cli.py:419-421`: spread 1.5, slippage 0.5, commission 0.0) plus
   `InstrumentMeta.long_rate`/`short_rate` (`data/oanda_client.py:56`; loaded via
   `data/store.py:694`). Missing
   instrument meta → `unknown`, not a cost-free run (INV-06).
5. `--refresh` never calls `submit_order`, `size_position`, or OANDA. AST/import
   test: `eval/counterfactual.py` imports `backtest.engine`, `data.store`,
   `signals.ranker.Candidate` — not `execution.orders`, not `execution.models`,
   not `risk.sizing`, not `cli`.
   Failure to simulate one row logs WARNING and writes `unknown` for that id;
   other rows still process (isolation is per-row, not process-abort).
6. A round-trip: ledger fixture whose store has **20 session rows from the
   entry bar**; the long hits target on fill-check bar `k=2` (bars `k=3..19` are
   present and do not hit). `--refresh` then
   `load_veto_ledger_outcomes(ledger_id=...)` returns `outcome="target"`. A
   3-row-only store for the same ledger id must yield `unknown`, not `target`.
   UTC-aware `entry_time`/`exit_time` from bar data (INV-03, never
   `datetime.now()` inside the simulator). `source` is not stored on the
   outcomes row (join via `ledger_id`).

## Sequence diagram

```mermaid
sequenceDiagram
    actor Op as Operator
    participant CLI as fathom veto-report --refresh
    participant TR as eval.counterfactual
    participant ST as store
    participant EN as BacktestEngine.simulate_bracket

    Op->>CLI: --refresh
    CLI->>TR: refresh_counterfactuals(db_path, now)
    TR->>ST: load_veto_ledger_rows()
    TR->>ST: load_veto_ledger_outcomes()
    loop each ledger row
        alt already stop|target|timeout
            Note over TR: unchanged_terminal
        else generated_at parsed UTC >= now
            Note over TR: skipped_future; no outcomes write
        else
            TR->>ST: load_candles from generated_at to generated_at+400d
            alt missing InstrumentMeta or stop_distance/target_distance <= 0
                TR->>ST: upsert outcome unknown
            else fewer than 20 session rows from entry bar
                TR->>ST: upsert outcome unknown
            else
                Note over TR: truncate to HORIZON_BARS from entry bar
                TR->>EN: simulate_bracket(candles20, direction=..., stop_distance=..., target_distance=...)
                EN-->>TR: stop|target|timeout + Trade
                TR->>ST: upsert veto_ledger_outcomes
            end
        end
    end
    CLI-->>Op: (no stdout from tracker; RefreshCounts to caller)
```

## Component design

**Extract, do not wrap `BacktestEngine.run`.** `run` (`backtest/engine.py:166`)
always calls `strategy.generate_signals` and walks the full frame. A single
synthetic bracket must not drag in the walk-forward harness (phase-09 scoping
assumption — verified: there is no `simulate_bracket` today). Add:

**Who loads candles:** `refresh_counterfactuals` only. It parses
`Candidate.generated_at` with `datetime.fromisoformat` (accept a trailing `Z` by
replacing with `+00:00`) to a UTC-aware `datetime`. It calls
`load_candles(instrument, granularity=candidate.timeframe, start=generated_at, end=generated_at + 400 days)`
so a weekend can still yield 20 H1 **session** rows. Then it takes the first
candle **strictly after** `generated_at` as the entry bar and **truncates to
exactly `HORIZON_BARS` rows** (or fewer → `unknown`, no engine call). Extra
rows beyond 20 are dropped by the tracker; `simulate_bracket` fill-checks every
row it is given and is only called with `len(candles)==20`. `unknown` is a
**tracker** status only — never returned by the engine.

`refresh_counterfactuals` builds `BacktestEngine(store, CostParams(spread_pips=1.5,
slippage_pips=0.5, commission_pips=0.0, pip_value=_pip_value_from_location(meta.pip_location),
swap_long_rate=meta.long_rate, swap_short_rate=meta.short_rate))` once per
instrument (AC 4). `Direction(candidate.direction)` converts the INV-13 string.

```python
def simulate_bracket(
    self,
    candles: pd.DataFrame,
    *,
    direction: Direction,
    stop_distance: float,
    target_distance: float,
) -> SimulateBracketResult: ...
```

`candles` is the hold window: row 0 is the entry bar, `len(candles)==HORIZON_BARS`. `SimulateBracketResult`: `status: Literal["stop","target","timeout"]`
plus a `Trade` (timeout closes at the last window bar's **close**,
`exit_reason="end_of_data"`, `backtest/engine.py:80`). `len(candles)!=HORIZON_BARS` raises `ValueError`. The engine does not
return `unknown` and does not call `load_candles`.

**Entry/exit loop (pinned):**

1. Tracker fetch as above; empty or no row strictly after `generated_at` or
   fewer than `HORIZON_BARS` rows from the entry bar → upsert `unknown` (do not
   call `simulate_bracket`).
2. Open at row 0 `open_bid` with stop/target from distances (copy
   `_open_from_signal`). Do **not** use `build_bracket` / `entry_ref`.
3. Fill-check rows `k = 0 .. HORIZON_BARS-1` (entry bar included). Hit →
   `_close_trade`. All 20 present, no hit → timeout at row 19 close.

**Tracker module:** `eval/counterfactual.py`

```python
def refresh_counterfactuals(db_path: str | Path, *, now: datetime) -> RefreshCounts: ...
```

`now` is injected (CLI passes `datetime.now(timezone.utc)`); fill timestamps
come from candles only. **Single skip predicate:** `generated_at >= now` →
`skipped_future`, no outcomes row. A signal that is already in the past but
whose next bar is not in the store yet → `unknown` (retry), not `skipped_future`.
Do not also skip on “entry bar still in the future.” `eval/` does not read
`settings.env`.

**Outcomes table** `veto_ledger_outcomes`:

UPSERT on `ledger_id`. Terminal statuses are sticky (AC 3). `unknown` is
overwriteable.

**CLI (owned by [[veto-report]] after that spec lands):** this module exposes
`refresh_counterfactuals` → `RefreshCounts`. It does **not** print operator
stdout. Tracker unit tests assert the return value; smoke that used to look
for `resolved>=1` on stdout now asserts `RefreshCounts.resolved >= 1`.

**`eval*`** already queued in [[veto-ledger]] Approach for `pyproject.toml`.

## Artefact verdicts

- Sequence diagram: include — operator, CLI, tracker, store, engine (≥3 actors;
  store vs engine ordering is load-bearing).
- Component design: include — `simulate_bracket` vs `run`, entry convention vs
  `build_bracket`, horizon vs `unknown`.
- User flow: skip — CLI count line; no frontend empty/loading/error chrome
  beyond stderr WARNING per failed row.

## Non-goals

- No aggregate hit-rate / net-R table — [[veto-report]].
- No change to live/demo execute or analyze gates.
- No candle refresh / OANDA history pull.
- No panel view.
- No acting on outcomes (no auto-disable of a veto).
- Does not write `positions` / `fills` — this is not broker P&L (INV-16 does
  not apply; must not pretend it does).

## Touches

- [INV-01] — `eval/counterfactual.py` has no order authority; must not import
  placement/sizing.
- [INV-03] — fill timestamps from candle `time`; `now` injected at refresh
  only for "future signal" skip.
- [INV-04] / [INV-11] — replay uses the candidate's stop **and** target
  distances; a non-positive distance → `unknown` (do not simulate a naked
  trade).
- [INV-06] — `CostParams` with spread > 0; missing instrument meta → `unknown`.
- [INV-09] — tracker does not read `settings.env`; it is store-local
  measurement (ledger rows are already demo-only per [[veto-ledger]]).
- [INV-13] — deserialize `candidate_snapshot` to frozen `Candidate`.

## Events

- Written: `veto_ledger_outcomes` upserts.
- Consumed: `veto_ledger` rows, `candles`, `instruments`.

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| _(none)_ | Tracker is store-local; no `ENV` branch | — | — |

## Wire-format contract

`veto_ledger_outcomes` (SQLite; writer: `refresh_counterfactuals` only):

| Column | Type | Written by | Notes |
|---|---|---|---|
| `ledger_id` | INTEGER PK | tracker | FK to `veto_ledger.id` |
| `outcome` | TEXT | tracker | `"stop"` \| `"target"` \| `"timeout"` \| `"unknown"` |
| `r_multiple` | REAL NULL | tracker | NULL when `unknown`; else net-pips / stop-distance-pips |
| `exit_reason` | TEXT NULL | tracker | engine `"stop"` \| `"target"` \| `"end_of_data"`; NULL when `unknown` |
| `entry_time` | TEXT NULL | Store `_to_rfc3339` | UTC RFC-3339 from entry bar; NULL when `unknown` |
| `exit_time` | TEXT NULL | Store `_to_rfc3339` | UTC RFC-3339; NULL when `unknown` |
| `pnl_net_pips` | REAL NULL | tracker | from `Trade.pnl_net_pips`; NULL when `unknown` |
| `resolved_at` | TEXT | Store `_to_rfc3339(now)` | refresh clock (injected `now`), not a fill time |

`--refresh` stdout (this spec): one JSON object, snake_case. Integers are
disjoint except `refreshed = resolved + unknown`:

| Key | Meaning |
|---|---|
| `unchanged_terminal` | existing outcome in `{stop,target,timeout}` — not upserted |
| `skipped_future` | `generated_at >= now` — no outcomes write |
| `resolved` | this-run upserts with `outcome` in `{stop,target,timeout}` (`timeout` counts here) |
| `unknown` | this-run upserts with `outcome="unknown"` (including retries still unknown) |
| `refreshed` | `resolved + unknown` (every upsert this run) |

[[veto-report]] owns any richer printer.

## Depends on

- [[veto-ledger]] — `load_veto_ledger_rows`, `id`, frozen `Candidate` snapshot.
- `backtest/engine.py` — `_resolve_fill`, `_open_from_signal`, `_close_trade`,
  `Trade.exit_reason`.
- `data/store.py::load_candles` (`:527`) / `load_instruments` (`:694`).
- Universe cost defaults — `cli.py:419-426` (do not duplicate a second
  undocumented spread). Extracting a shared helper is allowed at taskgraph;
  this spec may copy the four constants + `_pip_value_from_location` into
  `eval/counterfactual.py` if extracting would touch the runner in the same
  slice.

## Approach

1. TDD `simulate_bracket` with **20-row** frames (`len != 20` raises
   `ValueError`). Pad a both-breach on `k=0` and a target-hit on `k=2` with
   inert bars; compare `_resolve_fill` on those rows. Do not pass the
   engine-`run` signal+entry layout (bar 0 is entry, not the signal bar).
2. Outcomes DDL + `upsert_veto_ledger_outcome` /
   `load_veto_ledger_outcomes(*, ledger_id: int | None = None) -> list[VetoLedgerOutcomeRow]`
   (`ledger_id=` filters to one row).
3. `refresh_counterfactuals` skip-terminal / retry-unknown / per-row WARNING.
4. Python API `RefreshCounts`; CLI registration is [[veto-report]].
5. Do not implement the aggregate printer.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| Engine has no single-bracket API; `run` always takes a `Strategy` | `backtest/engine.py:166` | read `def run` signature and body start (loads candles, `generate_signals`) |
| Next-bar-open entry after signal bar | `backtest/engine.py:249-256` | read loop step 1 + module docstring lines 18-20 |
| Stop/target from **fill** open, not reference price | `backtest/engine.py:366-388` | read `_open_from_signal` |
| `build_bracket` uses `entry_ref` ± distances (wrong for this replay) | `execution/models.py:319-324` | read docstring maths |
| Stop wins if both breach same bar; clamp to bar range | `backtest/engine.py:399-426` | read `_resolve_fill` |
| `Trade.exit_reason` is `stop` \| `target` \| `end_of_data` | `backtest/engine.py:80` | read field comment |
| `load_candles` returns empty frame on miss, UTC-aware bounds required | `data/store.py:527-558` | read docstring |
| `Candidate.generated_at` is signal bar close, RFC-3339 | `signals/ranker.py:113` | read class docstring |
| `Candidate` has `stop_distance` / `target_distance` / `entry_ref` | `signals/ranker.py:121-124` | read fields |
| Cost defaults 1.5 / 0.5 / 0.0 and pip from `pip_location` | `cli.py:419-426` | read constants + helper |
| CLI subparsers live at `cli.py:708` | `cli.py:708` | grep `add_subparsers` |
| `veto_ledger` has no outcome columns; outcomes are a sibling keyed by `id` | [[veto-ledger]] wire table | read ready spec |

## Constraint blast radius

**New constraint: terminal outcomes are sticky (no re-simulate of `stop`/`target`/`timeout`).**
- **Protects:** a later candle-archive rewrite cannot silently change a published
  counterfactual used by [[veto-report]] / phase-10.
- **Blocks:** recomputing a timeout if you later decide the horizon should be
  40 bars — that is a new `ledger_id` world (or a future `--force`, out of
  scope).

**New constraint: no ad hoc market-data fetch.**
- **Protects:** the phase honesty rule (unknown ≠ made-up timeout).
- **Blocks:** "just pull H1 from OANDA so this row resolves."

## Smoke checklist hooks

- Seed a demo DB with one ledger row + **20** candles from the entry bar that
  hit target on `k=2`; `refresh_counterfactuals(...)` returns
  `resolved>=1`; second run `unchanged_terminal>=1`.
- Delete those candles; a **new** ledger id (not the sticky terminal one)
  resolves `unknown`.

## Open questions

- None load-bearing. Timeout closes at last-bar close with `end_of_data` (this
  spec). [[veto-report]] may later bucket timeout separately in the printer
  without changing this table.

## Out of scope

- Aggregate report printer and breakdowns — [[veto-report]].
- `--force` re-resolution of terminal rows.
- Live-account ledger rows (none exist until INV-07; tracker does not filter
  `ENV` itself).
- Changing `HORIZON_BARS` per timeframe.

## Notes

Horizon count is intentionally crude (20 bars on D ≠ 20 bars on H1 in calendar
time). That is the measurement convention for phase-09; phase-10 may split by
TF. Pinning one integer beats an unpinned "trade horizon" in the phase doc.
