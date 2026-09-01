# Feature: review-command

**Status:** draft
**Phase:** phase-08
**Owner:** saambaby
**Last updated:** 2026-09-01

## Summary

`fathom review` — the operator's "what should I look at" command. Assembles open
positions, the most recent broker-reconciled account snapshot, upcoming
high/medium-impact calendar events for the instruments in play, and the recent
deviation log into one context pack, hands it to the LLM via `ai/companion.py`
(`companion-core.md`), and prints a per-item "worth investigating?" flag with a short
reason for each. `fathom review --deviations` folds in a plain-English explanation of
each recent deviation-log entry (phase doc's "Deviation-log explainer", item 4) rather
than shipping as a separate command. Nothing here reads live broker state or triggers
a reconcile pass — see Grounded claims for why, and the corrected scoping verdict this
forces relative to the phase doc's framing.

## User-facing behaviour

```
fathom review [--db-path PATH] [--deviations] [--limit N]
```

1. Load open positions: `store.load_open_positions()`.
2. Load the last-reconciled account snapshot: `store.load_account_state()` (freshness
   is surfaced, not assumed — see Non-goals/Grounded claims: this is **not** a fresh
   reconcile).
3. Load recent deviation-log rows: `store.load_deviation_log(limit=N)` (default
   `N=20`, `--limit` overrides). Always loaded (not gated by `--deviations`) so
   `review`'s anomaly pass can cross-reference a position against its own deviation
   history even without `--deviations`.
4. For each open position's instrument, derive the ISO 4217 currency pair
   (`instrument.split("_")`) and query
   `FairEconomyCalendar(db_path).upcoming_events(currencies, window=timedelta(hours=48))`
   — a **local-DB-only** read (no HTTP fetch; see Grounded claims) — for medium/high
   impact events in the next 48h.
5. Build a `ContextPack` (`companion-core.md`) of kind `"review"` from the above, and
   call `run_companion_call(prompt, response_model=ReviewResponse, fallback=_offline_review(...))`.
6. Print each `ReviewFinding` (subject, `worth_investigating` flag, note) as a table;
   if `--deviations`, also print `ReviewFinding`s for each loaded deviation-log row
   (`subject="deviation:<event_id>"`).
7. With `LLM_API_KEY` unset (or any call/parse failure), print the deterministic
   fallback: the raw positions/account-state/deviation tables with **no** LLM
   commentary and the line `"analysis unavailable — showing raw data only"`; exit 0.

## Acceptance criteria

1. `fathom review` with no open positions and an empty deviation log prints "nothing
   to review" and exits 0 without calling the LLM (no context pack worth sending —
   avoids a wasted call and a hallucinated "no findings" narrative built from nothing).
2. `fathom review` with ≥1 open position and `LLM_API_KEY` set (or an injected stub
   client in tests) prints a non-empty finding per position, each tagged
   `worth_investigating: true|false` with a `note`.
3. `fathom review` with `LLM_API_KEY` unset prints the deterministic fallback (raw
   tables + "analysis unavailable") and exits 0 — never crashes, never fabricates a
   flag (mirrors `companion-core.md` AC2).
4. `fathom review --deviations` includes one `ReviewFinding` per loaded deviation-log
   row in addition to the position findings; `fathom review` (no flag) does not print
   deviation-log findings even though it still loads the log for cross-referencing
   (AC1's data load vs. AC4's display are independently verified).
5. A position whose instrument has a medium/high-impact calendar event in the next 48h
   (from `upcoming_events`, local DB only) surfaces that event in the context pack
   passed to the LLM — verified by asserting the built prompt/context contains the
   event's `event_name`, without asserting on the LLM's own judgement of it.
6. An AST boundary test (extending `companion-core.md`'s pattern) asserts
   `ai/review.py` imports no order/risk-placement module and does not call
   `execution.reconcile.reconcile` or open a live `OandaClient` — `review` reads the
   store (and the calendar module's local-DB-only query) exclusively.
7. `fathom review` never modifies `positions`, `account_state`, or `deviation_log` —
   verified by asserting the store's mtime/row-counts are unchanged across a run
   (read-only in practice, not just by import-graph proof).

## Sequence diagram

Skip — see Artefact verdicts.

## Component design

New CLI subcommand `review` in `cli.py` (thin: arg parsing + store wiring), backed by
a new `ai/review.py`:

- `ReviewFinding` (pydantic): `subject: str` (`"position:<broker_trade_id>"` or
  `"deviation:<event_id>"`), `worth_investigating: bool`, `note: str`.
- `ReviewResponse` (pydantic, `extra="forbid"`): `findings: list[ReviewFinding]`,
  `summary: str`.
- `_offline_review(positions, account_state, deviation_rows) -> ReviewResponse` — the
  `companion-core.md` `fallback` argument: a deterministic, LLM-free
  `ReviewResponse` built purely from rule checks already computable in Python
  (see below), so "analysis unavailable" still carries *some* signal rather than an
  empty list.
- Two cheap deterministic rule checks run **before** the LLM call and are folded into
  the context pack (not gated on the LLM being available — they also populate
  `_offline_review`'s findings):
  - **Unusual stop distance:** for each position, `rr_actual = abs(take_profit_price - entry_price) / abs(entry_price - stop_loss_price)`; flag if `rr_actual` deviates from the INV-11 default `rr_ratio` (1.5) by more than a configurable fraction (default 30%). Self-contained on the `positions` row — no watchlist join needed (candidate_ref's `instrument:timeframe:strategy_name` shape doesn't carry the originating `run_timestamp`, so a precise watchlist-row join is ambiguous across repeated runs — see Grounded claims).
  - **Calendar proximity:** a position whose instrument's currency pair has a
    medium/high-impact event in the next 48h (step 4 above) is flagged
    `worth_investigating: true` by the offline path unconditionally; the LLM path may
    additionally reason about direction/relevance.
- `--deviations` is a pure display-time filter over already-loaded deviation rows —
  it does not change what is queried from the store (AC4's split).

## User flow

Skip — see Artefact verdicts.

## Artefact verdicts

- Sequence diagram: skip — one synchronous request/response through the shared
  `companion-core.md` call shape (itself skipped for the same reason); no new actor
  or cross-boundary coordination — "User-facing behaviour" above fully covers the
  flow in prose.
- Component design: include — the offline fallback's rule checks and the
  `ReviewFinding`/`ReviewResponse` shapes need to be pinned so `journal.md` and
  `ask-command.md` (siblings sharing `companion-core.md`) don't reinvent a
  differently-shaped finding model.
- User flow: skip — CLI-only, no frontend surface (phase doc: "Panel views for
  journal/review — CLI only this phase").

## Non-goals

- No fresh reconcile pass and no live OANDA read — `review` reads whatever
  `account_state`/`positions` the **last** `fathom execute` or `fathom reconcile` run
  left behind. It is explicitly a **stale-tolerant** view, not a live one; the
  printed `account_state.as_of` timestamp is how the operator judges staleness. See
  Grounded claims for why the phase doc's "latest reconcile report" framing needed
  correcting.
- No write path to broker, risk, or execution modules (phase doc, Out of scope) —
  `review` never calls `execution.reconcile.reconcile`, `risk.sizing`, or
  `execution.orders`.
- No historical trend/pattern analysis across many reviews — that is `journal show|summarize`'s job (`journal.md`), not `review`'s.
- No live calendar fetch — `upcoming_events` is a local-DB read only; if the operator
  hasn't run whatever refreshes `calendar_events` recently, the calendar-proximity
  check silently sees a stale/empty table (same staleness posture as
  `account_state`).

## Touches

- INV-01 — read-only: store + local-DB calendar reads only, no broker write, no
  execution/risk import (AST boundary test, AC6).
- INV-02 — applies the discipline by extension via `companion-core.md`'s
  `run_companion_call` (advisory-only, see `companion-core.md` Notes).
- INV-03 — all timestamps read/displayed (`account_state.as_of`,
  `deviation_log.created_at`, calendar `time`) are already-UTC-RFC-3339 per their
  owning tables; `review` does no timestamp math of its own beyond the 48h window.
- INV-11 — the unusual-stop-distance rule check's baseline (`rr_ratio` default 1.5) is
  the same constant INV-11 pins for signal generation; `review` reads it as a
  reference, does not enforce or alter it.
- INV-16 — `review` surfaces `account_state`/`positions` as **broker-truth-as-of-last-reconcile**, consistent with INV-16's "broker is truth" framing, but explicitly does not re-assert that truth itself (no live read).

## Events

- Written: none.
- Consumed: `positions`, `account_state`, `deviation_log` (store reads), local
  `calendar_events` (via `data/calendar.py`'s `upcoming_events`, store read).

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| `LLM_API_KEY` | Enables the live review LLM call; unset → deterministic fallback | runtime | `.env` (existing, reused via `companion-core.md`) |

No new env vars beyond the three `companion-core.md` already documents (`LLM_MODEL`,
`LLM_BASE_URL` are used identically, omitted here to avoid re-listing).

## Wire-format contract

Request: `run_companion_call`'s prompt embeds the `ContextPack.data` for kind
`"review"` — `positions: list[dict]` (the `Position` fields, INV-14 shape),
`account_state: dict | None`, `deviation_log: list[dict]` (`deviation_log` column
shape, see Grounded claims), `calendar_events: list[dict]` (`currency`,
`event_name`, `time`, `impact`). Response: JSON parsed against `ReviewResponse`
(`findings: [{subject, worth_investigating, note}]`, `summary: str`) — malformed
JSON, an extra field (`extra="forbid"`), or a non-bool `worth_investigating` all fall
through to `_offline_review`'s fallback per `companion-core.md`'s parse boundary.

## Depends on

- `companion-core.md` (`run_companion_call`, `ContextPack`).
- `data/store.py`'s `load_open_positions`, `load_account_state`, `load_deviation_log`
  (all shipped, Phase 3/3 — see Grounded claims).
- `data/calendar.py`'s `FairEconomyCalendar.upcoming_events` (shipped, Phase 1B —
  see Grounded claims); phase-08's Out-of-scope explicitly says "no new data
  capture" — this reuses the existing local read, adds no fetch.

## Approach

Add `ai/review.py` with the two response models, the offline rule checks, and
`run_review(store, *, deviations: bool, limit: int, client=None) -> ReviewResponse`;
wire a thin `cmd_review` in `cli.py`. Unit-test the offline rule checks against
synthetic `Position` rows first (pure functions, no store), then the store-integration
path with an in-memory SQLite store, then the LLM branches via an injected stub
client exactly as `companion-core.md` prescribes.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| `load_open_positions` returns the INV-14 `Position` shape from a plain store read | `data/store.py:1137-1178` | Opened file, read method |
| `load_account_state` returns `{start_of_day_equity, day_pl, as_of}` or `None`; no separate persisted "reconcile report" table exists anywhere in the store | `data/store.py:1309-1328`, plus a full grep of `data/store.py` `CREATE TABLE` statements (lines 106-401) shows no `reconcile_report`/`reconcile_log` table — only `candles`, `instruments`, `approved_set`, `watchlist`, `orders`, `fills`, `positions`, `account_state`, `deviation_log`, `equity_snapshots`, `preflight_attestations` | Opened file, read method + grepped `CREATE TABLE` |
| `ReconcileReport` (`adopted`/`closed`/`matched`/`drift_flags`) is a plain in-memory `@dataclass` returned by `reconcile()`, never written to the store — `execution/reconcile.py` has no `store.write_*` call for a reconcile-report row; `drift_flags` is only appended in-memory and logged at WARNING | `execution/reconcile.py:161-176` (dataclass def), and the `reconcile()` body at `execution/reconcile.py:444-560` (store writes only touch `positions`/`account_state`/`equity_snapshots`) | Opened file, read dataclass + function body, grepped `store.write` calls |
| `load_deviation_log` column shape: `event_id, instrument, deviation_type, detail, broker_trade_id, severity, created_at, delivered` | `data/store.py:1403-1459` | Opened file, read method |
| `FairEconomyCalendar.upcoming_events(currencies, window)` queries only the local `calendar_events` table (`SELECT ... FROM calendar_events WHERE ...`), performing no HTTP call | `data/calendar.py:310-365` | Opened file, read method body — no `self._fetch`/`httpx` call in this method (contrast with `fetch_and_persist`-style methods that do call `self._fetch` at line ~289) |
| `candidate_ref` on `positions`/`orders` is `f"{instrument}:{timeframe}:{strategy_name}"` with no `run_timestamp` component, so it cannot be joined back to one specific watchlist row when the same (instrument, timeframe, strategy) combination has appeared across multiple watchlist runs | `execution/models.py:125,138,403-415`; `docs/features/order-model-and-brackets.md:48` | Opened file, read field + docstring; cross-checked spec |
| INV-11's default `rr_ratio` is 1.5 | `docs/product/invariants.md:114` | Opened file, read invariant text |
| The phase doc frames `review-command`'s input as "positions + **latest reconcile report** + deviation log" | `docs/phases/phase-08/phase.md` line 24-26 (In scope, item 1) | Opened file, read scope item |

## Constraint blast radius

- New constraint: `review` may not trigger `execution.reconcile.reconcile` or open a
  live `OandaClient` (AC6/AC7). What it protects: INV-01's read-only boundary for
  always-on/operator-facing surfaces — an LLM-driven advisory command is exactly the
  kind of surface that invariant's Phase 4 enforcement clause anticipates extending to.
  What it blocks (legitimate-looking but disallowed): an operator wanting `fathom
  review` to force a fresh broker read for a truly up-to-the-second view — that
  remains `fathom reconcile` (a separate, already-shipped, explicitly-broker-reading
  command) followed by `fathom review`, not a flag on `review` itself.

## Smoke checklist hooks

- Run `fathom reconcile` then `fathom review` against the demo store; confirm
  `account_state.as_of` printed by `review` matches the reconcile's timestamp (proves
  the staleness-surfacing behaviour, not a silent live read).
- Run `fathom review --deviations` against a store with ≥1 deviation-log row; confirm
  a `deviation:<event_id>` finding appears.
- Run `fathom review` with `LLM_API_KEY` unset; confirm exit 0 and the "analysis
  unavailable" fallback line.

## Open questions

- Should the calendar-proximity window (48h) and the RR-deviation threshold (30%) be
  `.env`-configurable, matching the pattern of `MAX_CANDIDATE_AGE_BARS`
  (`config/settings.py:77`)? Propose yes at the taskgraph stage — flagged here rather
  than decided, since it's a minor config-surface decision, not a behavioural one.

## Out of scope

- Counterfactual veto tracking (what a blocked trade would have done) —
  phase-09.
- Any action beyond printing text — `review` cannot flatten a position or modify a
  bracket; that remains `fathom execute`/the deviation monitor's own optional
  auto-response (`deviation-monitor.md`), never `review`.

## Notes

**Cross-phase assumption — reconcile at drift radar:** relies on `companion-core.md`'s
`run_companion_call`/`ContextPack` shape and on the post-`ai-package-migration` module
layout (`ai/` package) from phase-07, same as `companion-core.md` itself.

**Corrected scoping assumption (phase-08's "verify at spec time" item):** the phase
doc states the deviation log's stored fields "carry enough context ... for a per-entry
explanation without new capture." Verified largely **true** — `deviation_log`'s
`detail` column already carries the human-readable figure (per
`deviation-monitor.md`'s `DeviationEvent.detail` contract, "short human-readable
figure"), and `broker_trade_id` lets a per-entry explanation join back to the full
`positions` row for entry/stop/target context. The one gap: `deviation_log` does not
store a structured expected-vs-actual pair (e.g. `expected_stop_distance` vs.
`actual_excursion`) — that decomposition lives only inside the free-text `detail`
string, so the LLM explainer's prompt passes `detail` as opaque text rather than
structured fields. This is sufficient for `--deviations`' purpose (plain-English
narration of what already-human-readable text says) and does not block this spec, but
it means the explainer cannot do its own independent arithmetic check on the
deviation — it can only rephrase/contextualize what the watcher already computed.

**Separately, the phase doc's premise for this spec ("positions + latest reconcile
report + deviation log") needed correction, not just verification:** no
`ReconcileReport` is ever persisted (see Grounded claims) — `review` cannot read "the
latest reconcile report" because it does not exist as a store artifact once
`reconcile()` returns. The functional equivalent available to a read-only command is
`account_state` (which reconcile writes) plus `positions` (which reconcile's
adopt/close/refresh actions already updated) — both already broker-truth as of the
last reconcile pass, just without the `drift_flags` list itself (which only ever hit
the log, not the DB). This spec's design (step 2 above) uses that substitute and
surfaces `as_of` so the operator can judge freshness explicitly rather than the
command silently pretending the data is live.
