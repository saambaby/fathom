# Feature: pine-generation

**Status:** ready
**Phase:** phase-07
**Owner:** operator + Claude
**Last updated:** 2026-09-01

## Summary

`fathom pine` renders the latest persisted watchlist as a single **Pine Script v6
indicator** the operator pastes into TradingView, so candidates (entry/stop/target levels,
direction, strategy, flags) draw natively on the charts the operator actually uses. This
replaces PNG chart rendering as the presentation layer (ADR-003; PNG confirmed unused).
It is a pure, deterministic transform over stored `Candidate` rows — no LLM, no network,
no order authority — and it is built **first** in phase-07 because the paste-workflow's
usability is the phase's riskiest assumption.

## User-facing behaviour

- `fathom pine [--db-path PATH] [--out FILE] [--no-clipboard]` prints a complete Pine v6
  script to stdout, copies it to the clipboard (macOS `pbcopy`; silently degraded to
  stdout-only with a stderr warning when unavailable), and optionally writes `--out FILE`.
- The script is one indicator (`overlay=true`) containing every watchlist candidate. Each
  candidate's drawings render **only** on the TradingView chart whose symbol matches the
  candidate's instrument (`EUR_USD` → ticker `EURUSD`), so one paste serves all charts.
- Per candidate on its matching chart: three horizontal price lines — entry (`entry_ref`),
  stop (`entry_ref − stop_distance` for LONG / `+` for SHORT), target (`entry_ref +
  target_distance` for LONG / `−` for SHORT) — plus one label: rank, strategy_name,
  direction, timeframe, OOS Sharpe, and a `⚠ news` marker when `news_flag` is set.
- **Analysis join (verdict-aware rendering):** standalone `fathom pine` first reads the
  latest watchlist run key via a new scalar store accessor
  `latest_watchlist_run_ts() -> str | None` (`SELECT MAX(run_timestamp) FROM watchlist`;
  returns the stored RFC-3339 TEXT verbatim — it is the join key), loads that exact run
  (`load_watchlist(run_timestamp=parse(run_ts))` — the existing signature takes a UTC
  datetime, so the CLI parses the string back before calling; race-free vs a concurrent
  scan), then asks `load_latest_analysis(watchlist_run=run_ts)` (string form —
  `analysis_log.watchlist_ts` is TEXT)
  (analyze-command's accessor, pinned there to return rows **only** when an
  `analysis_log` run's `watchlist_ts` equals the given run key). When rows come back,
  pine drops candidates whose `suggest_action` is `skip` and adds a `reduce size` marker
  where it is `reduce_size`; when the accessor returns no rows — no analyze run yet, or
  the latest analysis belongs to an older watchlist — **all** watchlist candidates render
  with `news_flag` only (the no-join path; stale analysis is never joined onto a newer
  watchlist). (`fathom analyze` bypasses the join — it passes its in-memory survivor
  list to `render_pine` directly.)
- No-candidates output (INV-10 honest empty): when `load_watchlist` returns zero rows,
  emit a valid, compiling script that draws a single "Fathom: no candidates" table cell
  (timestamp-free — nothing is stored to stamp, and wall-clock would break determinism);
  stderr notice, exit 0. **Known store semantics:** an empty scan persists no rows and
  no run marker, so on a previously-used store `load_watchlist` returns the *previous
  non-empty run* — `fathom pine` then renders that superseded watchlist, and the
  per-candidate stale markers are the mitigation that makes its age visible. The
  zero-row branch therefore fires only on a never-populated table. A missing or
  unreadable database file is an error (non-zero exit), distinct from zero rows.
- Staleness is **per candidate**, exactly as the execute gate computes it: a candidate is
  stale when `generated_at` is older than `max_candidate_age_bars ×` its **own**
  timeframe's bar length (same setting + bar-length map as execute Step 1.5). Stale
  candidates get "(stale)" in their label; if **any** candidate is stale the script's
  status cell says "STALE" and the CLI prints a stderr warning. Generation always
  proceeds — presentation warns, only `fathom execute` refuses.

## Acceptance criteria

1. With a persisted watchlist of ≥3 candidates across ≥2 instruments (matching the
   phase-07 Done-when), `fathom pine` emits a script that compiles in the TradingView
   Pine v6 editor unmodified, and on each instrument's chart renders exactly that
   instrument's candidates (operator walk — the phase-07 riskiest-assumption gate).
2. Level arithmetic is direction-correct: for LONG, stop < entry < target; for SHORT,
   target < entry < stop — asserted in unit tests against hand-computed values from
   `Candidate` fixtures, and the emitted Pine contains those absolute prices formatted
   with the instrument's display precision.
3. Zero watchlist rows → compiling no-candidates script (timestamp-free) + stderr
   notice, exit 0; a missing/unreadable database exits non-zero with a distinct error.
4. Determinism: two runs over identical watchlist (+ analysis) rows produce
   byte-identical scripts (candidates ordered by `rank`; the only timestamps in the
   output are stored `generated_at` values — no run timestamp and no wall-clock).
5. Clipboard degradation: with `pbcopy` absent from PATH, the command still succeeds
   (stdout intact, stderr warning); `--no-clipboard` skips the attempt entirely.
6. Boundary: an AST test (pattern of `tests/test_admin_panel.py`'s forbidden-import
   probe) proves the pine module imports none of `execution.*`, `risk.*`, `cli`,
   `ai.*`/`hermes_integration.*`, `httpx`, `oandapyV20`, or `urllib.request` — the
   enumerated list is the network/order surface; clipboard's `subprocess` use is
   confined to the CLI handler, not the module.
7. Per-candidate staleness (AC semantics above): a fixture with one stale-H1 and one
   fresh-D candidate yields "(stale)" on the H1 label only, "STALE" in the status cell,
   a stderr warning, exit 0.
8. Analysis join: with an `analysis_log` run present for the watchlist, `skip`
   candidates are absent from the script and `reduce_size` survivors carry the marker;
   with no analysis rows the full watchlist renders (fixture-tested both ways).

## Component design

- **`signals/pine.py`** — `render_pine(items: list[PineItem]) -> str` where
  `PineItem { candidate: Candidate, stale: bool, reduce_size: bool }` (a thin frozen
  wrapper — `Candidate` itself is frozen per INV-13 and cannot carry flags). All flags
  are **caller-computed**: the standalone CLI derives `stale` from the TTL rule and
  `reduce_size` from the analysis join; `fathom analyze` passes its in-memory survivors
  with verdict flags. `render_pine` stays pure and clock-free (AC 4): given identical
  `PineItem` lists it is byte-identical, and per-candidate "(stale)" labels (AC 7) and
  markers (AC 8) come straight off the wrapper. Empty list → the no-candidates script.
  The status cell's timestamp is the max `generated_at` across items (deterministic,
  stored; the watchlist `run_timestamp` is used only as the join key in the CLI handler
  and never enters the rendered script).
  Templating by plain string building (no new dependency); one module-level
  `PINE_VERSION = 6`.
  - Symbol scoping: per candidate emit a guarded block —
    `if syminfo.ticker == "EURUSD"` … — comparing against `candidate.instrument` with the
    underscore stripped. `syminfo.ticker` is broker-prefix-free, so the same paste works
    on OANDA, FXCM, or IDC-fed charts (open question 1 covers exotic ticker variants).
  - Drawing primitives: `line.new` × 3 anchored from `bar_index` back N bars to the right
    edge, `label.new` × 1 per candidate, one `table` status cell (latest `generated_at`
    + STALE marker + candidate count). Object budget: ≤ 4 drawing objects per candidate, far
    under Pine's ~500-object ceilings at watchlist scale (portfolio limiter caps
    concurrent candidates — see Grounded claims).
- **CLI** — `fathom pine` subcommand in `cli.py` mirroring `fathom watchlist`'s read
  path: `run_ts = Store.latest_watchlist_run_ts()` (new scalar accessor, this feature) →
  `Store.load_watchlist(run_timestamp=run_ts)` → `Candidate` list →
  `load_latest_analysis(watchlist_run=run_ts)` join (analyze-command's accessor; no
  rows → no-join path) → `render_pine` → stdout / clipboard
  (`subprocess.run(["pbcopy"], input=...)`) / `--out`. Until analyze-command lands the
  accessor, the join call site is a guarded no-op (no-join path) — pine ships first.
- Freshness: reuse the same TTL constant/setting the execute gate reads (single source;
  see Grounded claims) to compute `stale`.

## Artefact verdicts

- Sequence diagram: **skip** — two actors (operator, CLI), one synchronous read-render
  path; nothing to order.
- Component design: **include** — the symbol-scoping and object-budget decisions are the
  parts a competent engineer would otherwise guess at.
- User flow: **skip** — CLI command; the acceptance walk (AC 1) covers the one manual
  TradingView step.

## Non-goals

- No TradingView write/API integration of any kind — paste is the transport (ADR-003).
- No alerts, strategies (`strategy()` scripts), or order visualization beyond levels —
  the script is an `indicator()`; TradingView never executes anything.
- No inbound data from TradingView (webhooks stay out of scope per Workstream 2).
- No per-candidate chart PNG replacement rendering — PNG path is deleted by
  hermes-teardown, not reimplemented here.
- No LLM involvement — narration/brief text is analyze-command's concern; the Pine label
  carries only stored `Candidate` fields.

## Touches

- INV-01 — pine module holds no order authority; AST-probed (AC 6).
- INV-03 — all timestamps rendered are the stored UTC RFC-3339 strings; no local time.
- INV-10 — empty watchlist is a first-class, honest output (AC 3).
- INV-13 — consumes the frozen `Candidate` contract read-only; no field added or
  reinterpreted.

## Events

- Written: none.
- Consumed: `watchlist` table (read directly, as `fathom watchlist` does);
  `analysis_log` rows (read-only, optional — the standalone analysis join; absent →
  no-join path).

## Environment variables

| Var | Purpose | Arg type | Where set |
|---|---|---|---|
| `MAX_CANDIDATE_AGE_BARS` | consumed (not new): the freshness TTL the staleness marker reuses — changes the STALE stamp in output | runtime | operator `.env`; same setting `fathom execute` enforces |

No new variables; `--db-path` CLI arg covers store location, as existing commands do.

## Wire-format contract

The `Candidate` → Pine mapping (all fields snake_case from the frozen INV-13 model;
transform site is `render_pine`, the only producer):

| Candidate field | Pine usage | Transform |
|---|---|---|
| `instrument` (`"EUR_USD"`) | `syminfo.ticker` guard | strip `_` → `"EURUSD"` (in Python, at render time) |
| `direction` (`"LONG"`/`"SHORT"`) | sign of stop/target offsets; label text; line colors | LONG: stop below / target above entry; SHORT mirrored |
| `entry_ref`, `stop_distance`, `target_distance` (float) | absolute line prices | computed in Python; formatted to instrument display precision (from instrument metadata; fallback 5 dp / 3 dp for JPY quotes) |
| `rank`, `strategy_name`, `timeframe`, `oos_sharpe_mean` | label text | f-string; Sharpe to 2 dp; prices formatted to the instrument's `display_precision` from stored instrument metadata (data/store.py:131,682) |
| `news_flag` (bool) | `⚠ news` marker in label | verbatim |
| `generated_at` (RFC-3339 str) | staleness input; labels | verbatim string in script; parsed only for TTL comparison |

Join input (standalone `fathom pine` only, optional): rows from
`load_latest_analysis(watchlist_run=run_ts)` — analyze-command's accessor, which yields
rows only when the latest `analysis_log` run's `watchlist_ts` equals `run_ts` (the
watchlist run key pine fetched via `latest_watchlist_run_ts()`). Rows are matched to
candidates on the `(instrument, timeframe, strategy_name)` identity triple —
`suggest_action = "skip"` drops the candidate, `"reduce_size"` adds the label marker.
Schema is pinned in analyze-command's wire-format contract; this feature is a read-only
consumer plus the owner of the `latest_watchlist_run_ts()` scalar accessor.

## Depends on

- signal-ranker — the frozen `Candidate` (INV-13) and the persisted watchlist table this
  feature reads.
- execution-cli (read-only reuse) — the freshness TTL definition, so "stale" means the
  same thing everywhere.
- analyze-command (forward, soft) — defines the `analysis_log` contract the standalone
  join consumes. pine-generation ships first: until analyze lands there are no analysis
  rows and the no-join path renders the full watchlist (fixture-tested, AC 8).

## Approach

1. `signals/pine.py::render_pine` with golden-file tests (byte-identical fixtures for
   LONG, SHORT, JPY-quote precision, empty, stale).
2. CLI subcommand + clipboard/out plumbing + degradation tests.
3. AST boundary test.
4. Operator acceptance walk on TradingView (AC 1) — the go/no-go on the phase's riskiest
   assumption, run before the remaining phase-07 specs are implemented.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| `Candidate` is frozen, flat snake_case with exactly the fields mapped above | signals/ranker.py:85-134 | read model: `model_config = {"frozen": True}`, field list matches INV-13 table |
| `entry_ref` is an absolute price; `stop_distance`/`target_distance` are distances (not prices) | signals/ranker.py:101-103 | docstring + INV-11 derivation; arithmetic in AC 2 follows |
| Watchlist persists to SQLite and is re-readable as dicts mapping 1:1 to `Candidate` | data/store.py:844-900 | read `write_watchlist`/`load_watchlist` ("each maps to one `Candidate`") |
| `fathom watchlist` already models the read-only CLI path to copy | cli.py:803-833 | read subparser wiring |
| A freshness TTL for watchlist candidates exists and is enforced at the execute gate — settings field (TTL in bars of the candidate's own timeframe) + per-timeframe bar lengths + gate check | config/settings.py:71 · cli.py:215 · cli.py:1625 | read all three: setting defines TTL, cli holds bar-length map, execute Step 1.5 refuses stale candidates; pine reuses the same setting + map |
| An AST forbidden-import probe pattern exists to copy | tests/test_admin_panel.py | named as the pattern by implementation-plan Task 2.1; file exists in tests/ |
| Portfolio limiter caps admitted candidates at `max_concurrent` (default 5) → ≤ ~20 drawing objects/script, far under Pine limits | signals/portfolio.py:68,129-130 | read `DEFAULT_MAX_CONCURRENT: int = 5` and the Field default |
| `load_watchlist` already accepts an exact `run_timestamp` and the latest-run scalar is a one-line query (`SELECT MAX(run_timestamp) FROM watchlist`) the store uses internally — `latest_watchlist_run_ts()` only exposes it | data/store.py:897-937 | read `load_watchlist` signature + the latest-run branch's MAX subquery |

## Smoke checklist hooks

- Run `fathom scan` (or seed fixture watchlist) → `fathom pine` → paste into TradingView
  Pine editor → confirm compile + correct levels on ≥2 instruments' charts (AC 1).
- Empty-watchlist day: `fathom pine` prints the no-candidates script and exits 0.

## Open questions

1. TradingView ticker variants: some feeds ticker metals/indices differently
   (`XAU_USD` → `XAUUSD` holds, but verify one non-FX instrument during the acceptance
   walk if the universe includes any).
2. Line anchoring length (how many bars back the level lines extend) — cosmetic; settle
   during the acceptance walk, default 50 bars.

## Out of scope

- Watchlist content, ranking, filtering — signal-ranker owns those.
- Verdict/brief text in labels — analyze-command.
- Deleting the PNG path — hermes-teardown.
- Any TradingView paid-plan features (alerts, webhooks).

## Notes

Riskiest-assumption ordering: this spec implements first and its AC 1 walk is the
phase's continue/abort gate for the presentation-layer thesis (docs/phases/phase-07/phase.md, Purpose).
