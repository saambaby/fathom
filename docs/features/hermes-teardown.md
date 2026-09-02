# Feature: hermes-teardown

**Status:** ready
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
   `tests/test_hermes_job.py`,
   `tests/test_execution_cli.py::TestInv01Boundary::test_daily_md_allowlist_unchanged`,
   `signals/charts.py`, `tests/test_charts.py`, the `chart` subparser + `cmd_chart` in
   `cli.py`, and the `matplotlib` dependency in `pyproject.toml` (its only consumers
   are the two deleted files). The deletion manifest below is authoritative where it
   and this list differ. Suite + CI green.
2. `git grep -i 'hermes'` over code, tests, `CLAUDE.md`, `docs/product/`,
   `docs/phases/phases-manifest.json`, and `docs/operator-acceptance.md` hits only:
   the `hermes_integration/` package name itself (renamed next by
   ai-package-migration), historical phase/results docs — **including the `name`
   strings of phase entries in `phases-manifest.json`, which are historical phase
   identity and are never rewritten** — and explicit "removed/superseded" notes (the
   amended phase-02 outcome string is one such note; phase-07's own "de-Hermes" name
   is a removal description and sanctioned on the same basis).
   **Timing:** the `docs/product/` leg of this grep is accepted at **phase close**
   together with AC 4 (`architecture.md` keeps its Hermes prose until the deferred
   redraw); the commit-2 grep excludes `docs/product/architecture.md` and must pass on
   everything else. This taxonomy (package name pre-rename · historical phase/results
   docs · explicit removed/superseded notes) refines the phase Done-when's shorthand
   "nothing but historical phase/results docs". The doc-lint guard also asserts the
   INDEX amendments below (retired statuses + de-Hermesed cli-commands/monitor-alerts
   summaries) so they have an executable acceptance check. The doc-lint test's dead-name guard is updated
   so retired names (`fathom chart`, the job doc, T-08) cannot reappear in operator docs.
3. Invariant re-wordings land in `docs/product/invariants.md` with rule *substance*
   unchanged: INV-01 restated as "no AI/analysis surface may import or invoke
   execution — order authority lives solely behind operator-run `fathom execute`"
   (Hermes named only in a note using retired/superseded phrasing, so the mention
   lands in AC 2's sanctioned-note category); INV-02 generalized off provider
   vocabulary — title/Rule "Claude" → "LLM" and enforcement text naming the
   OpenAI-compatible adapter + parse boundaries instead of "`anthropic` SDK call"
   (consistent with INV-20); INV-13 reworded **in full** — title ("Frozen
   Hermes-Facing Wire Contract" → e.g. "The `Candidate` Model Is the Frozen Wire
   Contract"), Rule, Reason, and Enforcement — replacing the Hermes-job /
   Discord-watchlist consumer vocabulary with the live consumers (portfolio, cli,
   narration, pine, panel); the frozen-contract substance and pinned field list are
   untouched.
4. `docs/product/architecture.md`'s container diagram is redrawn: HERMES and
   DISCORD-watchlist nodes gone, `ai/` + PINE + LLM-provider nodes present (matching
   the phase-07 phase-doc diagram); the "Hermes Boundary" prose section is replaced by
   the AI-surface boundary; data-flow sections describe `fathom analyze`.
5. `docs/operator-acceptance.md`: T-08 removed from the ordered gate list with a
   superseded-by note pointing at the pine/analyze acceptance walk; remaining gates
   (T-11, T-06, T-05) renumber-free and intact **except** the residue rewords: the
   "4 remaining gates" framing becomes 3; T-05's precondition "gates 1–3 closed
   positive (INV-07)" becomes "T-11 + T-06 closed positive **plus** the phase-07
   pine/analyze acceptance walk"; T-06's "Watchlist (mirrors Discord)" phrasing drops
   the Discord reference; and the prerequisites/one-time-setup rows referencing
   Hermes-side keys or a running Hermes instance (today :37, :40, :43-44) are removed
   or reworded. The hermes grep alone is NOT the completeness check for this file —
   a second residue sweep (`grep -inE 'discord|gate 1|gates 1|four gates'`) catches
   the Discord/gate-count leftovers (today at least :23 "Gates 1–3 build the demo
   track record", :39 `DISCORD_WEBHOOK_URL` "Needed for: gate 1 (watchlist)", :110,
   :141 "gates 1–3", :156 "the four gates above"; the grep pattern is illustrative —
   the "four gates" framing at :4-5 and :14 is line-wrapped past it and is verified by
   read-through under AC 5's 4→3 reword mandate).
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
| `cli.py` chart subparser (:815-846) + `cmd_chart` (def at :1268; banner :1264) | delete | argparse wiring only |
| `matplotlib` in `pyproject.toml` | remove dep | only signals/charts.py + its test import it |
| Surviving-code Hermes **prose** (comments/docstrings in kept modules and tests — grep-driven at implementation time; today e.g. `cli.py:68,114,849-911,2054-2097`, `signals/ranker.py:10,86-91`, `signals/scan.py:26,200`, `monitoring/alerts.py:6-12`, `execution/orders.py:5`, `config/settings.py:35`, `tests/test_cli_commands.py:130-142`, `tests/test_execution_cli.py:23,934`) | reword to standalone-platform vocabulary in commit 1; `cli.py:114`'s "Hermes allow-list (scan/watchlist/chart) is unchanged" becomes factually false at deletion time and MUST be rewritten | ai-package-migration's later sweep only fixes the literal `hermes_integration` path string, never plain "Hermes" prose — this row is the only owner |
| `docs/features/INDEX.md` rows: chart-generation, hermes-job-definitions | status → `retired (phase-07)` with note | spec files kept as history |
| `docs/features/INDEX.md` rows: cli-commands, monitor-alerts | amend summaries — cli-commands drops `chart` + "(Hermes tools; the Hermes boundary)", monitor-alerts drops "via Hermes gateway" (webhook is direct) | rows currently advertise retired vocabulary at status `ready` |
| `CLAUDE.md` — header line ("orchestrated by Hermes Agent"), Stack row ("Hermes Agent (Nous Research)"), Commands, doc map, phase-status table | remove `fathom chart` + Hermes/Discord vocabulary; phase table mirrors the manifest change below | trigger-table routine |
| `docs/phases/phases-manifest.json` — phase-02 entry | `open_gate` (T-08) removed; status → `completed`; outcome amended: "code merged; Discord watchlist delivery retired before operator acceptance (phase-07 teardown) — acceptance responsibility transfers to the phase-07 pine/analyze walk" | manifest is the declared source of truth; CLAUDE.md phase table mirrors it |
| invariants INV-01/02/13 | reword per AC 3 | every phase-07 spec builds on the new wording |
| `docs/product/spec.md` — **every** present-tense Hermes workflow mention (grep-driven at implementation time; today at least :15, :46, :73-74, :79) | reword/strike as superseded (phase-07) | AC 2 greps `docs/product/`; these are not historical docs — the AC-2 grep, not this line list, is the completeness check |
| `docs/product/code-map.md` — Hermes-job capstone/workflow prose (today :66-68) | reword: capstone retired, acceptance transferred to pine/analyze walk | same AC-2 scope. **Path-spelling rows** (`hermes_integration/` in area/dispatch tables, today :17, :65, :80) are NOT this spec's — ai-package-migration's import sweep updates them to `ai/` (see handoff note below) |
| `docs/product/architecture.md` | redraw diagram + boundary prose + data flows | AC 4; timing per Approach — lands at phase close |
| `docs/operator-acceptance.md` | retire T-08 + residue rewords | AC 5 |
| `docs/go-live-runbook.md` — T-08/Discord-watchlist prerequisite residue (today :45 "Phase 2 T-08 acceptance closed: the live Discord alert/watchlist delivery", :68, :358, :372 "T-08 [Y/N]" checklist rows) | reword: T-08 rows become "phase-07 pine/analyze acceptance walk closed" (mirroring AC 5's T-05 precondition change); any remaining T-08 mention takes the tagged retire/supersede form | the runbook is in the dead-name guard's scan set; without this row the guard goes red on lines no other row edits, and the INV-07 prerequisite would silently still demand the retired gate |

**Rename handoff:** the sanctioned-hit category "the `hermes_integration/` package
name itself" expires when ai-package-migration renames the package. The residue that
category covers — `docs/product/code-map.md` path spellings and the "today
`hermes_integration/…`" parentheticals in INV-20/INV-21 — is updated to `ai/` by
**ai-package-migration's** sweep (its AC 1 grep extends to `docs/product/` path
references). The phase-close AC-2 grep therefore has exactly two surviving sanctioned
categories: historical phase/results docs, and explicit removed/superseded notes.

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
- INV-13 — reworded in full (title, Rule, Reason, Enforcement) per AC 3; frozen-contract
  substance and field list untouched.
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

1. Code deletions + test/dep sweep + the surviving-code Hermes-prose reword
   (manifest row above), suite green — commit 1 (first task of the phase after
   pine-generation).
2. Invariant re-wordings + CLAUDE.md + operator-acceptance + go-live-runbook residue
   reword + phases-manifest + `docs/product/spec.md` / `code-map.md` workflow-prose
   rewords + INDEX flips/amendments + doc-lint guard update — commit 2 (doc-lint test
   proves the dead names stay dead).
3. **Architecture redraw + `fathom analyze` data-flow prose — deferred to the phase's
   closing docs task**, after ai-package-migration and analyze-command merge, so
   `architecture.md` never describes a package (`ai/`) or command that doesn't exist
   yet. AC 4 is accepted at phase close, not with commit 2.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| matplotlib's only consumers are the chart module + its test | repo grep: signals/charts.py, tests/test_charts.py | `grep -rln matplotlib --include='*.py'` returns exactly those two |
| panel does not import `signals.charts`; it renders via streamlit_lightweight_charts | panel/app.py:47 | read import block; repo-wide grep for `signals.charts` hits cli.py + tests/test_charts.py only |
| alerts module is a plain Discord-webhook httpx client, no Hermes dependency | monitoring/alerts.py:16,71,121-156 | read module: webhook URL from `Settings.discord_webhook_url`, injectable client |
| chart CLI surface location | cli.py:815-846 (subparser), cli.py:1268 (`cmd_chart`) | read wiring |
| doc-lint test hardcodes the jobs dir path | tests/test_docs_lint.py:20-21 | read test (`REPO_ROOT / "hermes_integration" / "jobs"`) |
| T-08 is gate 1 of the ordered operator checklist | docs/operator-acceptance.md:18,50 | read gate table + section |
| INV-01/INV-13 are written in Hermes vocabulary needing re-baseline | docs/product/invariants.md:8-14,134-140 | read rules; INV-02's "`anthropic` SDK" wording likewise (:26 region) |
| `discord_webhook_url` setting serves alerts and is documented for both alert/watchlist delivery today | config/settings.py:33,50 | read docstring + field |

## Constraint blast radius

**Doc-lint dead-name guard extended to retired surfaces.** Protects: `fathom chart`,
the daily job, and T-08 cannot silently resurface in operator docs after deletion.
Guard scope, pinned: the guard scans `docs/operator-acceptance.md`, `CLAUDE.md`, and
`docs/go-live-runbook.md` (the operator docs among tests/test_docs_lint.py's asserted
files; the test also asserts over `cli.py` and the deleted jobs dir, which are not
part of this dead-name scan). Within that set the literals `fathom chart` and `daily.md` are
forbidden everywhere; `T-08` is forbidden **except** inside a line containing the
substring "retire" or "supersede" (covers "retired"/"superseded-by" — the exact form
AC 5's note and the amended phase-02 outcome take, so the lint passes on the notes
this spec itself mandates). The INDEX amendments are asserted separately: a doc-lint
clause checks the four amended `docs/features/INDEX.md` rows carry their
retired/de-Hermesed wording (INDEX is not in the dead-name scan — its
hermes-job-definitions row name would trip it). Historical discussion lives in
`docs/phases/` and `docs/reference/`, which the guard does not scan. Blocks: operator
docs can only mention retired names in the tagged retire/supersede form — acceptable;
that is where history lives.

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
