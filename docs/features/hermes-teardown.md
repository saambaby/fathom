# Feature: hermes-teardown

**Status:** draft
**Phase:** phase-07
**Owner:** operator + Claude
**Last updated:** 2026-09-01

## Summary

The deletion-and-re-baseline half of ADR-001: remove every Hermes-orchestration artifact
(the daily job definition, its lint test, the PNG chart command and module, the Discord
*watchlist* delivery contract, the T-08 operator gate) and re-baseline the product docs —
container diagram without Hermes/Discord-watchlist nodes, INV-01/02/13 reworded off the
Hermes vocabulary. It deletes and rewords only; it adds no behavior. **Runs before
ai-package-migration** (settled during spec review): the rename then operates on a
jobs-free package and migration's clean-grep AC becomes achievable. The deviation
*monitor's* Discord webhook alerting is explicitly kept — it is Hermes-free.

## User-facing behaviour

- `fathom chart` no longer exists: `fathom chart …` exits with argparse's standard
  unknown-command error; `fathom --help` no longer lists it. All other commands
  unchanged.
- No Hermes job doc ships; no watchlist ever posts to Discord. Deviation alerts
  (`monitoring/alerts.py`, `DISCORD_WEBHOOK_URL`) continue to work unchanged.
- Docs describe the standalone platform: architecture diagram, invariants, operator
  acceptance (T-08 retired, replaced by the pine-generation acceptance walk), CLAUDE.md
  command table.

## Acceptance criteria

1. Deleted and unreferenced: `hermes_integration/jobs/` (daily.md),
   `tests/test_hermes_job.py`, `signals/charts.py`, `tests/test_charts.py`, the
   `chart` subparser + `cmd_chart` in `cli.py`, and the `matplotlib` dependency in
   `pyproject.toml` (its only consumers are the two deleted files). Suite + CI green.
2. `git grep -i 'hermes'` over code, tests, `CLAUDE.md`, `docs/product/`, and
   `docs/operator-acceptance.md` hits only: the `hermes_integration/` package name
   itself (renamed next by ai-package-migration), historical phase/results docs, and
   explicit "removed/superseded" notes. The doc-lint test's dead-name guard is updated
   so retired names (`fathom chart`, the job doc, T-08) cannot reappear in operator docs.
3. Invariant re-wordings land in `docs/product/invariants.md` with rule *substance*
   unchanged: INV-01 restated as "no AI/analysis surface may import or invoke
   execution — order authority lives solely behind operator-run `fathom execute`"
   (Hermes named only in a historical note); INV-02's enforcement text names the
   OpenAI-compatible adapter + parse boundaries instead of "`anthropic` SDK call";
   INV-13's Reason clause lists the live consumers (portfolio, cli, narration, pine,
   panel) instead of "the Hermes daily job / Discord watchlist".
4. `docs/product/architecture.md`'s container diagram is redrawn: HERMES and
   DISCORD-watchlist nodes gone, `ai/` + PINE + LLM-provider nodes present (matching
   the phase-07 phase-doc diagram); the "Hermes Boundary" prose section is replaced by
   the AI-surface boundary; data-flow sections describe `fathom analyze`.
5. `docs/operator-acceptance.md`: T-08 removed from the ordered gate list with a
   superseded-by note pointing at the pine/analyze acceptance walk; remaining gates
   (T-11, T-06, T-05) renumber-free and intact.
6. `panel/app.py` and `monitoring/alerts.py` are untouched by the deletion commit
   (verified consumers: panel renders via `streamlit_lightweight_charts`, not
   `signals.charts`; alerts POST the webhook directly).

## Component design

Deletion manifest (the spec's core artifact — the taskgraph executes it verbatim):

| Target | Action | Consumers verified |
|---|---|---|
| `hermes_integration/jobs/daily.md` | delete | tests/test_hermes_job.py, tests/test_docs_lint.py:20-21 (both updated/deleted here) |
| `tests/test_hermes_job.py` | delete | lint test over the deleted doc |
| `tests/test_execution_cli.py::TestInv01Boundary::test_daily_md_allowlist_unchanged` (:966-982) | delete | asserts the deleted doc's allow-list; already `pytest.skip`s when the file is absent — deleted here so no zombie skip survives. (The class's sibling test that rglobs `hermes_integration/` is **kept** and retargeted to `ai/` by ai-package-migration's import sweep.) |
| `signals/charts.py` | delete | cli.py only (+ its own test) |
| `tests/test_charts.py` | delete | — |
| `cli.py` chart subparser (:815-845) + `cmd_chart` (:1264-…) | delete | argparse wiring only |
| `matplotlib` in `pyproject.toml` | remove dep | only signals/charts.py + its test import it |
| `docs/features/INDEX.md` rows: chart-generation, hermes-job-definitions | status → `retired (phase-07)` with note | spec files kept as history |
| `CLAUDE.md` Commands + doc map | remove `fathom chart`, Hermes/Discord rows | trigger-table routine |
| invariants INV-01/02/13 | reword per AC 3 | every phase-07 spec builds on the new wording |
| `docs/product/architecture.md` | redraw diagram + boundary prose + data flows | AC 4 |
| `docs/operator-acceptance.md` | retire T-08 | AC 5 |

Kept (explicit non-targets): `monitoring/alerts.py` + `DISCORD_WEBHOOK_URL` setting
(deviation alerts, Hermes-free); `panel/` (charts view renders from the store);
`hermes_integration/*.py` modules + prompts (ai-package-migration renames them);
`docs/features/chart-generation.md` / `hermes-job-definitions.md` spec files (history).

## Artefact verdicts

- Sequence diagram: **skip** — deletions and doc edits; no runtime interaction.
- Component design: **include** — the deletion manifest with per-target consumer
  verification IS the design; it prevents both under- and over-deletion.
- User flow: **skip** — no surface added.

## Non-goals

- No package rename (`hermes_integration` → `ai`) — ai-package-migration, which runs
  immediately after.
- No removal of the deviation monitor's Discord alerting or its settings.
- No panel changes (whether Pine/terminal obsoletes panel views is a later Layer-3
  decision, per the phase doc).
- No invariant *substance* change — re-wordings only (AC 3); anything stricter or looser
  is out of scope.
- No deletion of historical docs (phase-02 results, retired spec files).

## Touches

- INV-01 — reworded here (substance preserved); the enforcement boundary this phase
  makes package-name-independent.
- INV-02 — enforcement text reworded here.
- INV-13 — Reason-clause consumer list re-baselined here.
- INV-08 — `DISCORD_WEBHOOK_URL` stays a SecretStr; no secret touched by deletions.

## Events

- Written: none. — Consumed: none.

## Environment variables

| Var | Purpose | Arg type | Where set |
|---|---|---|---|
| `DISCORD_WEBHOOK_URL` | **kept** — deviation alerts only (watchlist delivery is gone); docstring updated to drop "watchlist delivery" | runtime secret | operator `.env` |

## Wire-format contract

None — this feature removes contracts. The retired ones: the Hermes tool allow-list
(scan/watchlist/chart) and the Discord watchlist message format (both defined in
`hermes_integration/jobs/daily.md`, deleted).

## Depends on

- pine-generation — must be `ready` (and ideally its acceptance walk passed) before
  T-08's replacement note can point at it; the walk is the presentation layer's
  continue/abort gate.
- (Enables) ai-package-migration — hard ordering: teardown first.

## Approach

1. Code deletions + test/dep sweep, suite green — commit 1.
2. Invariant re-wordings + architecture redraw + CLAUDE.md + operator-acceptance +
   INDEX status flips + doc-lint guard update — commit 2 (doc-lint test proves the dead
   names stay dead).

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| matplotlib's only consumers are the chart module + its test | repo grep: signals/charts.py, tests/test_charts.py | `grep -rln matplotlib --include='*.py'` returns exactly those two |
| panel does not import `signals.charts`; it renders via streamlit_lightweight_charts | panel/app.py:47 | read import block; repo-wide grep for `signals.charts` hits cli.py + tests/test_charts.py only |
| alerts module is a plain Discord-webhook httpx client, no Hermes dependency | monitoring/alerts.py:16,71,121-156 | read module: webhook URL from `Settings.discord_webhook_url`, injectable client |
| chart CLI surface location | cli.py:815-845 (subparser), cli.py:1264-1268 (`cmd_chart`) | read wiring |
| doc-lint test hardcodes the jobs dir path | tests/test_docs_lint.py:20-21 | read test (`REPO_ROOT / "hermes_integration" / "jobs"`) |
| T-08 is gate 1 of the ordered operator checklist | docs/operator-acceptance.md:18,50 | read gate table + section |
| INV-01/INV-13 are written in Hermes vocabulary needing re-baseline | docs/product/invariants.md:8-14,132-136 | read rules; INV-02's "`anthropic` SDK" wording likewise (:26 region) |
| `discord_webhook_url` setting serves alerts and is documented for both alert/watchlist delivery today | config/settings.py:33,50 | read docstring + field |

## Constraint blast radius

**Doc-lint dead-name guard extended to retired surfaces.** Protects: `fathom chart`,
the daily job, and T-08 cannot silently resurface in operator docs after deletion.
Blocks: any future doc legitimately discussing the *history* of these names must use the
lint's allow-listed historical docs — acceptable; that is where history lives.

## Smoke checklist hooks

- `fathom --help` shows no `chart`; `pytest` green on a fresh venv without matplotlib.
- Doc-lint passes; `git grep -i hermes docs/product/ CLAUDE.md` → only sanctioned hits
  (AC 2 list).

## Open questions

_None._

## Out of scope

- Everything in Non-goals; retirement of `docs/operator-acceptance.md` gates other than
  T-08.

## Notes

Fifth and final spec of the phase-07 sprint. Implementation order within the phase:
pine-generation → **hermes-teardown** → ai-package-migration → market-brief →
analyze-command.
