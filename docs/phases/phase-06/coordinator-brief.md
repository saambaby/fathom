# WS0 Followup-Sweep — Coordinator Brief

**Date:** 2026-09-01 · **Base:** `main` @ post-#132 · **Work list:** issues #134–#143 (`phase:ws0`) + completing PR #133 (WS1 adapter).
**Task specs:** `docs/implementation-plan.md` (Workstream 0 + Strategy Verification Protocol + Workstream 1).

## Baseline (recorded before wave 1)

- **No CI exists** (created by #141). Substitute merge gate until then: fresh-venv `python -m pytest` + `python -m mypy .` run by the reviewer.
- **Known pre-existing failures on main** (not blockers for unrelated PRs; #141 fixes them):
  `tests/test_execution_cli.py::TestCliHelp::test_fathom_help_lists_all_subcommands` and
  `tests/test_panel_data.py::TestReadOnlyBoundary::test_panel_data_does_not_import_forbidden_modules`
  (hardcoded `/home/sam-baby/...` paths), plus `tests/test_stream.py::...::test_reconnect_on_heartbeat_timeout` (flaky under full-suite load only). mypy: 2 `unused-ignore` in `data/store.py:1640,1695`.
- **#143 blocked-on-human:** no candle db and no `.env`/OANDA token on this machine; the fixture requires real OANDA data (no-mocks rule). Operator must supply a token or a db export.

## Dependency edges / conflict map

- `cli.py` is the hot file: #135 (~1586), #137 (~1384), #139 (~1496), #140 (help strings ~826/1343), #136 (call site ~595), WS1 Task 1.3 (~1546). **Never more than one cli.py-touching task in flight.**
- `execution/models.py`: #134 then #142 (serialize).
- #140 (docs) runs LAST — its doc-lint must match post-WS1/post-#139 reality.
- #141 first — it un-reds the suite and stands up CI for every later PR.
- Independent: #138 (scripts/run_monitor.py), WS1 (hermes_integration + config; pyproject touch conflicts with #141's lockfile → WS1 rebases after #141 merges).

## Waves (concurrency cap: 4; drop to 2 if rebase conflicts cascade)

| Wave | Items | Role/agent |
|---|---|---|
| 1 | #141 (portability+CI) · #134 (INV-15 date) · #138 (alerter) · WS1 finish (PR #133, tasks 1.1–1.4) | sonnet · opus · sonnet · opus |
| 2 | #136 (annualization) · #142 (frozen contracts, after #134) | opus · sonnet |
| 3 | #135 (rate refusal) → #137 (TTL) → #139 (attestation/runbook) — serialized (cli.py) | opus/sonnet/opus |
| 4 | #140 (docs, last) | sonnet |
| — | #143 blocked-on-human (operator supplies OANDA token/db) | opus, later |

## Worker boilerplate (every dispatch carries this)

- Create your own worktree from `origin/main` under `/Users/sambaby/Development/@saam.baby/arp/fathom/.claude/worktrees/ws0-<issue>` on branch `fix/ws0-<issue>`; **cd into it first**; never touch other worktrees. Never use bare `git stash`.
- Env: `uv venv .venv -p 3.11 && VIRTUAL_ENV=$PWD/.venv uv pip install -e '.[dev]'`; run tools as `.venv/bin/python -m pytest` / `-m mypy .`.
- TDD: failing test verified before implementation. Full suite + whole-repo mypy before PR; pre-existing baseline failures (above) are tolerated and must be listed in the PR body — no NEW failures.
- Commits: conventional style, **no AI-attribution trailers of any kind**. PR title ≤ 70 chars. Do not merge — the coordinator merges after fresh review.

## Reviewer gates (fresh reviewer per PR; never the implementer)

Diff-vs-issue ACs · anti-tautology (tests fail without the fix) · `grep -n "INV-" ` on the diff for invariant claims vs `docs/product/invariants.md` · env-drift (new env vars in `.env.example` + docs — `.env.example` keys must match Settings fields) · spec amendments land in the SAME PR · full local gate (pytest + mypy, fresh venv).
