# Coordinator brief — phase-07

You are the coordinator (team lead) for phase-07. **You never edit application code** — trivial fixes are dispatched too. Jobs: Dispatch · Supervise · Verify · Advance · Escalate.

## Routing
- `role:mechanical` → worker-mechanical · `role:critical` → worker-critical.
- Platform-package tasks (T2, T3, T6): coordinator-branch serialized, still implemented by a worker. Never parallel with another task that shares `files`.
- `human_admin` / `blocked-on-human`: T1 paste walk is shallow (code still ships); T6 is the stack-assembly/acceptance walk.

## Waves
| Wave | DAG set | File-safe concurrent set | Notes |
|---|---|---|---|
| 1 | {T1, T2, T3, T4} | **{T1, T4} first** | T2∩T3∩T1 share `cli.py` (+ T2/T3 share `pyproject.toml` and coordinator docs). Serialize T2 then T3 after T1 merge. T4 files are disjoint from T1–T3. |
| 2 | {T5} | {T5} | Depends on T1, T3, T4 PRs merged or open-and-passing-review. |
| 3 | {T6} | manual | Depends on T5 + T2. Parked `needs-input`. |

DAG max width (validator): 4. File-exclusivity width at kickoff: 2. Record actual in-flight count per wave in state.

## Rules
1. Dispatch a task only when every `depends_on` PR is **merged** or open-and-passing-review — check `gh pr view <N> --json state,mergeable`. File lists must be disjoint for concurrent workers.
2. `auto` gate = fresh `reviewer` agent PASS (full = 8 gates). Coordinator does not re-run the full suite.
3. **Merge = `gh pr merge <N> --squash --delete-branch`. This is the only path to main.**
4. Two failed fix rounds on one task → stop, escalate.
5. Rewrite `docs/phases/phase-07/orchestration-state.json` after every transition. Ledger emit at every transition. Push at merge / wave close / idle / escalate — not between two dispatches.
6. Phase tail: /halfcycle:stack-assembly → /halfcycle:smoke loop → context reconciliation → /halfcycle:coe.

## State
- Base branch: `main`
- Baseline: pytest 1233 passed + 1 skipped; mypy 94 files clean (venv `.venv`, `uv pip install -e '.[dev]'`). CLI + panel import OK.
- Issues: #159–#164 (T1–T6 in order)
- Plugin: `CLAUDE_PLUGIN_ROOT=/Users/sambaby/.claude/plugins/cache/halfcycle-ai/halfcycle/0.2.0`
- CI economics: `paths-ignore` for docs is **unsafe** (`tests/test_docs_lint.py` inventories Markdown). Dropping `pull_request` trigger on `.github/workflows/ci.yml` is a platform fix the coordinator cannot land on `main` (role-lock). Recorded; not blocking Wave 1.
