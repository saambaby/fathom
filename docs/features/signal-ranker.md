# Feature: signal-ranker

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Turn "all approved strategies evaluated against current data" into a short, ranked, filtered list of trade **candidates**. This is the deterministic core of Phase 2: it loads the approved-set (the INV-10 gate), evaluates only approved (strategy, pair, timeframe) combos against current candles, scores each by backtested expectancy × current signal quality, filters out poor conditions (wide spread, illiquid session, imminent high-impact news), resolves conflicts, and emits a ranked `Candidate` list. The `Candidate` shape is the contract every downstream Phase 2 feature consumes (CLI, narration, charts, the Hermes job).

## User-facing behaviour

Backend module `signals/ranker.py`. `Ranker(store, calendar)` with `rank(now: datetime) -> list[Candidate]`:

1. **Gate (INV-10):** `store.load_approved_set()` → the approved (strategy, pair, timeframe) rows. **If empty, return `[]`** (no signals — not all signals) and log it.
2. **Evaluate:** for each approved combo, run that strategy's `generate_signals` on the latest cached candles; take the most recent bar's `Signal` (if any).
3. **Filter:** exclude candidates where current spread exceeds an instrument threshold (`spread_ok=False` → dropped) or the instrument is in an illiquid session (`session_ok=False` → dropped).
4. **News gate (deterministic, calendar-based):** for either leg-currency, query `calendar.upcoming_events` within the news window. A **high-impact** event in-window ⇒ the candidate is **dropped** (hard pre-filter). A **medium-impact** event in-window ⇒ the candidate is **kept with `news_flag=True`** (so Claude/narration can mention it; the Claude news-risk layer in the Hermes job is the finer veto on survivors). Low/none ⇒ `news_flag=False`.
5. **Conflict policy (D-P2-1, lead ruling):** if two approved combos produce **opposite** directions on the **same (instrument, timeframe)**, suppress both. Different timeframes are ranked independently (no conflict).
6. **Rank (D-P2-4 / AMBIGUOUS-04 ruling):** sort survivors by **`oos_sharpe_mean` descending (primary)**, then **`quality_score` descending (tie-break)**. `quality_score` is *not* multiplied into a cross-strategy composite — `oos_sharpe_mean` is the INV-11-comparable validated number; `quality_score` (per-strategy [0,1]) only orders ties. Assign 1-based `rank`. Return the ranked list.

`Candidate` is a flat pydantic model — the frozen Hermes-facing wire contract (**INV-13**). Its exact fields are pinned in Component design below.

## Acceptance criteria

- [ ] Loads the approved-set via `store.load_approved_set()`; an empty approved-set → `rank()` returns `[]` and logs (INV-10 — no signals, not all signals).
- [ ] Only emits candidates for (strategy, pair, timeframe) combos present in the approved-set.
- [ ] Ranking is by `oos_sharpe_mean` descending (primary), `quality_score` descending (tie-break) — deterministic, with a final stable tie-break (instrument then strategy_name). No multiplicative cross-strategy composite.
- [ ] A candidate whose current spread exceeds the instrument's threshold is excluded (`spread_ok=False` → dropped); illiquid session → `session_ok=False` → dropped.
- [ ] A **high-impact** calendar event for either leg-currency within the news window ⇒ candidate **dropped**; a **medium-impact** event ⇒ candidate kept with `news_flag=True`; low/none ⇒ `news_flag=False`.
- [ ] The `Candidate` output matches the pinned field table exactly (INV-13); a serialisation round-trip test pins the JSON shape.
- [ ] The approved-set gate join uses `signal.timeframe == row['granularity']` (DRIFT-01).
- [ ] Same-(instrument, timeframe) opposite-direction conflict → both suppressed (D-P2-1).
- [ ] All timestamps UTC (INV-03); candidates carry their `Signal.generated_at`.
- [ ] Produces only candidates — never places or sizes orders (INV-01 boundary).

## Component design

`Ranker` composes existing pieces: `Store.load_approved_set` + `Store.load_candles` (data), the strategy registry (map `strategy_name` → `Strategy` instance, mirroring the runner's `_build_strategy`), and `EconomicCalendar.upcoming_events` (news gate). Pipeline stages are pure functions (gate → evaluate → filter → news → conflict → rank) so each is unit-testable in isolation.

**The `Candidate` wire contract (pinned — INV-13).** Flat, snake_case, the single authoritative field list every downstream spec ([[cli-commands]], [[chart-generation]], [[watchlist-narration]], [[hermes-job-definitions]]) builds against:

| Field | Type | Source / meaning |
|---|---|---|
| `instrument` | str | e.g. `EUR_USD` |
| `timeframe` | str | from `Signal.timeframe`; **same dimension the approved-set/DB calls `granularity`** |
| `strategy_name` | str | from `Signal` / approved-set row |
| `direction` | str | `"LONG"` \| `"SHORT"` |
| `entry_ref` | float | from `Signal` |
| `stop_distance` | float | from `Signal` (ATR-derived, INV-11) |
| `target_distance` | float | from `Signal` (RR multiple, INV-11) |
| `oos_sharpe_mean` | float | from the approved-set row — the validated expectancy + **primary rank key** |
| `quality_score` | float | from `Signal`, [0,1] — current signal strength; **tie-break only** |
| `rank` | int | 1-based position after sorting |
| `spread_ok` | bool | passed the spread filter |
| `session_ok` | bool | passed the session-liquidity filter |
| `news_flag` | bool | medium-impact event nearby (high-impact ⇒ dropped, never flagged) |
| `generated_at` | str | UTC RFC-3339 (the signal bar's close time, INV-03) |

`Candidate` flattens the relevant `Signal` fields (no nested object) so the `fathom watchlist` JSON is flat for Hermes/Discord. Do not leak raw dicts.

**INV-10 gate join (DRIFT-01 resolution):** `load_approved_set()` rows key the dimension as `row['granularity']`; `Signal` calls it `timeframe`. They are the **same dimension** — the gate match is `signal.instrument == row['instrument'] AND signal.strategy_name == row['strategy_name'] AND signal.timeframe == row['granularity']`. The `Candidate` exposes it as `timeframe` (human-facing, matching `Signal`).

## Non-goals

- No position sizing, no order placement, no risk limits (Phase 3 / INV-01).
- No LLM reasoning — the deterministic news gate here uses the calendar directly; the *Claude* news-risk assessment ([[news-risk-assessment]]) is a separate, Hermes-side layer applied in the daily job.
- No portfolio correlation/exposure caps — that is the next stage ([[portfolio-limits]]).

## Touches

- [INV-10] — the ranker is the enforcement point: empty approved-set ⇒ no candidates.
- [INV-11] — consumes `Signal`s whose ATR-derived stops make cross-strategy scores comparable.
- [INV-03] — UTC timestamps throughout.
- [INV-01] — output is a watchlist of candidates only; never orders.

## Depends on

- `Store.load_approved_set` / `load_candles` (shipped), `strategies/*` (`Signal`, `generate_signals`), `data/calendar.py` (`upcoming_events`, `CalendarEvent`, `Impact`) — all on `main`.

## Approach

A new `signals/` package. `Ranker.rank()` runs the pipeline; the strategy-name→instance mapping reuses the runner's construction logic (factor a shared `_build_strategy` if helpful). The deterministic news gate queries `calendar.upcoming_events([base_ccy, quote_ccy], news_window)` and flags high-impact hits. Conflict suppression operates on the `(instrument, timeframe)` group after scoring.

## Open questions

- **D-P2-1 conflict policy — RESOLVED (lead ruling, overridable):** suppress both on same-(instrument, timeframe) opposite-direction conflict; cross-timeframe ranked independently. Conservative / bounded-downside; revisit once a demo track record exists.
- News window length: how many hours before a high-impact event to veto? Propose **4h** for high-impact, 1h for medium. Confirm in Plan.
- Spread threshold source: per-instrument `typical_spread` from `InstrumentMeta` × a multiplier, vs a flat pip cap. Lean: `InstrumentMeta`-derived.

## Out of scope

- Portfolio limits ([[portfolio-limits]]), charts ([[chart-generation]]), the CLI surface ([[cli-commands]]), Claude assessment ([[news-risk-assessment]]).
