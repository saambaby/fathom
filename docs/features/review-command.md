# Feature: review-command

**Status:** ready
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
   (`instrument.split("_")`) and call **`Store.load_calendar_events`** (new
   read-only SELECT on `calendar_events`; **do not** construct
   `FairEconomyCalendar` — its `__init__` runs `CREATE TABLE IF NOT EXISTS` +
   `commit` and mutates the SQLite file, `data/calendar.py:273-275`). If the
   `calendar_events` table is missing (`sqlite3.OperationalError` whose message
   contains `no such table` / `calendar_events`), return `[]` — treat as empty,
   never crash, never `CREATE`. Re-raise other `OperationalError`s (locked DB).
   returned rows in `ai/review.py` to `impact in {medium, high}` (`Impact.low`
   includes holidays, `data/calendar.py:17-22`; `upcoming_events` itself has no
   impact predicate, `data/calendar.py:310-365`). Window: injected UTC `now`
   plus 48h. Non-`BASE_QUOTE` instrument ids (no single `_`): skip calendar
   lookup for that position (no currencies derived).
5. Build a `ContextPack` via `build_context_pack(kind="review", ...)` so
   `ContextPack.sources` is populated (e.g. `positions:N`, `account_state:1|0`,
   `deviation_log:N`, `calendar_events:N` after the medium/high filter).
   Call `run_companion_call(prompt, response_model=ReviewResponse, fallback=_offline_review(...))`.
6.    **Print path:** if there is nothing to show (AC1 empty case), print
   `"nothing to review"` and stop — no raw tables, no LLM. Otherwise print
   raw tables first: always the positions table (may be empty when
   deviation-only); `account_state` or `account_state: (none — never
   reconciled)`; the raw deviation **table** only when `--deviations`.
   Then print `ReviewFinding`s after a **display filter**: keep
   `subject="position:<broker_trade_id>"` only when that id is in the loaded
   open positions; keep `subject="deviation:<event_id>"` only when `--deviations`
   **and** that `event_id` is in the loaded log. Drop any other LLM subjects.
7. Offline / no key / parse failure (`run_companion_call` returns `fallback`
   unchanged — `companion-core.md` AC2/AC4, no live-vs-fallback discriminator):
   print the raw tables, then `_offline_review`'s **rule-check** findings
   (labeled in `note` with the prefix `[rules]` so they are not read as LLM
   commentary), then a line containing the canonical substring
   `"analysis unavailable"` (suffix `" — showing raw data only"` is allowed);
   exit 0. AC3's "never fabricates a flag" means never an LLM-invented flag;
   deterministic `[rules]` flags are in-scope.

## Acceptance criteria

1. `fathom review` with no open positions **and** an empty deviation log prints
   "nothing to review" and exits 0 without calling the LLM. Deviation-only
   (zero positions, ≥1 log row, `--deviations`): still calls the LLM (or
   fallback) and prints deviation findings only. Deviation-only without
   `--deviations`: "nothing to review" (log is loaded for cross-ref but not
   displayed; no position subjects exist).
2. With ≥1 open position and an injected stub client returning a valid
   `ReviewResponse` that includes one `subject="position:<id>"` per open
   `broker_trade_id`, those findings print. If the parsed list is missing any
   open-position subject,    `run_review` **discards** the whole parsed response and uses `_offline_review`
   (post-validate; `extra="forbid"` does not catch a short list). The same
   post-validate applies when `--deviations`: every loaded `event_id` must have
   `subject="deviation:<event_id>"` or the parse is discarded for
   `_offline_review`. When `--deviations` is set, `_offline_review` emits one
   `[rules]` row per loaded `event_id`; it does not emit `deviation:*` findings
   when the flag is off.
3. With `LLM_API_KEY` unset (and no client), print raw tables + `[rules]`
   findings + a line containing `"analysis unavailable"`; exit 0; zero network
   I/O (INV-20). Never crash. Never print an LLM flag.
4. Display filter: `--deviations` prints one finding per loaded log `event_id`
   (LLM or `[rules]` explainer note); without the flag, drop all
   `deviation:*` subjects even if the model emitted them. Raw log rows stay
   in the context pack either way.
5. After the in-process medium/high filter, the context pack's
   `calendar_events` contains only those impacts; a fixture with a low-impact
   holiday plus a high-impact event in the 48h window (frozen `now`) asserts
   the pack contains the high `event_name` and not the holiday.
6. AST test (same forbidden set as `companion-core.md` AC5, plus
   `execution.reconcile`): `ai/review.py` must not import `execution.orders`,
   `execution.models.build_bracket`, `execution.reconcile`, `risk.sizing`,
   `risk.limits`, `cli`, or `data.calendar.FairEconomyCalendar`, and must not
   construct `OandaClient`. `from execution.models import Position` is allowed
   (INV-14 frozen read model returned by `Store.load_open_positions`).
7. `positions` / `account_state` / `deviation_log` **row counts** are unchanged
   across a run (not SQLite mtime — a `FairEconomyCalendar` construct would
   bump mtime; this spec forbids that constructor). Calendar reads go through
   `Store.load_calendar_events`.

## Sequence diagram

Skip — see Artefact verdicts.

## Component design

New CLI subcommand `review` in `cli.py` (thin: arg parsing + store wiring), backed by
a new `ai/review.py`:

- `ReviewFinding` (pydantic): `subject: str` (`"position:<broker_trade_id>"` or
  `"deviation:<event_id>"`), `worth_investigating: bool`, `note: str`.
- `ReviewResponse` (pydantic, `extra="forbid"`): `findings: list[ReviewFinding]`,
  `summary: str`.
- `_offline_review(...)` — `companion-core.md` `fallback`: deterministic
  `ReviewFinding`s from the two rule checks below. Each `note` starts with
  `[rules]`. This is the same object printed on the offline path after the raw
  tables (User-facing §7); it is **not** LLM commentary.
- Two cheap deterministic rule checks run **before** the LLM call, folded into
  the context pack, and used by `_offline_review`:
  - **Unusual stop distance:** skip the position (no flag, no div/0) when
    `entry_price == stop_loss_price`. Else
    `rr_actual = abs(take_profit_price - entry_price) / abs(entry_price - stop_loss_price)`
    using live bracket **prices** on the `Position` row (not INV-11 ATR
    distances). Flag iff `abs(rr_actual - 1.5) / 1.5 > 0.30`.
    `candidate_ref` cannot join a unique watchlist row (Grounded claims).
  - **Calendar proximity:** after the medium/high filter, a position whose
    currencies have ≥1 remaining event in the 48h window is flagged
    `worth_investigating: true` on the `[rules]` path; the LLM may add
    direction/relevance in its own notes.
- `--deviations` is a **display-time** filter over already-loaded deviation
  rows (AC4). When the flag is on, `_offline_review` adds one finding per
  loaded `event_id` with `note` prefixed `[rules]` that restates `detail`
  (no independent arithmetic — see Notes).
- `Store.load_calendar_events(currencies, start, end) -> list[dict]` — new
  read-only method; SELECT only; `now`/`end` injected by `run_review` (so
  AC5 can freeze time). Does not create tables. Missing table → catch
  `sqlite3.OperationalError` with `no such table`/`calendar_events` in the
  message and return `[]`; re-raise other OperationalErrors. After a successful
  SELECT, `run_review` post-validates LLM findings (AC2): missing
  `position:<id>` or, when `--deviations`, missing `deviation:<event_id>` →
  discard parse, use `_offline_review`.

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
  left behind. Stale-tolerant: if `load_account_state()` is `None`, print
  `account_state: (none — never reconciled)` and still exit 0 (do not
  dereference `as_of`). When a row exists, print `as_of` so the operator
  judges freshness. Phase-08 in-scope item 1 is amended to this substitute
  (no persisted `ReconcileReport` / `drift_flags`).
- No write path to broker, risk, or execution modules (phase doc, Out of scope) —
  `review` never calls `execution.reconcile.reconcile`, `risk.sizing`, or
  `execution.orders`.
- No historical trend/pattern analysis across many reviews — that is `journal show|summarize`'s job (`journal.md`), not `review`'s.
- No live calendar fetch — `Store.load_calendar_events` is a local SELECT
  only (do not call `FairEconomyCalendar.upcoming_events`, which uses
  wall-clock `now` at `data/calendar.py:324`). Missing table → empty list.

## Touches

- INV-01 — read-only store/calendar; AST set in AC6 (INV-01 Phase-4 list plus
  `execution.reconcile` / no `OandaClient`).
- INV-02 — **does not apply** (advisory display, not an automated decision).
  Fail-soft is INV-20 + the 2026-09-01 INV-02 scope note, not skip/veto.
- INV-03 — displayed timestamps stay UTC RFC-3339; 48h window uses injected UTC `now`.
- INV-08 — never logs `LLM_API_KEY`.
- INV-11 — RR **reference constant only** (`:116`); live prices, not ATR stops.
- INV-16 — surfaces last-reconcile broker-truth; does not re-read the broker.
- INV-20 — LLM traffic only via `run_companion_call` → `OpenAICompatClient`;
  offline predicate is zero I/O + `"analysis unavailable"` (plus `[rules]`).

## Events

- Written: none.
- Consumed: `positions`, `account_state`, `deviation_log`, `calendar_events`
  (all via `Store` reads).

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| `LLM_API_KEY` | Offline predicate (unset → fallback, zero I/O) | runtime secret | `.env` (existing) |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | runtime | `.env` (existing) |
| `LLM_MODEL` | Model id when a call is made | runtime | `.env` (existing) |

## Wire-format contract

Request: `run_companion_call`'s prompt embeds the `ContextPack.data` for kind
`"review"` — `positions: list[dict]` (the `Position` fields, INV-14 shape),
`account_state: dict | None`, `deviation_log: list[dict]` (`deviation_log` column
shape, see Grounded claims), `calendar_events: list[dict]` (`currency`,
`event_name`, `time`, `impact`) **after** the medium/high filter. Response: JSON
parsed against `ReviewResponse`; malformed JSON, extra fields, bad types,
missing a `position:<id>` for an open position, **or** (when `--deviations`)
missing a `deviation:<event_id>` for a loaded log row all use `_offline_review`.
Datetimes in the pack (Position `opened_at`, calendar `time`, `account_state.as_of`)
are serialized as UTC RFC-3339 strings (`model_dump(mode="json")` / already-TEXT
store columns) — never naive ISO without `Z`.

## Depends on

- `companion-core.md` — `build_context_pack`, `run_companion_call`, `ContextPack.sources`.
- `order-model-and-brackets.md` / INV-14 — `Position` field table.
- `reconciliation.md` — `account_state` keys; no persisted `ReconcileReport`.
- `monitor-alerts.md` / `deviation-monitor.md` — `deviation_log` / `DeviationEvent.detail`.
- `economic-calendar.md` — `CalendarEvent` / `Impact`; consumers filter impact.
- `execution-cli.md` — `fathom reconcile` remains the broker-read command.

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
| `FairEconomyCalendar.upcoming_events` is local SELECT only (no HTTP) and has **no** impact filter | `data/calendar.py:310-365` | Read method; holidays stored as `Impact.low` at `:17-22` |
| `FairEconomyCalendar.__init__` mutates the DB (`CREATE TABLE` + `commit`) | `data/calendar.py:273-275` | Read constructor — why this spec uses `Store.load_calendar_events` instead |
| `Position.candidate_ref` is `instrument:timeframe:strategy_name` (no run timestamp) | `execution/models.py:241` (Position); Order field `execution/models.py:138` | Read field docs |
| INV-11 default `rr_ratio` is 1.5 (reference only) | `docs/product/invariants.md:116` | Read rule sentence |
| Phase-08 in-scope item 1 originally asked for a persisted reconcile report | `docs/phases/phase-08/phase.md` In scope item 1 (amended this sprint) | Cross-check: no `CREATE TABLE` for reconcile reports in `data/store.py:106-401` |

## Constraint blast radius

- New constraint: `review` must not call `reconcile()`, construct `OandaClient`,
  or instantiate `FairEconomyCalendar`. Protects: INV-01 read-only operator
  surface + AC7 row-count stability. Blocks: `fathom review --live` that would
  refresh the broker or calendar HTTP feed — that stays `fathom reconcile` /
  calendar refresh, then `review`.

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

**Phase-08 in-scope item 1** is amended in `phase.md` this sprint: `drift_flags`
stay out (in-memory only). Do not persist a reconcile-report table just to feed
`review`. INV-01's Phase-4 surface list is not extended in invariants.md this
slice — the AST set in AC6 / companion-core is the enforcement.
