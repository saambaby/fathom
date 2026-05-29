# Fathom — Build Playbook

**What this is.** The reproducible process we used to build Fathom from a blank
directory to a working multi-strategy trading system, plus the current status of
every phase. Read it to (a) see what is done and what's next, and (b) replay the
exact method — the kickoff prompt, the half-cycle layers, the runbooks, and the
orchestration pattern — on the next phase or the next project.

**Audience.** The operator (you) and any future Claude Code session picking this
up. Every prompt block below is copy-paste ready.

---

## Part 1 — Status at a glance

| Phase | Scope | Status | Result |
|---|---|---|---|
| **PoC** | MA-crossover only, 3 pairs × {H1,D} × 6 params = 36 combos, walk-forward + costs | ✅ Done | **0/36 approved** — honest negative; strict per-window gate validated. [poc-results.md](phases/poc-results.md) |
| **Phase 1A** | 6 strategies × 3 pairs × {H1,H4,D} = 72 combos, full backtest runner + swap costs | ✅ Done | **10/72 approved** — genuine OOS edge (Bollinger-on-D strongest but thin; H4 breakouts more trustworthy). O(n) engine: 30 min → 22 s. [phase-1a-results.md](phases/phase-1a-results.md) |
| **Phase 1B** | Live pricing stream + economic calendar | ✅ Done | Live demo ticks received; 97 FF calendar events stored. Two leaks caught + fixed at the acceptance gate. [phase-1b-results.md](phases/phase-1b-results.md) |
| **Phase 2** | Signal ranker → watchlist (Candidate contract), portfolio limits, charts, Claude news-risk, narration, CLI, Hermes job defs | ✅ Code merged | All 7 code/config tasks merged. `Candidate` pinned as **INV-13**. |
| **Phase 2 — T-08** | Live Discord acceptance of the daily watchlist job | ⏳ **Operator gate** | Blocked on human: needs a configured Hermes + Discord webhook + Anthropic key. See *What's next*. |
| **Phase 3** | Position sizing / order placement | ◻ Not started | The first phase that crosses the INV-01 boundary — design carefully. |

**Health (as of last run).** `mypy .` → 0 errors (56 files, strict). `pytest` →
667 passed. Both are the merge gate — keep them green.

### What's next (concrete)

1. **Close P2-T-08 (operator).** This is a human-in-the-loop acceptance, not a
   coding task. The daily Hermes job is defined (`hermes_integration/`); to accept
   it live you must: configure the Hermes agent, set a Discord webhook + Anthropic
   API key, run the job once, and confirm a ranked watchlist narration posts to
   Discord with **no order placed** (INV-01). Until that's done Phase 2 is
   "code-complete, acceptance-pending."
2. **Phase 3 kickoff (planning only).** Sizing/orders cross INV-01 — Hermes still
   never places orders; a separate, explicitly-gated executor would. Run the
   *Phase kickoff (planning-only)* prompt below against a freshly carved
   `docs/phases/phase-3.md` before any code.
3. **Standing hygiene.** Reviewers must run **`mypy .`** (whole-repo), not scoped
   mypy — scoped runs let test-layer type-debt accumulate (that was PR #68).

---

## Part 2 — The method (half-cycle, Layers 1–5)

We built this with the **half-cycle method**
(`~/development/myQ/content/02-Topics/half-cycle-method.md`). Five layers, each a
durable artifact, each gated before the next:

| Layer | Produces | Where it lives |
|---|---|---|
| **L1 — Product spec** | What we're building + invariants | `docs/product-spec.md`, `docs/invariants.md`, `docs/architecture-overview.md` |
| **L2 — Persistence** | Thin context map | `CLAUDE.md`, `docs/features/INDEX.md`, `docs/code-map.md` |
| **L3 — Phase scoping** | Shippable phases w/ strict-subset diagrams | `docs/phases/<phase>.md` |
| **L4 — Feature specs** | One spec per feature, audited for coherence | `docs/features/<feature>.md` |
| **L5 — Orchestrated execution** | Task graph → workers → review → merge | `docs/phases/<phase>-taskgraph.md` + PRs |

The runbooks that drive L3–L5 live in
`~/development/myQ/content/02-Topics/runbook-*.md`; the kickoff prompt and the
feature-spec template live alongside.

### The loop per phase (this is the rhythm to repeat)

```
plan specs (planning-only)  →  draft specs (L4)
   →  runbook-cross-spec-audit  (fresh auditor finds drift; lead fixes)
   →  runbook-taskgraph-generation  (carve into parallel-safe tasks)
   →  runbook-orchestration-kickoff (L5):
         for each task: gh issue → dispatch 1 worker in its own git worktree
            →  fresh read-only `reviewer` subagent per PR
            →  gh pr merge <N> --squash --delete-branch
            →  git reset --hard origin/main ; git worktree remove --force
   →  acceptance run against docs/phases/<phase>.md gate
   →  write docs/phases/<phase>-results.md
```

---

## Part 3 — Copy-paste prompts ("custom commands")

These are the exact prompt shapes we used. Treat each `<…>` as a fill-in. Run them
in order for a new project; jump to the *Phase kickoff* block for the next phase
of an existing one.

### 3.0 — Project kickoff (new project, blank dir)

```
/index
```
Then:
```
Read ~/development/myQ/content/00-System/Prompt-Library/claude-code-project-kickoff.md
and follow it for this new project.
```

### 3.1 — Persist the spec (Layer 1 + 2)

```
Persist the spec we just produced into:
- docs/product-spec.md
- docs/invariants.md            (cross-cutting decisions)
- docs/features/INDEX.md        (one-line summaries, empty for now)
- docs/architecture-overview.md (with a Mermaid container diagram)
And update CLAUDE.md to be a thin map (stack + pointers, not content).
```

### 3.2 — Carve phases (Layer 3)

```
Read docs/product-spec.md, docs/architecture-overview.md, and the Layer 3 section
of ~/development/myQ/content/02-Topics/half-cycle-method.md. Carve the product
into shippable phases. Apply the four-question diagnostic. Produce
docs/phases/poc.md (or phase-0.md) and stubs for phase-1.md, phase-2.md.
Each phase gets its own strict-subset architecture diagram.
```

### 3.3 — Phase kickoff (planning-only) — **use this for the next phase**

```
Phase <N> kickoff — planning only, no code yet.
Read: docs/phases/phase-<N>.md, the prior phase's results, docs/invariants.md,
docs/features/, the feature-spec-template, and half-cycle Layer 4.
Then propose but DO NOT yet write:
  (a) the feature-specs list, grouped + with dependencies
  (b) the code-map (area boundaries + safe-parallel rules)
  (c) the drafting order
Do NOT start writing specs yet. Plan first.
```

After approval: *"Yes — draft the specs."*

### 3.4 — Cross-spec audit (Layer 4 gate)

```
Read ~/development/myQ/content/02-Topics/runbook-cross-spec-audit.md and follow it
verbatim: a fresh, independent, read-only auditor over the Phase <N> specs vs
invariants.md / code-map.md / INDEX.md / phase-<N>.md / shipped contracts.
Report findings by severity; I (lead) apply fixes and annotate resolutions.
```

### 3.5 — Task graph (Layer 5 prep)

```
Read ~/development/myQ/content/02-Topics/runbook-taskgraph-generation.md and follow
it verbatim against docs/phases/phase-<N>.md and docs/features/.
```

### 3.6 — Orchestrate execution (Layer 5)

```
You are running ~/development/myQ/content/02-Topics/runbook-orchestration-kickoff.md
for Phase <N>. Start by reporting your dispatch plan (tasks, waves, worktrees),
then proceed: one worker per task in its own git worktree, a fresh read-only
`reviewer` per PR, merge ONLY via `gh pr merge <N> --squash --delete-branch`,
then realign main and remove the worktree. Complete the phase.
```

---

## Part 4 — The non-negotiable rules (how we kept it safe)

These are the operating constraints that the whole process leans on. They map to
`docs/invariants.md` (INV-01…INV-13). The ones that bite most often:

- **Merge only via `gh pr merge`.** Never `git push origin main` — the auto-mode
  classifier blocks it, and it bypasses review. Everything (even docs/scaffold)
  goes branch → PR → squash-merge → `git reset --hard origin/main`.
- **One worker per task, one worktree per worker.** Workers are
  `.claude/agents/fathom-worker-{sonnet,opus}.md`. Opus for invariant-heavy work
  (backtest engine, cost model — a silent look-ahead leak invalidates everything);
  Sonnet for scaffolding/boilerplate/strategy code.
- **A fresh reviewer per PR.** Read-only `reviewer` subagent, no memory of writing
  the code. It caught real bugs every phase (RSI bar-1 SHORT, warm-up divergence,
  streaming thread leak, calendar 404).
- **Acceptance gate is mandatory and has caught real bugs.** The runner-never-
  -fetched-candles bug and the O(n²) hang both surfaced only at the gate.
- **`mypy .` whole-repo, strict, as the merge gate** — not scoped. (PR #68 cleaned
  up the debt that scoped runs let through.)
- **INV-01 — Hermes never places orders.** Everything through Phase 2 produces a
  *watchlist of candidates only*. Crossing this line is a deliberate Phase 3
  decision with its own gate.
- **INV-13 — `Candidate` is the frozen wire contract** (14 flat fields). Every
  downstream Phase 2 feature builds against it; the INV-10 gate join is
  `signal.timeframe == approved_set.granularity`.
- **Secrets never committed.** `.env` is gitignored (INV-08). A token pasted in
  chat should be rotated — the transcript persists it.

---

## Part 5 — Gotchas we hit (so the next session doesn't)

- **Stale editable install.** Orchestration creates throwaway git worktrees; if
  `pip install -e .` ran inside one, the `.pth` finder keeps pointing at that
  (now-removed) path and subprocess-based integration tests fail with
  `ModuleNotFoundError: No module named 'backtest'`. Fix: `pip install -e .` from
  the canonical repo root. (This was the 9 `test_poc_runner` failures, not a code
  bug — see PR #68 notes.)
- **OANDA token rotation/expiry mid-run** → 401. Refresh `.env`, re-run the gate.
- **`matplotlib` line-data typing.** `Line2D.get_xdata()/get_ydata()` return a
  loose union mypy won't `float()`/index — use `np.asarray(..., dtype=float)`.
- **`**dict` unpack into non-uniform params** is rejected by strict mypy — pass
  keyword args explicitly instead.
- **Empty approved-set is a valid result, not a failure** (INV-10). PoC exited 0
  with 0/36 approved. Don't "fix" the gate to make signals appear.

---

## Reference index

- Invariants: [`docs/invariants.md`](invariants.md) (INV-01…INV-13)
- Architecture: [`docs/architecture-overview.md`](architecture-overview.md)
- Area boundaries + safe-parallel rules: [`docs/code-map.md`](code-map.md)
- Feature registry: [`docs/features/INDEX.md`](features/INDEX.md)
- Phase docs + results + taskgraphs + spec-audits: [`docs/phases/`](phases/)
- Worker roles: `.claude/agents/fathom-worker-{sonnet,opus}.md`
- Method + runbooks: `~/development/myQ/content/02-Topics/half-cycle-method.md`,
  `runbook-{taskgraph-generation,orchestration-kickoff,cross-spec-audit}.md`;
  kickoff prompt: `~/development/myQ/content/00-System/Prompt-Library/claude-code-project-kickoff.md`
