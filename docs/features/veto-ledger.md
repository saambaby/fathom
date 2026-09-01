# Feature: veto-ledger

**Status:** ready
**Phase:** phase-09
**Owner:** saambaby
**Last updated:** 2026-09-01

## Summary

An append-only record of every LLM verdict that can influence a trade — both the
news-risk veto and the pre-trade veto — plus the candidate snapshot it was judged
against. Today every veto is unfalsifiable: nobody can later ask "did that block save
money?" This feature is the recording layer only — it writes rows, it never reads them
back into a decision. Aggregation and the counterfactual outcome live in
[[counterfactual-tracker]] and [[veto-report]]. The hard constraint (from the phase
"Done when") is failure isolation: a ledger write must never alter or raise into
either gate — a broken ledger degrades to "no measurement," never to "no trade" or "bad
trade." A hung/slow `INSERT` may delay the gate this phase (best-effort synchronous
write, no timeout); that is accepted for demo measurement and must still not raise.

## User-facing behaviour

No new CLI surface. Three call sites gain a recording hook (declined is in-scope,
not optional — it is cheap at the existing confirm abort):

All three hooks are **demo-only**, gated on `settings.env != "live"` at the hook call
site itself (INV-09 Phase-9 measurement-write clause) — see Component design.

1. **Pre-trade hook** — `cli.py`'s `cmd_execute`, immediately after
   `verdict = pretrade_check(candidate, client=...)` (today `cli.py:1773`, unchanged line
   number/shape post-phase-07 rename). The hook is called with the already-computed
   `verdict` and does not change what happens next in the function. Because
   `cmd_execute`'s live path (`cli.py:1698-1767`, the `if settings.env == "live":` block)
   runs *before* line 1773 and does not early-return on every live run (a passing live
   gate falls through to the same pretrade-check line), the hook call itself carries an
   explicit `if settings.env != "live":` guard rather than relying on an upstream
   short-circuit — a live pretrade verdict is real-money-adjacent and out of scope per
   the phase doc's "Live-account anything" exclusion.
2. **News-risk hook** — owned by [[analyze-command]] `run_analysis`, **immediately
   after** `news_risk_check(...)` returns a `NewsRiskVerdict` and **before** `narrate`
   (not colocated with the later `analysis_log` append, which sits after narration).
   Same `settings.env != "live"` guard. Signature wrapped:
   `news_risk_check(candidate, calendar_events, entry_window_utc, *, client=None)`
   from [[ai-package-migration]]. This spec defines the recorder; analyze-command
   owns the call site and the `analyze_run_ts` join key.
3. **Operator-declined hook (required)** — `cli.py`'s `cmd_execute` confirm prompt, at
   the point the operator answers anything other than `y`/`yes`, **or** the prompt is
   interrupted (`EOFError`/`KeyboardInterrupt`) — both are treated as a decline (today
   `cli.py:1995-2001`, inside `if settings.env != "live" and not skip_confirm:`). A
   `declined` row records that the gate passed (pretrade `proceed`, sizing, limits all
   allowed) but the human said no. `reason` is pinned: `"confirm_interrupted"` on
   `EOFError`/`KeyboardInterrupt`; `"operator_said_no"` on any other non-`y`/`yes`
   answer. The hook opens its own `Store(db_path)` (see Component design) — after
   reconcile, `cmd_execute` has already `store.close()`'d (`cli.py:1652-1673`) and
   `store_submit` is not opened until `cli.py:2003`. Live's typed-confirm path
   (`cli.py:1742-1754`) is out of scope — no ledger row there.

Operator-visible effect: none directly. The `veto_ledger` table fills up silently; the
operator sees it only via `fathom veto-report` ([[veto-report]]).

## Acceptance criteria

1. Every `pretrade_check` call inside `fathom execute` on `settings.env != "live"` (both
   `--dry-run` and demo submit) writes exactly one `veto_ledger` row with
   `source="pretrade"`, the full `Candidate` snapshot, the `PretradeVerdict` JSON,
   `prompt_version`, `model_id`, and a UTC `created_at` — before `cmd_execute` proceeds to
   the next gate step. On `settings.env == "live"`, a test that **reaches**
   `pretrade_check` (live gate mocked/passed so control falls through `cli.py:1767` to
   `cli.py:1773`; broker mocked) asserts zero `veto_ledger` rows. A live-gate refuse
   that returns before line 1773 is a different fixture and does not satisfy this AC.
2. A ledger write failure (store exception, serialization error, disk full) is caught at
   the hook boundary, logged at WARNING, and `cmd_execute`'s return value / control flow
   is identical to a run with the ledger disabled — proven by a test that runs
   **execute** twice (ledger store swapped for a raising stub vs. a real store) and
   diffs the verdict, exit code, and every downstream call made (stdout/stderr may
   include the WARNING log line). `record_news_risk_verdict` isolation is the same unit test
   (raising stub Store) plus [[analyze-command]] AC 1 (loop continues). Isolation does
   not claim equal latency.
3. `record_pretrade_verdict` / `record_news_risk_verdict` / `record_declined` never
   raise out of the hook under any store or serialization failure, and never **mutate
   or replace** the `verdict` / `DeclinedRecord` / `Candidate` objects passed in
   (identity + equality after the call). Recorders **may read** those objects to
   serialize (`model_dump_json` / `model_dump`) inside the `try`. The write is
   synchronous with **no timeout**; a hung `INSERT` may delay the caller. Isolation
   is exception-and-control-flow only, not latency.
4. The `veto_ledger` table has no rewrite path in `store.py` — no `UPDATE`, `DELETE`,
   or `INSERT OR REPLACE` / upsert bound to this table, only a plain `INSERT` —
   and a test introspects the SQL actually executed against `veto_ledger` (not a
   module-wide grep that would miss `INSERT OR REPLACE`, the pattern `write_order`
   uses at `data/store.py:974-1000`).
5. The `declined` row: when written, it carries `source="operator_declined"`,
   the same `Candidate` snapshot, and a verdict payload conforming to the
   `DeclinedRecord` model (`{"decision": "declined", "reason": "..."}` with
   `decision: Literal["declined"]` — **not** `PretradeVerdict`, whose `decision` field is
   strictly `Literal["proceed", "block"]` per `pretrade-check.md:22` and would reject
   `"declined"` at validation). Presence/absence is covered by a test for the
   `y` / non-`y` / `EOFError` / `KeyboardInterrupt` branches of the confirm prompt
   (`reason` values pinned in User-facing behaviour). `--dry-run` and `--yes`
   (`skip_confirm`) write zero `operator_declined` rows.
6. A round-trip test per `source` via `Store.load_veto_ledger_rows(*, source=None) ->
   list[VetoLedgerRow]`: same `source`, `prompt_version`, `model_id`, `analyze_run_ts`,
   `created_at`; `candidate_snapshot` deserializes to `Candidate`; `verdict_json`
   deserializes to the model matching that `source`. A `source="news_risk"` fixture
   asserts `analyze_run_ts` equals the minted run-start `run_ts` and `created_at` is
   the per-candidate hook clock (not equal to `analyze_run_ts` unless they happen to
   share a second). Equality of JSON blobs is pydantic-model equality after validate,
   not byte-for-byte (float encoding).

## Component design

**New module:** `eval/veto_ledger.py` (path pinned — INV-09 names this file; not
colocated with `ai/`). Three recording functions, mirroring
the fail-safe style already used by the two INV-02 parsers (`_safe_default()` +
try/except-everything, `news_risk.py:91-155`, `pretrade_check.py:129-193`) but inverted:
here the *default on failure* is "write nothing, log WARNING," never "raise" and never
"change the verdict."

```python
def record_pretrade_verdict(
    db_path: str | Path, candidate: Candidate, verdict: PretradeVerdict,
    *, model_id: str, prompt_version: str, now: datetime,
) -> None: ...

def record_news_risk_verdict(
    db_path: str | Path, candidate: Candidate, verdict: NewsRiskVerdict,
    *, model_id: str, prompt_version: str, now: datetime,
    analyze_run_ts: str,
) -> None: ...

def record_declined(
    db_path: str | Path, candidate: Candidate, *, now: datetime,
    reason: Literal["confirm_interrupted", "operator_said_no"],
) -> None: ...
```

Each recorder **opens and closes its own** `Store(db_path)` inside the `try` (same
pattern as `cmd_execute`'s post-pretrade metadata load at `cli.py:1793-1798`). Callers
do not thread a live `Store` — after reconcile, execute has already closed its store
(`cli.py:1652-1673`); the confirm abort returns before `store_submit` (`cli.py:2003`).
`record_declined` writes `prompt_version=""`, `model_id=""`, `analyze_run_ts=NULL`
inside the recorder (callers do not pass those sentinels). `Store.__init__` already
accepts `str | Path` (`data/store.py:435-438`); recorders match that.

`DeclinedRecord` (new pydantic model, `eval/veto_ledger.py`, `model_config =
{"extra": "forbid"}` — mirrors the two INV-02 verdict models' strictness):
`{decision: Literal["declined"], reason: Literal["confirm_interrupted", "operator_said_no"]}`. It is deliberately its own model, not a
reuse of `PretradeVerdict`, because `PretradeVerdict.decision` is a strict two-value
enum (`"proceed"|"block"`, `pretrade-check.md:22`) that would raise on `"declined"` —
consistent with the two existing INV-02 models' strict-enum posture, this ledger does not
loosen either of them to fit a third case.

**Split of responsibilities (pinned):** the `if settings.env != "live":` guard lives
**only** in `cli.py` and `signals/analyze.py`. Open / serialize / `INSERT` / close live
**only** inside `eval/veto_ledger.py` `record_*`. `eval/veto_ledger.py` and
`data/store.py` never read `settings.env`. Callers pass `model_id` already resolved
(see below). The demo-only skip matches how `cli.py` already gates the `[y/N]` confirm
(`cli.py:1984`).

**Execute- and analyze-side `model_id` (one predicate, same as the check's no-call
path):** `"offline"` iff `client is None and not os.environ.get("LLM_API_KEY")`
(`pretrade_check.py:386-391`). News-risk's equivalent skip is [[ai-package-migration]]
AC 3 (that package does not exist yet; do not read it from today's parser-only
`news_risk.py`). `settings.llm_api_key` is **not** a third conjunct — `_llm_client_from_settings`
already turns a settings key into an injected client (`cli.py:154-160`). Otherwise
`settings.llm_model`. Injected stub-client tests pass `model_id=settings.llm_model`.
[[analyze-command]] uses this same predicate.

Each `record_*` wraps work as:

```python
try:
    store = Store(db_path)
    try:
        store.write_veto_ledger_row(...)
    finally:
        store.close()
except Exception:
    log.warning(...)
```

(no timeout on the `INSERT`; hang may delay). `Store.write_veto_ledger_row` (new method,
`data/store.py`, alongside `write_order`/`write_rejection` at `data/store.py:974-1054`)
is a single `INSERT` (idempotency is not required here — unlike `write_order`'s
`INSERT OR REPLACE` on `client_order_id`, INV-15 — because a veto verdict has no natural
dedup key; a re-run of `fathom execute` on the same stale-refused candidate never reaches
the pretrade-check line, and a re-run that does reach it produces a legitimately new
verdict worth its own row). `Store.write_veto_ledger_row(*, source, candidate_snapshot, verdict_json,
prompt_version, model_id, created_at: datetime, analyze_run_ts=None) -> int` returns
the new `id`. `created_at` is timezone-aware UTC; Store calls `_to_rfc3339` (same as
`write_order` at `data/store.py:974-1000`). Recorders pass `now` through; they do not
pre-format TEXT. `VetoLedgerRow` is a pydantic read model with those columns plus `id`
(`created_at` as RFC-3339 TEXT on read). `Store.load_veto_ledger_rows(*, source: str |
None = None) -> list[VetoLedgerRow]` returns rows in `id` order.

The execute pretrade call site (`cli.py:1773`) calls the recording function **after**
the verdict is computed and **before** `if verdict.decision == "block":` at
`cli.py:1774` — so the same hook line runs whether the verdict is `proceed` or `block`,
and cannot itself introduce a new decision branch. Analyze's news-risk hook is the
line after `news_risk_check` returns, before `narrate` ([[analyze-command]] sequence).
`run_analysis` mints **one** UTC RFC-3339 `run_ts` at the start of the run and passes
that same string into every `record_news_risk_verdict(..., analyze_run_ts=run_ts)` and
every `analysis_log` row (ledger write precedes the `analysis_log` append).
The declined hook is **not** the unconditional-before-branch constraint: it runs only
on the demo confirm-abort branches (`cli.py:1996-2001`). `--dry-run` returns before
confirm (`cli.py:1964-1977`) and `--yes` / `skip_confirm` skips the prompt
(`cli.py:1984`) — those paths write **zero** `operator_declined` rows.

## Artefact verdicts

- Sequence diagram: skip — a single-hop "call site → store.write, catch-all on
  failure" flow with no async coordination or multi-service ordering; prose covers it.
- Component design: include — the module split (`eval/veto_ledger.py` vs. `data/store.py`
  vs. the call-site guards) and the `DeclinedRecord` model are load-bearing for
  [[counterfactual-tracker]] and [[veto-report]] to build against.
- User flow: skip — no frontend/CLI surface; this is a backend recording hook.

## Non-goals

- No aggregation, no outcome resolution — [[counterfactual-tracker]] and
  [[veto-report]].
- No change to `parse_news_risk` / `parse_pretrade_verdict` — INV-02 parse boundaries are
  untouched; the hook wraps their *callers*, not the boundary functions themselves.
- No live-account rows — the phase doc's "Live-account anything" exclusion; **all three**
  hooks (pretrade, news-risk, operator-declined) are gated on `settings.env != "live"` at
  the call site (INV-09 Phase-9 measurement-write clause), not just the declined one.

## Touches

- [INV-01] — `eval/veto_ledger.py` records verdicts only: it must not import or call
  sizing, order construction, or placement (`risk.sizing`, `execution.orders`,
  `build_bracket` / `submit_order`). Order-free orchestration (`signals/analyze.py`,
  the analyze CLI handler) **may** import `record_news_risk_verdict`; that import is
  not an execution path. INV-01's rule is order authority, not "AI surfaces cannot
  import eval."
- [INV-02] — records the verdict; never alters the parse boundary's safe-default
  behavior or return value.
- [INV-03] — `created_at` is UTC RFC 3339, written the same way as every other store
  timestamp (`_to_rfc3339`, `data/store.py:57`).
- [INV-08] — the `Candidate` snapshot and verdict JSON carry no secrets by construction
  (both are already-public pipeline data); `model_id` is a model name string, never the
  `LLM_API_KEY`.
- [INV-09] — demo-only measurement-write skip at `cli.py` / `signals/analyze.py` call
  sites; `eval/` and `data/store.py` never read `settings.env` (Phase-9 clause).
- [INV-13] — `candidate_snapshot` is a serialization of the frozen `Candidate` (INV-13,
  `signals/ranker.py:85` class, `model_config` frozen at `:116`); the ledger never
  mutates a `Candidate` instance.

## Events

- Written: one `veto_ledger` row per pretrade-check call, per news-risk verdict,
  and per operator decline, each **only when** `settings.env != "live"`.
- Consumed: none by this feature. [[counterfactual-tracker]] reads `veto_ledger`.

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| `ENV` | call-site skip of measurement writes when `live` (INV-09 Phase-9) | runtime | operator `.env` (existing) |
| `LLM_MODEL` | `model_id` column when an LLM client was used | runtime | operator `.env` (existing) |
| `LLM_API_KEY` | participates in the no-call/`"offline"` `model_id` predicate | runtime secret | operator `.env` (existing; no new vars) |

## Wire-format contract

`veto_ledger` table (SQLite, mirrors the existing store's TEXT-timestamp / JSON-blob
conventions — see `write_order`/`write_rejection`, `data/store.py:974-1054`):

| Column | Type | Written by | Notes |
|---|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | SQLite | row identity; also the FK `counterfactual-tracker` outcome rows key on |
| `source` | TEXT | hook | `"news_risk"` \| `"pretrade"` \| `"operator_declined"` |
| `candidate_snapshot` | TEXT (JSON) | hook | `Candidate.model_dump_json()` — full frozen INV-13 shape |
| `verdict_json` | TEXT (JSON) | hook | `source="pretrade"` → `PretradeVerdict` JSON; `source="news_risk"` → `NewsRiskVerdict` JSON; `source="operator_declined"` → `DeclinedRecord` JSON (`{"decision":"declined","reason":"..."}`) |
| `prompt_version` | TEXT | hook | `PRETRADE_PROMPT_VERSION` / `NEWS_RISK_PROMPT_VERSION` module constants (`"v1"` this phase); empty string `""` for `operator_declined` |
| `model_id` | TEXT | hook | for LLM-backed rows: `settings.llm_model` (`config/settings.py:99`) when a call was made, else `"offline"` (same rule as [[analyze-command]] `analysis_log.model_id`); empty string `""` for `operator_declined` |
| `analyze_run_ts` | TEXT NULL | news-risk hook | `analysis_log.run_ts` of the enclosing `fathom analyze` run; NULL for `pretrade` and `operator_declined` (those have no analyze run). Join key with `analysis_log` + identity triple inside `candidate_snapshot` |
| `created_at` | TEXT (UTC RFC 3339) | Store `_to_rfc3339(now)` | Execute: `cmd_execute`'s `run_dt` (`cli.py:1585`). News-risk: per-candidate UTC-aware hook clock (not the run-start `analyze_run_ts`). Recorders never call `datetime.now()`. |

No `outcome`/`r_multiple`/`resolved_at` columns live here — those are
[[counterfactual-tracker]]'s separate `veto_ledger_outcomes` table, keyed by this table's
`id`, kept structurally separate so this table's "append-only, no update path" guarantee
(acceptance criterion 4) is never at odds with the tracker's idempotent-refresh
requirement (which *does* need upserts, just not on this table).

## Depends on

- `signals/ranker.py::Candidate` (INV-13, shipped).
- `hermes_integration/news_risk.py::NewsRiskVerdict`/`parse_news_risk` (shipped);
  `hermes_integration/pretrade_check.py::PretradeVerdict`/`parse_pretrade_verdict`/
  `pretrade_check` (shipped) — post-phase-07 these are `ai/news_risk.py` /
  `ai/pretrade_check.py` (path only; contract unchanged per phase-07 scoping).
- `data/store.py::Store` (shipped) — this spec adds one table + `write_veto_ledger_row` +
  `load_veto_ledger_rows`.
- [[ai-package-migration]] — `news_risk_check(candidate, calendar_events,
  entry_window_utc, *, client=None)` is the function the news-risk hook wraps.
- [[analyze-command]] (phase-07) — **owns** the `record_news_risk_verdict` call site
  (immediately after `news_risk_check`, before `narrate`) and supplies `analyze_run_ts`
  = that run's `analysis_log.run_ts`. Vocabulary: UTC RFC-3339 TEXT timestamps;
  `model_id` is `LLM_MODEL` or `"offline"` (shared no-client predicate).
- [[execution-cli]] (shipped) — this spec adds two side-effect lines to `cmd_execute`
  (pretrade record before the `block` branch; declined record on confirm abort). Gate
  order is otherwise unchanged.
- **Cross-phase note:** near-term news-risk module is still
  `hermes_integration/news_risk.py` until hermes-teardown + the `ai/` rename; this
  spec's path references say "today `hermes_integration/`" for that reason.

## Approach

1. Add the `veto_ledger` table + `Store.write_veto_ledger_row` (same execute+commit+
   `_to_rfc3339` skeleton as `write_order`; **plain `INSERT` only** — not
   `INSERT OR REPLACE`).
2. Add `eval/veto_ledger.py` with the three record functions, each fully wrapped for
   failure isolation, unit-tested with a raising stub `Store`. Add `eval*` to
   `[tool.setuptools.packages.find] include` in `pyproject.toml` (today's list is
   `hermes_integration*` and siblings; without this the package is not installed).
3. Wire the pretrade hook into `cli.py:1773`, guarded by `if settings.env != "live":`
   (after the existing assignment; recorder opens its own Store).
4. Wire the declined hook into `cli.py:1995-2001`, in both the non-`y` branch (line
   1999-2001) and the `EOFError`/`KeyboardInterrupt` branch (line 1996-1998) — the
   surrounding `if settings.env != "live" and not skip_confirm:` block already provides
   the env guard. Recorders take `db_path` + pinned `reason`; both execute call sites
   pass `now=run_dt` (`cli.py:1585`).
5. Define `prompt_version` as a module-level constant per prompt file (`PRETRADE_PROMPT_VERSION = "v1"` in `pretrade_check.py`, `NEWS_RISK_PROMPT_VERSION = "v1"` in `news_risk.py`) — no versioning convention exists today; this spec introduces the minimal one needed for the ledger to be meaningful across future prompt edits.
6. News-risk hook **call site**: [[analyze-command]] ships in phase-07 *without* the
   hook (`eval/` does not exist yet) but pins the insertion point (after
   `news_risk_check` returns, before `narrate`). A phase-09 task from *this* spec
   retrofits the `record_news_risk_verdict` call into `run_analysis` per that pinned
   contract — so this spec's "done" is the ledger module + table + execute call sites
   + the analyze retrofit; analyze-command's AC 1 marks the ledger clauses as
   phase-09-accepted.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| `parse_pretrade_verdict` is the single INV-02 parse boundary for the pretrade veto | `hermes_integration/pretrade_check.py:129` | opened file, read full function body (lines 129-193) |
| `pretrade_check()` is the public entrypoint `cli.py` calls | `hermes_integration/pretrade_check.py:355` | opened file, read full function |
| `cmd_execute` calls `pretrade_check` and branches on `verdict.decision` | `cli.py:1773-1784` | opened file, read the step-3 block |
| The demo confirm prompt is a single `input()` call gated on `env != "live" and not skip_confirm` | `cli.py:1984-2001` | opened file, read step-6 block |
| The live typed-confirm path is separate and NOT bypassable by `--yes` | `cli.py:1742-1754` | opened file, read the live-gate block |
| `parse_news_risk` is the single INV-02 parse boundary for news-risk | `hermes_integration/news_risk.py:91` | opened file, read full function body (lines 91-155) |
| `Candidate` is defined in `signals/ranker.py`, frozen per INV-13 | `signals/ranker.py:85` class, `model_config` at `:116` | grepped `class Candidate`, confirmed `frozen` on model_config |
| `write_order`/`write_rejection` show the store's existing write-method shape (positional bind, `_to_rfc3339`, single `execute` + `commit`); `write_fill` sits in the same span | `data/store.py:974-1054` | opened file, read the three write methods |
| `settings.llm_model` is the model-id source, default `"gpt-5-nano"` | `config/settings.py:99` | grepped `llm_model` in settings.py |
| No prompt-version convention exists yet in `hermes_integration/prompts/` | `hermes_integration/prompts/{narration,news_risk,pretrade}.md` | `ls`'d the directory; files are plain templates, no version header found |
| `settings.env` is the single field both the live gate and the demo confirm already branch on; `cmd_execute`'s live block does not early-return on every live run (a passing gate falls through) | `cli.py:1698,1767,1773,1984` | opened file, traced control flow from the live-gate `if` through to the pretrade-check call and the confirm block |
| `PretradeVerdict.decision` is a strict `Literal["proceed","block"]` enum (would reject `"declined"`) | `hermes_integration/pretrade_check.py:105` | opened file, read the model field declaration |
| After reconcile, execute's `store` is closed; confirm abort returns before `store_submit` | `cli.py:1652-1673,2003` | opened file, traced Store open/close |
| Post-pretrade instrument metadata already opens a fresh `Store(db_path)` | `cli.py:1793-1798` | opened file, read store2 try/finally |
| `_llm_client_from_settings` returns `None` when `llm_api_key` is unset | `cli.py:144-160` | opened function |
| `pretrade_check` no-call path is `client is None and not LLM_API_KEY` | `hermes_integration/pretrade_check.py:386-391` | opened function |

## Constraint blast radius

**New constraint: `veto_ledger` has no UPDATE/DELETE path (append-only).**
- **Protects:** the audit trail's integrity — a verdict row can never be silently
  corrected or backdated after the fact, which is what makes the later hit-rate report
  trustworthy.
- **Legitimate mutations blocked:** none — this table has no legitimate reason to be
  edited after write; a wrong verdict is corrected by writing a *new* row (a future
  verdict with a later `created_at`), not by editing history. (Contrast:
  `write_order`'s `INSERT OR REPLACE` on `client_order_id` is a different table with a
  real dedup need — INV-15 — that does not apply here.)

**New constraint: the pretrade and news-risk hook call sites run unconditionally vs
proceed/block (and the declined hook vs confirm abort), after the INV-09 live skip.**
- **Protects:** a ledger bug cannot correlate with which verdict branch is taken.
- **Legitimate mutations blocked:** none for those two hooks once `env != "live"`.
  Live skip is the INV-09 Phase-9 exception. The **declined** hook only runs on
  confirm-abort branches (`y`/`yes` writes no declined row).

## Smoke checklist hooks

- Run `fathom execute --dry-run <candidate>` twice (once with a working DB, once with the
  DB file made read-only) and confirm both runs produce identical exit code and
  gate behaviour (WARNING may appear on the failing write) — demonstrating the ledger
  write failure did not change the verdict.
- Inspect the `veto_ledger` table after a `fathom execute --dry-run` run and confirm
  exactly one `source="pretrade"` row was written.

## Open questions

- None load-bearing. Module home is `eval/` (INV-09 names `eval/veto_ledger.py`);
  moving it into `ai/` would require amending INV-09. `prompt_version` stays a
  hand-maintained `"v1"` constant this phase (content-hash is a later upgrade).

## Out of scope

- Outcome resolution, R-multiple computation, aggregation — [[counterfactual-tracker]],
  [[veto-report]].
- Implementing the news-risk call site inside this taskgraph slice — [[analyze-command]]
  owns that loop; this spec ships the recorder + execute hooks.
