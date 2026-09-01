# Feature: journal

**Status:** ready
**Phase:** phase-08
**Owner:** saambaby
**Last updated:** 2026-09-01

## Summary

Upsert-on-execute operator journal: every `fathom execute` that reaches a
`client_order_id` (INV-15 `build_bracket` at `cli.py:1908-1913`) upserts one
row keyed by that id, then `fathom journal show|summarize` reads it. This is
the operator's narrative log — not INV-22 measurement history. Re-running the
same candidate (same id) updates the row (dry-run → submitted) instead of
duplicating it (phase-08 Done-when). LLM summarization uses
`companion-core.md`; the execute hook never calls the LLM.

## User-facing behaviour

```
fathom journal show [--db-path PATH] [--limit N]
fathom journal summarize [--db-path PATH] [--limit N]
```

`show` and `summarize` share default `limit=50`.

1. `show` prints the newest `limit` (default 50) journal rows as a table
   (`created_at`, `client_order_id`, `candidate_ref`, `outcome`, `dry_run`,
   `pretrade_decision`, `operator_reason`). `pretrade_decision` is
   `PretradeVerdict.decision` parsed from `pretrade_json` (always `"proceed"`
   for rows that exist — hook is after a proceed). `operator_reason` is the
   stored column (`""` except declined). Do **not** invent `operator_decision`.
   Empty table → `"journal empty"`; exit 0; no LLM.
2. `summarize` with **zero** rows: same `"journal empty"`, no LLM (AC 5).
   With ≥1 row: `build_context_pack(kind="journal", entries=list[dict])`
   (`ContextPack.sources` is filled by the builder — do not pass `sources=`).
   Then `run_companion_call(..., response_model=JournalSummary,
   fallback=offline)` where `offline = _offline_journal_summary()`.
   **Discriminator:** `result is offline` → print `"analysis unavailable"` plus
   the `show` table (companion-core returns fallback unchanged; AC2). Else
   print `result.text` only.
3. **Execute hook (demo-only):** `cmd_execute` calls
   `record_journal_entry(...)` only when `settings.env != "live"` (INV-09
   Phase-8/9 operator-telemetry skip). Live execute writes **zero**
   journal rows. Hook runs **after** `build_bracket` so `client_order_id`
   exists; gate aborts before that line write **zero** rows (no natural key).
4. Outcomes written (one upsert per id):
   - `dry_run` — dry-run return after limits pass (`cli.py:1964-1977`)
   - `operator_declined` — confirm abort (`cli.py:1996-2001`); `reason` matches
     [[veto-ledger]] (`confirm_interrupted` | `operator_said_no`)
   - `submitted` — fill printed (`cli.py:2026-2045`)
   - `broker_rejected` — `OrderRejected` (`cli.py:2013-2017`)
   - `submit_failed` — other submit exception (`cli.py:2018-2022`)
5. Limits reject **after** `build_bracket` (`cli.py:1931`): upsert
   `outcome="limits_rejected"` with `client_order_id` (the id was minted; a
   later retry uses the same id).
6. Hook failure: WARNING, execute control flow unchanged (isolation like
   [[veto-ledger]] AC 2). Hung INSERT may delay.

## Acceptance criteria

1. Demo `fathom execute --dry-run <ref>` that passes limits writes exactly one
   journal row with `outcome="dry_run"`, the candidate snapshot JSON, pretrade
   `proceed`, `dry_run=1`. Live (`settings.env == "live"`) fixture that reaches
   `build_bracket` writes zero journal rows.
2. A second demo execute of the same candidate (same INV-15
   `client_order_id`) **upserts** — row count for that id stays 1; `outcome`
   becomes `submitted` (or `operator_declined`) as appropriate. `--yes` submit
   after dry-run is the fixture.
3. Confirm abort writes `outcome="operator_declined"` and does not submit
   (`cli.py:1999-2001`). `--yes` writes zero declined rows (matches
   [[veto-ledger]] AC 5).
4. `record_journal_entry` never raises into `cmd_execute`; a raising Store
   stub leaves exit code and submit/dry-run behaviour identical (WARNING
   allowed). Does not mutate `Order` / `Candidate` / `PretradeVerdict`
   identity.
5. `fathom journal show` with empty table prints `"journal empty"` and does
   not call the LLM. `summarize` with empty table same, no LLM.
6. `summarize` with ≥1 row, `LLM_API_KEY` unset, and no client: print a line
   containing `"analysis unavailable"` plus the raw table; exit 0; zero
   network I/O (INV-20). Empty table is AC 5, not this AC.
   AST: `ai/journal.py` uses the companion-core forbidden set (plus
   `execution.reconcile`); `from execution.models import Order, Fill` is
   allowed (INV-14 read/write models for serialization). Must not import
   `execution.orders` / `submit_order` / `build_bracket`. `Order`/`Fill` are
   serialized read-only (`model_dump_json`); the recorder does not mutate
   them (AC 4). `cmd_journal`
   only reads (`show`/`summarize`); it never calls `record_journal_entry`.
7. Round-trip: upsert then `load_journal_entries` returns the same
   `client_order_id`, `outcome`, `candidate_ref`, UTC `created_at` /
   `updated_at` (INV-03). `updated_at` changes on upsert; `created_at` is
   sticky from the first insert.

## Sequence diagram

Skip — see Artefact verdicts.

## Component design

**Table `operator_journal`** (name pinned — not `journal`, which is a SQL
keyword in some dialects). Lifecycle UPSERT on `client_order_id` — **not**
INV-22 (INV-22 lists `analysis_log` / `veto_ledger` / trial ledger;
explicitly not this table).

```python
def record_journal_entry(
    db_path: str | Path,
    *,
    order: Order,
    candidate: Candidate,
    verdict: PretradeVerdict,
    outcome: Literal[
        "dry_run", "operator_declined", "submitted",
        "broker_rejected", "submit_failed", "limits_rejected",
    ],
    operator_reason: str,  # "" except declined
    now: datetime,
    fill: Fill | None = None,
) -> None: ...
```

`fill` is required (`Fill` instance) when `outcome="submitted"`; `None` for
every other outcome (`fill_json` NULL). `now` is `cmd_execute`'s `run_dt`
(`cli.py:1585`). Newest `show`/`summarize` sort is `updated_at DESC`.
Same UTC calendar date on `execution_date` is required for AC 2's same-id
fixture (INV-15).

Opens its own `Store` inside try/finally (execute has closed the reconcile
store before confirm, `cli.py:1652-1673`). `Store.upsert_journal_entry` uses
`INSERT ... ON CONFLICT(client_order_id) DO UPDATE` setting `outcome`,
`operator_reason`, `updated_at`, `dry_run`, `fill_json`. First insert sets
`created_at`; conflict never changes `created_at`.

`JournalSummary` (pydantic, `extra="forbid"`): `{text: str}`.
`_offline_journal_summary()` returns `JournalSummary(text="analysis unavailable")`.

Prompt: `ai/prompts/journal_summarize.md` with placeholder `{{entries}}`
(JSON array of the pack's `entries` dicts).

`ContextPack.data` keys: `entries: list[dict]` — each dict is the show
projection plus `pretrade_json` / `candidate_snapshot` truncated to INV-13
identity fields (`instrument`, `timeframe`, `strategy_name`, `direction`).

CLI: extend `cli.py:708` subparsers with `journal` → `show`/`summarize`.

## User flow

Skip — see Artefact verdicts.

## Artefact verdicts

- Sequence diagram: skip — one upsert + one optional LLM call; no multi-actor
  saga. Prose + execute line numbers cover order vs veto-ledger hooks.
- Component design: include — UPSERT vs INV-22, outcome enum, hook placement
  after `build_bracket`.
- User flow: skip — CLI-only.

## Non-goals

- No journal row for stale-candidate / missing-watchlist / pretrade-block /
  sizing-zero / missing-meta aborts (no `client_order_id` yet).
- Journal never feeds sizing, ranking, or execute (phase-08 Out of scope).
- No panel view.
- No live-account rows this phase.

## Touches

- INV-01 — `ai/journal.py` has no order authority; `cli.py` already owns
  execute. Recording must not call `submit_order`.
- INV-02 — **does not apply** (`summarize` is advisory). Fail-soft is INV-20
  (`"analysis unavailable"`), never skip/veto.
- INV-03 — `created_at` / `updated_at` via Store `_to_rfc3339`.
- INV-08 — never logs `LLM_API_KEY`; snapshot is Candidate/Order public fields.
- INV-09 — demo-only skip at `cmd_execute` (Phase-8/9 clause includes
  `operator_journal`); `ai/journal.py` and `data/store.py` do not read
  `settings.env`.
- INV-13 / INV-14 / INV-15 — snapshot `Candidate`; store `client_order_id`
  from `Order`; upsert key is that id.
- INV-20 — summarize-only LLM path via `run_companion_call`.
- INV-22 — **does not apply** as a forbid-UPSERT rule; INV-22 names this table
  as a lifecycle exception (like `veto_ledger_outcomes`).

## Events

- Written: `operator_journal` upserts (demo execute after `build_bracket`).
- Consumed: `show`/`summarize` reads; summarize may call LLM.

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| `ENV` | skip journal writes when `live` | runtime | `.env` (existing) |
| `LLM_API_KEY` | summarize offline predicate | runtime secret | `.env` (existing) |
| `LLM_BASE_URL` | summarize endpoint | runtime | `.env` (existing) |
| `LLM_MODEL` | summarize model id | runtime | `.env` (existing) |

## Wire-format contract

`operator_journal` (SQLite):

| Column | Type | Notes |
|---|---|---|
| `client_order_id` | TEXT PK | INV-15 id |
| `candidate_ref` | TEXT | `instrument:timeframe:strategy_name` |
| `candidate_snapshot` | TEXT JSON | `Candidate.model_dump_json()` |
| `pretrade_json` | TEXT JSON | `PretradeVerdict` |
| `outcome` | TEXT | enum in Component design |
| `dry_run` | INTEGER | `1` only on the dry-run success upsert (`cli.py:1964`); else `0` |
| `operator_reason` | TEXT | `""` or `confirm_interrupted` \| `operator_said_no` |
| `fill_json` | TEXT NULL | `Fill` JSON when `submitted`; else NULL |
| `created_at` | TEXT RFC-3339 | first insert, sticky |
| `updated_at` | TEXT RFC-3339 | every upsert |

`JournalSummary` LLM JSON: `{"text": "..."}` snake_case.

## Inbound third-party wire contract

Skip — no provider webhook.

## Depends on

- `companion-core.md` — summarize call shape.
- `execution-cli.md` / `cli.py` — hook sites after `build_bracket`.
- `pretrade-check.md` — `PretradeVerdict`.
- `order-model-and-brackets.md` / INV-14 — `Order` / `Fill`.
- [[veto-ledger]] — shared confirm-abort `reason` literals and demo-only skip
  (hooks are independent; either may fail without blocking the other).

## Approach

1. DDL + `upsert_journal_entry` / `load_journal_entries(limit)`.
2. `ai/journal.py` recorder + models; unit-test isolation + upsert stickiness
   of `created_at`.
3. Wire **seven** `cmd_execute` sites (limits reject, dry-run, declined
   EOF/interrupt, declined non-yes, fill, OrderRejected, generic submit
   error) covering the six outcomes.
4. CLI `journal show|summarize`; stub-client: valid `JournalSummary` →
   printed `text` only (`result is not fallback`).

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| `build_bracket` runs before limits and before dry-run/confirm | `cli.py:1908-1913` then `:1920`, `:1964`, `:1984` | Read cmd_execute step 4–6 |
| Dry-run returns 0 after printing order, no v20 | `cli.py:1964-1977` | Read block |
| Confirm abort is demo-only, `--yes` skips it | `cli.py:1984-2001` | Read block |
| `OrderRejected` vs generic submit except | `cli.py:2013-2022` | Read except arms |
| Fill printed then return 0 | `cli.py:2026-2045` | Read end of cmd_execute |
| INV-15 id is minted on `Order` by `build_bracket` before limits | `cli.py:1908-1913`; `docs/features/order-model-and-brackets.md` (client_order_id) | Read call site |
| CLI subparsers at `cli.py:708` | `cli.py:708` | grep |

## Constraint blast radius

**UPSERT on `client_order_id` (not append-only).**
- Protects: phase Done-when “repeated execute does not duplicate.”
- Blocks: keeping both a dry-run row and a submitted row for the same id —
  the submitted upsert is the surviving truth. Historical dry-run text is
  not preserved (operator who needs an audit trail uses `veto_ledger` +
  `analysis_log`, not this table).

**No journal before `build_bracket`.**
- Protects: idempotency key exists.
- Blocks: journaling a pretrade `block` here — that remains [[veto-ledger]]
  `source="pretrade"` only.

## Smoke checklist hooks

- Demo `--dry-run` then `--yes` on the same ref: one journal row, outcome
  `submitted`.
- `fathom journal show` prints it; `summarize` with key unset still exits 0
  with `"analysis unavailable"`.

## Open questions

- None load-bearing. Table name `operator_journal` vs SQLite `journal` is
  decided above.

## Out of scope

- Phase-09 counterfactuals.
- Feeding journal into ranker/sizing.
- Live journal rows until INV-07.

## Notes

Execute may also write [[veto-ledger]] on the same confirm-abort / pretrade
lines. Order of hooks is not load-bearing except: neither may change the
verdict; both isolate failures. `record_journal_entry` for declined should
run on the same abort branches as `record_declined`.
