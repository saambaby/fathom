# Feature: ask-command

**Status:** ready
**Phase:** phase-08
**Owner:** saambaby
**Last updated:** 2026-09-01

## Summary

`fathom ask "<question>"` is freeform Q&A **grounded only in store tables this
command is allowed to load**. It refuses — visibly, without a fabricated
live quote or unstored headline — when the model returns `refused` for
`live_quote` / `unstored_news` / `foreign_account` / `not_in_store`. Questions
about **this** account's last-reconciled equity/positions are **in-pack**:
answer from `account_state` / `positions` and cite `as_of` (INV-16); they are
not a refusal. Stale watchlist rows are still answerable but stamped
`stale` (INV-21 warn, not refuse).

## User-facing behaviour

```
fathom ask "QUESTION" [--db-path PATH]
```

1. Parse the question string (required positional). Empty / whitespace →
   stderr `"ask: empty question"`; exit 2; no LLM; no store reads beyond
   opening the DB (optional).
2. Load a **fixed source set** (no NLP router):
   latest watchlist (`Store.load_watchlist()`, `data/store.py:896-913`),
   **latest-run** `approved_set` only: `MAX(run_timestamp)` TEXT from the table,
   parse with `datetime.fromisoformat` (`Z` → `+00:00`), then
   `load_approved_set(run_timestamp=that)` so `_to_rfc3339` matches (`:791-807`).
   Empty MAX → empty list (not all-history). Open positions
   (`load_open_positions()`, `:1137`), `account_state` (`:1309-1328`, may be
   `None`), newest 50 fills (`load_fills(limit=50)`, `:1627-1642`). Do **not**
   load candles, Parquet, `veto_ledger`, or `operator_journal` this phase.
   Stamp each watchlist row `stale: bool` using INV-21. Import
   `TIMEFRAME_BAR_LENGTH` from `signals/timeframes.py` (relocated by
   [[analyze-command]]; **do not** import `cli`). `max_candidate_age_bars`
   from `Settings`. Arithmetic identical to execute Step 1.5. `now` is
   `run_ask`'s injected clock.
2b. If every source is empty (watchlist 0, approved_set 0, no positions,
   `account_state` None, no fills): print
   `"ask: store has no grounded tables yet"`; exit 0; **no LLM**.
3. `build_context_pack(kind="ask", watchlist=..., approved_set=...,
   positions=..., account_state=..., fills=...)`. The **question** is only in
   the prompt (`{{question}}`), not a pack field. `sources` is builder-owned.
4. `run_companion_call(..., response_model=AskResponse,
   fallback=_offline_ask(pack))`.
5. Print: if `refused` → `"REFUSED: " + reason` and **no** `answer` body.
   Else print `answer`. Offline (`result is fallback`): `answer` starts with
   `"analysis unavailable"` plus a `sources` inventory; **not** an AC2
   refusal. Exit 0 except empty question.

## Acceptance criteria

1. Stub client: a question whose answer is a field in the loaded watchlist
   (e.g. top `rank` instrument) returns `refused=False` and `answer`
   containing that instrument string (fixture-controlled JSON).
2. Stub client returning a valid `AskResponse` with `refused=true` and
   `reason` exactly `live_quote` (question: “what is EUR_USD mid right now?”)
   prints `REFUSED: live_quote`. There is **no** keyword post-validate that
   overrides `refused=false` — grounding is prompt + acceptance stubs.
   Post-validate on a **valid** parse only: if `refused` and `reason` not in
   `{live_quote, unstored_news, foreign_account, not_in_store}` → treat as
   malformed → `_offline_ask(pack)` (does **not** count as this AC’s refusal).
   Omitting `refused` is malformed JSON → same offline path (AC 5), not REFUSED.
3. Offline / no key: zero I/O (INV-20); printed text contains
   `"analysis unavailable"` and at least one `sources` token; exit 0.
   This is not a `REFUSED:` line.
4. AST: same forbidden set as companion-core AC5 + `execution.reconcile`;
   `Position`/`Fill` imports allowed. No `OandaClient`, no `FairEconomyCalendar`.
5. `AskResponse` `extra="forbid"`; malformed JSON → `_offline_ask(pack)` (not a
   partial answer).
6. Watchlist-empty + approved-set-empty: still runs (operator may ask about
   positions). If **all** sources are empty (`watchlist 0`, `approved_set 0`,
   no positions, `account_state` None, no fills): print
   `"ask: store has no grounded tables yet"`; exit 0; **no LLM**.
7. Never writes store tables (row counts of the five sources unchanged).

## Sequence diagram

Skip — see Artefact verdicts.

## Component design

`ai/ask.py`:

```python
class AskResponse(BaseModel):
    model_config = {"extra": "forbid"}
    refused: bool
    reason: str  # "" when not refused
    answer: str  # ignored by printer when refused=True

def run_ask(db_path: str | Path, question: str, *, client=None, now: datetime) -> AskResponse: ...
```

Post-validate (valid parse only): `refused` ⇒ `reason` ∈ the four tokens;
else fallback. Empty `reason` with `refused=true` → fallback.

Prompt `ai/prompts/ask.md`: `{{question}}` + `{{context}}`. Answer only from
context JSON. Live mids / unstored news / other accounts → `refused=true`.
Equity/positions **now** → use last-reconcile rows and mention `as_of`.
Stale watchlist rows: may cite them; must not imply they are executable now.

## User flow

Skip — see Artefact verdicts.

## Artefact verdicts

- Sequence diagram: skip — single sync ask → pack → LLM → print.
- Component design: include — refusal vs answer printer rule and source set.
- User flow: skip — CLI-only.

## Non-goals

- No tool-calling / multi-step retrieval. One pack, one call.
- No candles / veto ledger / journal in the pack this phase.
- No execute, sizing, or “what would this trade do.”
- No session memory across asks.

## Touches

- INV-01 — read-only; AST as AC 4.
- INV-02 — **does not apply** (advisory).
- INV-16 — last-reconcile `account_state` is answerable; prompt cites `as_of`.
- INV-21 — pack stamps `stale` per candidate; reaction is **warn in context**,
  not refuse (execute refuses; pine labels; ask discloses).
- INV-03 — timestamps in pack as RFC-3339 strings (`mode="json"`).
- INV-08 — no key logging.
- INV-10 / INV-13 — watchlist and approved_set shapes from existing loaders.
- INV-14 — positions/fills frozen models.
- INV-20 — offline predicate.

## Events

- Written: none.
- Consumed: `watchlist`, `approved_set`, `positions`, `account_state`, `fills`.

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| `LLM_API_KEY` | offline predicate | runtime secret | `.env` |
| `LLM_BASE_URL` | endpoint | runtime | `.env` |
| `LLM_MODEL` | model id | runtime | `.env` |

## Wire-format contract

Request: user message = question + `ContextPack.data` JSON (snake_case keys
as loader dicts / `model_dump(mode="json")` for Position/Fill).

Response JSON:

| Field | Type | Notes |
|---|---|---|
| `refused` | bool | |
| `reason` | str | `""` or one of `live_quote`, `unstored_news`, `foreign_account`, `not_in_store` (plus optional human suffix after a space is **forbidden** — exact token only; extra text → fallback) |
| `answer` | str | shown only if `refused` is false |

## Inbound third-party wire contract

Skip — no provider webhook.

## Depends on

- `companion-core.md`
- [[analyze-command]] — `signals/timeframes.py` (INV-21 map home after phase-07).
- `signal-ranker.md` / INV-13 watchlist columns (`load_watchlist` keys)
- `full-universe-backtest-runner.md` / INV-10 `approved_set` keys
- `order-model-and-brackets.md` positions/fills
- `reconciliation.md` `account_state`

## Approach

`run_ask` + CLI `ask`; table-driven stub tests for AC 1–3 and AC 6; AST test.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| `load_watchlist()` latest run, INV-13 keys minus `run_timestamp` | `data/store.py:896-913` | Read docstring |
| `load_approved_set()` keys | `data/store.py:791-807` | Read docstring |
| `load_open_positions` | `data/store.py:1137` | Read def |
| `load_account_state` → dict or None | `data/store.py:1309-1328` | Already verified in review-command |
| `load_fills(limit=)` newest filled/partial | `data/store.py:1627-1642` | Read docstring |
| INV-21 map moves to `signals/timeframes.py` (today still `cli.py`) | `cli.py:217-221` today; [[analyze-command]] relocation | Read map; ask imports the leaf module, never `cli` |

## Constraint blast radius

**Fixed source set (no extra tables).**
- Protects: refusal boundary; operator knows what ask can see.
- Blocks: “just load candles so I can ask about last H1 close” — that is a
  later spec.

**Printer drops `answer` when `refused`.**
- Protects: model cannot smuggle a fake quote in `answer` beside `refused`.
- Blocks: showing both for “transparency.”

## Smoke checklist hooks

- Three operator questions on a demo DB with watchlist+approved_set+one
  position (phase Done-when).
- One question “what is EUR_USD mid right now?” → `REFUSED:`.

## Open questions

- None load-bearing. Calendar/news in pack is deferred (would blur
  `unstored_news`).

## Out of scope

- Journal/veto-ledger Q&A.
- Streaming / follow-up turns.

## Notes

Phase-08 anticipated-specs “question → context routing” is implemented as a
**fixed pack**, not an LLM router — routing by dropping tables would make
refusals untestable. The “router” is the printer + refusal enum.
