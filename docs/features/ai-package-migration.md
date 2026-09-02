# Feature: ai-package-migration

**Status:** ready
**Phase:** phase-07
**Owner:** operator + Claude
**Last updated:** 2026-09-01

## Summary

Rename `hermes_integration/` → `ai/` and make it the platform's single AI-analysis
package: the shared LLM adapter is extracted to `ai/llm_client.py`, and the two calls
Hermes used to perform externally — per-candidate **news-risk** and **narration** — gain
in-process call functions on that adapter, mirroring the pattern `pretrade_check` already
uses. The INV-02 parse boundaries, safe defaults, and prompt templates are moved
**unchanged**. This is the load-bearing enabler for ADR-001 (standalone platform): after
it, no Fathom capability requires an external orchestrator.

## User-facing behaviour

- No new CLI surface. Observable changes: `import ai.…` works; `import
  hermes_integration.…` no longer exists (hard break, no compatibility shim — nothing
  external imports it once the Hermes job is retired by hermes-teardown).
- `fathom execute` behaves byte-identically; only its import site changes.
- New library functions (consumed by analyze-command, not by users directly):
  - `ai.news_risk.news_risk_check(candidate, calendar_events, entry_window_utc, *,
    client=None) -> NewsRiskVerdict` — both strings caller-supplied (the `Candidate`
    carries no entry-window field; analyze-command computes it)
  - `ai.narration.narrate(candidate, *, client=None) -> NarrationResult` where
    `NarrationResult` is a small frozen pydantic model
    `{text: str, source: Literal["model", "fallback"]}` — `text` is the model's line or
    `fallback_narration` output (never empty), `source` tells the caller which it was
    (analyze-command persists it as `narration_source`; its third value `none` is
    analyze's own, for vetoed candidates that were never narrated). `narrate` never
    raises.

## Acceptance criteria

1. `git grep -l 'hermes_integration'` over tracked `*.py` + `pyproject.toml` returns
   nothing; the full test suite passes with the `ai.` import paths (tests renamed/updated
   in the same change). The sweep also updates **path spellings in durable docs** to
   `ai/`: `docs/product/code-map.md`'s area/dispatch rows and the "today
   `hermes_integration/…`" parentheticals in INV-20/INV-21 (hermes-teardown's rename
   handoff — its phase-close grep depends on this).
2. `ai/llm_client.py` owns `OpenAICompatClient` + the `_ClientAdapter` protocol;
   `ai/pretrade_check.py` imports them from there and its public API
   (`pretrade_check`, `parse_pretrade_verdict`, `PretradeVerdict`, `MODEL`,
   re-exported `OpenAICompatClient` — the full existing import surface, per the
   re-export list in Component design; `cli.py:131` imports the adapter from here) is
   unchanged —
   proven by the existing pretrade test suite passing with only import-path edits.
3. `news_risk_check` follows the exact `pretrade_check` algorithm: no client + no
   `LLM_API_KEY` → safe default `skip` verdict without network I/O; injected stub client
   → prompt built from `ai/prompts/news_risk.md` with all six placeholders substituted;
   any transport/parse failure → `parse_news_risk` safe default (INV-02). Offline tests
   cover all three paths.
4. `narrate` returns `NarrationResult(text=<model line>, source="model")` when
   `should_use_fallback` says usable, else
   `NarrationResult(text=fallback_narration(candidate), source="fallback")`; with no
   key/client it returns the fallback result without network I/O; it never raises and
   `text` is never empty (NOT INV-02 — cosmetic contract preserved).
5. `parse_news_risk`, `parse_pretrade_verdict`, both pydantic verdict models, and all
   three prompt template files are moved byte-identical (templates) / behavior-identical
   (parsers — existing parser tests pass unmodified except import paths).
6. INV-08 holds: `LLM_API_KEY` appears in no log line or `repr` on any new path
   (existing key-hygiene tests extended to `news_risk_check`/`narrate`).

## Component design

```
ai/
  __init__.py        # package docstring rewritten: standalone AI analysis (no Hermes)
  llm_client.py      # OpenAICompatClient, _ClientAdapter, MODEL, DEFAULT_BASE_URL,
                     # _HTTP_TIMEOUT_S — extracted verbatim from pretrade_check.py
  pretrade_check.py  # verdict model + parser + pretrade_check(); imports adapter
  news_risk.py       # existing model + parser  ➕ _build_prompt() + news_risk_check()
  narration.py       # existing fallback + usability check  ➕ narrate() + NarrationResult
  prompts/           # pretrade.md, news_risk.md, narration.md — moved unchanged
```

- `news_risk_check` prompt building: `news_risk.md`'s placeholders (`{{instrument}}`,
  `{{base_currency}}`, `{{quote_currency}}`, `{{direction}}`, `{{entry_window_utc}}`,
  `{{calendar_events}}`) are filled from the `Candidate` plus the two caller-supplied
  positional strings (`calendar_events` pre-rendered text, `entry_window_utc`). Fetching
  and rendering calendar events, and computing the entry window, is the **caller's** job
  (analyze-command) — this module makes one LLM call per invocation and knows nothing
  about the store.
- `MODEL` and `OpenAICompatClient` move to `ai/llm_client.py` and are **re-exported** by
  `ai/pretrade_check.py`, so its public API (AC 2) and existing import sites
  (`tests/test_config.py:245` pattern) keep working with a path-only edit.
- Ordering: **hermes-teardown runs first.** It deletes `hermes_integration/jobs/`
  (`daily.md`), `tests/test_hermes_job.py`, and the doc-lint rows referencing them, so
  this feature renames a jobs-free package and AC 1's clean grep is achievable.
  (`tests/test_hermes_job.py:1,27,46` is a lint test over the job *doc* — teardown scope,
  settled.)
- Settings: `config/settings.py`'s `llm_*` fields already exist; the only settings edit
  is the comment that names the model-constant's home module.

## Artefact verdicts

- Sequence diagram: **skip** — single-process library refactor; the pretrade algorithm's
  5 steps are already documented in code and reused verbatim.
- Component design: **include** — the package layout and the "caller renders calendar
  text" boundary are the two decisions implementers would otherwise improvise.
- User flow: **skip** — no user-facing surface.

## Non-goals

- No behavior change to the pre-trade veto, its prompt, model constant, or timeout.
- No new prompt content — `news_risk.md`/`narration.md` move as-is; prompt tuning is
  analyze-command/market-brief territory.
- No orchestration (candidate loops, calendar fetch, persistence) — analyze-command.
- No deletion of Hermes artifacts (`jobs/daily.md`, Discord references, docs) — hermes-teardown.
- No compatibility shim for `hermes_integration` imports.

## Touches

- INV-01 — `ai/` remains verdict-only: no order/execution/risk import — re-proven by
  extending the existing forbidden-substring boundary scan
  (tests/test_cli_commands.py:135-175) and the AST probe pattern
  (tests/test_admin_panel.py) to the renamed package.
- INV-02 — both parse boundaries preserved; `news_risk_check` adds the same fail-closed
  wrapper `pretrade_check` has.
- INV-08 — key privacy on all new call paths (AC 6).
- INV-20 — this feature *creates* the invariant's single adapter (`ai/llm_client.py`)
  and gives `news_risk_check`/`narrate` the uniform offline predicate.

## Events

- Written: none. — Consumed: none. (Verdict persistence is analyze-command's concern.)

## Environment variables

| Var | Purpose | Arg type | Where set |
|---|---|---|---|
| `LLM_API_KEY` | auth for all in-process LLM calls (now three call sites, one adapter) | runtime secret | operator `.env` |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | runtime | operator `.env`, default `https://api.openai.com/v1` |
| `LLM_MODEL` | model id for all three calls | runtime | operator `.env`, default `MODEL` constant |

No new variables; the three existing ones now also govern news-risk and narration.

## Wire-format contract

Unchanged and already pinned by the moved modules (this spec freezes that they move
*without* amendment):

- News-risk response: `{"event_risk": "high"|"medium"|"low", "reason": str,
  "suggest_action": "proceed"|"reduce_size"|"skip"}` — strict enums, `extra="forbid"`.
- Pretrade response: `{"decision": "proceed"|"block", "reason": str}`.
- Narration response: one plain-text line; usability rule = non-empty ∧ ≤280 chars.
- Outbound LLM request: OpenAI chat-completions, single user turn, `Authorization:
  Bearer` — as implemented by `OpenAICompatClient.complete`.

## Depends on

- pretrade-check (shipped) — source of the adapter + algorithm being generalized.
- news-risk-assessment / watchlist-narration (shipped) — the parsers/fallbacks being moved.
- hermes-teardown (**hard, runs first**) — deletes `jobs/daily.md`,
  `tests/test_hermes_job.py`, and the chart/Discord surfaces before the rename; this
  feature's AC 1 grep assumes those path references are already gone.

## Approach

1. (After hermes-teardown lands.) `git mv hermes_integration ai` — jobs-free by then;
   extract `llm_client.py` (+ re-exports); fix imports (cli.py:131, tests, pyproject
   packages list); suite green — commit 1.
2. Add `news_risk_check` + `_build_prompt` (TDD: offline/no-key path, stub-client happy
   path, six-placeholder substitution, failure→skip) — commit 2.
3. Add `narrate` + `NarrationResult` (TDD: usable response, fallback response, no-key
   path, source values) — commit 3.
4. Extend AST boundary + key-hygiene tests to the package — commit 4.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| Adapter + protocol currently live inside pretrade_check and are self-contained (httpx only) | hermes_integration/pretrade_check.py:201-309 | read both classes; no other module-level deps beyond httpx/os/logging |
| `pretrade_check`'s 5-step fail-closed algorithm is the pattern to replicate | hermes_integration/pretrade_check.py:355-436 | read function end-to-end |
| news_risk.py today is parser-only, no LLM call, no SDK dep | hermes_integration/news_risk.py:16-17 | module docstring + no client import in file |
| narration.py exposes `fallback_narration` (never raises/never empty) + `should_use_fallback` (≤280) | hermes_integration/narration.py:47-143 | read both functions |
| The six news-risk placeholders come from the Hermes job's substitution table | hermes_integration/jobs/daily.md:93-100 | read table incl. `{{calendar_events}}` on line 100; template file exists at prompts/news_risk.md |
| The only non-test production import site is cli.py | cli.py:131 (`from hermes_integration.pretrade_check import …`) | repo-wide grep: cli.py + tests + a settings comment (config/settings.py:98) only |
| pyproject must be edited: package list names `hermes_integration*` | pyproject.toml:41 | read include list |
| Test files needing updates: test_pretrade_check, test_news_risk (imports at :20 **and** logger-name string assertions at :416-436), test_narration, test_config, test_live_gate, test_execution_cli, test_cli_commands, test_docs_lint (path strings) — test_hermes_job **and** test_execution_cli's daily.md allow-list test (tests/test_execution_cli.py:966-982) are deleted by teardown first (named in its manifest); this feature retargets the surviving `TestInv01Boundary` directory-scan test (tests/test_execution_cli.py:938-964, rglobs `hermes_integration/`, would pass vacuously after the rename) to `ai/`, plus import-path edits | grep hits, e.g. tests/test_pretrade_check.py:26, tests/test_news_risk.py:20, tests/test_config.py:245, tests/test_live_gate.py:391 | repo-wide grep for `hermes` in tests/ |
| `llm_*` settings fields exist; comment points at the model constant's home | config/settings.py:98 | read comment ("hermes_integration.pretrade_check.MODEL") — must be updated to `ai.llm_client.MODEL` |

## Constraint blast radius

**Hard removal of the `hermes_integration` import path.** Protects: no zombie external
integration can silently keep calling the old contract. Blocks: any operator script or
Hermes deployment still importing the old path breaks loudly at import time — accepted
and intended (ADR-001); the only known external consumer is the Hermes job being retired.

## Smoke checklist hooks

- `fathom execute <ref> --dry-run` walks the gate on the renamed package (no
  behavior delta vs pre-migration run).
- `python -c "from ai.news_risk import news_risk_check"` and the no-key offline call
  returns the `skip` default in <1s with no network.

## Open questions

_None — the `test_hermes_job.py` ownership question was settled during review: it lints
the job doc (tests/test_hermes_job.py:1,27,46) and is deleted by hermes-teardown._

## Out of scope

- Everything listed in Non-goals; plus any change to `Candidate` (INV-13) — the new
  functions consume it read-only.

## Notes

Second spec of the phase-07 sprint; analyze-command and market-brief build directly on
the two new call functions.

Implementer note: this spec's boundary is authoritative over the phase-07 diagram's
shorthand — `ai/` modules know nothing about the store or the calendar: verdict
persistence (`analysis_log`) and calendar fetching/rendering are analyze-command's
call-site concerns (the diagram's `CALENDAR --> CLI` edge reflects this).
