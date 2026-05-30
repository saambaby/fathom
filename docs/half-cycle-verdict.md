# Verdict — Half-Cycle AI, as Tested by Building Fathom

A deep, honest assessment of the half-cycle method after running it end-to-end:
PoC + 5 phases, **79 merged PRs** (50 code/scaffold, 29 docs/chore), **16.3k lines
of source, 22.5k lines of tests, 7.7k lines of method/doc markdown**, across many
AI sessions with several context resets. This is a verdict on the *method*, with
Fathom as the case study — not a verdict on Fathom.

---

## TL;DR verdict

Half-cycle AI is a **strong construction method that is easy to mis-read as a
success method.** It is **highly efficient** at what it is actually good at —
building a large, multi-component, invariant-critical system across many AI
sessions with low defect-escape and near-total resumability — and **inefficient and
over-ceremonious** for small, well-understood, single-component changes. It has two
structural blind spots: **(1) correctness validation is back-loaded to the very last
layer** (everything before code is prose and can't run), and **(2) it validates how
well you built the thing, never whether the thing should exist.** For Fathom the
method *succeeded at its job* — a coherent, safe, reviewed trading system — and that
success is *orthogonal* to whether Fathom should ever trade live, which remains
entirely a human judgment at the INV-07 gate.

**Use it when correctness, coherence, and resumability across a fleet matter. Pair
it with something that pressure-tests the premise — which it deliberately doesn't.**

---

## The numbers (efficiency, measured)

| Metric | Value | Reading |
|---|---|---|
| Merged PRs | 79 (50 code, 29 docs/chore) | ~37% of PRs were pure process/coordination |
| Source LOC | 16,261 | the actual product |
| Test LOC | 22,513 | **1.38× the source** — heavy, mostly justified |
| Doc/method markdown LOC | 7,684 | **~0.47× the source** — pure method overhead, *excluding* agent audit/review time |
| Cross-spec audits run | 5 (Phases 1-5) | **blocking findings in 5/5** — specs were *never* clean on the first pass |
| Reviewer send-backs (this session's code PRs) | ~5 of ~19 (~26%) | every dangerous bug was caught at review, none at spec |
| Phases | 6 (PoC + 1-5) | fixed per-phase ceremony cost regardless of phase size |

Two facts jump out. First, **every single cross-spec audit found blocking issues** —
the spec layer was never trustworthy until the audit reconciled it with reality.
Second, **every dangerous bug was caught at the last layer (review), not at any
spec/audit layer** — the P&L misfire, the live-order leak holes, the null safety
test. The process caught them, but always at the end.

---

## Efficiency analysis — where it pays and where it doesn't

**Where it is genuinely efficient:**
- **Resumability across context loss.** This is the killer feature for AI-driven
  work. The build spanned many sessions and at least one full context compaction; I
  rebuilt state from `PLAYBOOK.md` + `invariants.md` + `code-map.md` + the phase
  results docs and resumed without losing the thread. The 7.7k lines of "overhead"
  markdown are what *made* a multi-session AI build tractable — they are the memory
  the model doesn't have.
- **Defect-escape.** Across 6 phases, mypy stayed clean and the suite stayed green;
  the bugs that mattered were caught before merge by fresh reviewers. For a
  money-touching system that is the right trade.
- **Architectural coherence.** Frozen-contract invariants (INV-13 `Candidate`, INV-14
  `Order/Fill/Position`) + "lock the prerequisite hub first" eliminated the
  downstream churn that normally plagues multi-component builds.

**Where it is inefficient:**
- **Fixed ceremony cost per phase.** Phase 5 shipped ~4 small code units but produced
  a carve doc + 3 specs + an audit report + a taskgraph + a results doc + status
  syncs + 4 PR reviews. The ceremony dwarfed the code. The method does not scale
  *down* — a 4-unit phase pays nearly the same process tax as a 10-unit phase.
- **The audit became a reconciliation step, not a coherence check.** Because specs
  encode an *imagined* API (see Root Cause A), the audit's real job each time was
  "make the spec match the shipped code," not "find inter-spec drift." Valuable, but
  not what the layer was sold as — and it means the spec-first sequencing *created*
  rework rather than saving it.

**Bottom line on efficiency:** high for *correctness-critical, multi-session,
multi-component* work; low for *small, well-understood slices*. The method has no
"express lane."

---

## What went wrong — root causes (not just symptoms)

**Root cause A — Specs precede ground truth.** Layer 4 specs are written before
touching code, so they encode an imagined system. Every audit found spec-vs-shipped
drift (a phantom `InstrumentMeta.pip_value`; a "Hermes gateway" that was never a code
object; `kill_switch_status` with no "armed" concept; a book-risk *sum* inlined
inside `check_limits`; `cmd_scan` that couldn't be imported without dragging in the
order path). There is no draft-time obligation to cite real symbols, so the spec is
untrustworthy until the audit fixes it. For a fast-feedback typed codebase, spec-first
*created* rework instead of saving it.

**Root cause B — Prose validation can't catch behaviour.** Layers 3-4 (carve, specs,
audit) are entirely prose. The first executable check is Layer 5 (the worker's tests
+ the reviewer). So *all* correctness bugs necessarily survive to the last layer —
and the most dangerous ones did: storing OANDA's *lifetime* P&L as "today's" (would
have misfired the kill switch), the live-gate "safe by accident" hole, the panel
INV-01 test that silently never ran, a `ZeroDivisionError` on negative equity. The
method front-loads *coordination* validation and back-loads *correctness* validation
to the very end — backwards for correctness-critical code.

**Root cause C — Local-phase optimization, global-reuse blindness.** Each phase's
specs are scoped to ship *that* phase and never model future read-only consumers, so
logic gets inlined and every later phase pays an "extraction tax": Phase 4 had to
extract `correlation.py`, `run_scan`, and `book_risk_sum` out of Phase 2/3 code;
Phase 5 had to extract `kill_switch_armed`. Four refactors of shipped, tested code
purely because the earlier phase buried reusable logic inside side-effectful
functions. Phase isolation (great for reviewability) is a weakness for reuse — the
method has no "design for extraction" convention.

**Root cause D — It validates the build, not the bet.** The deepest one. Every layer
asks "is this built correctly / coherently / per-invariant?" None asks "should this
exist / does the edge hold?" On Fathom the actual value question — does a thin
10/72-combo edge survive live costs — is untouched by all the process, yet the green
dashboard (79 PRs, 1179 tests, clean mypy, 7 tidy results docs) manufactures
done-ness. INV-07 (demo-first) is the only guard, and it is a human judgment the
method cannot close. The method can drive a project *confidently* toward shipping
something that should not ship. This is not an execution bug — it is an unadvertised
scope boundary: half-cycle is a **construction** methodology, not a
**validation-of-premise** methodology.

**Root cause E — Lead rulings are an unaudited single point of failure.** Decisions
routed to "the lead" (conflict policy, the INV-09 amendment, the attestation
auto-pass) land *in the spec* and become the thing reviewers check *against* — so a
wrong ruling is invisible to the review rigor. The clearest case: the attestation
auto-pass was signed off as "sound *if* the runbook enforces ordering" — a process
assumption no test enforces. The method protects against worker error, not against
architect error.

---

## What genuinely worked (this is a verdict, not a hit piece)

- **Fresh-reviewer-per-PR is the single highest-value practice.** It caught *every*
  dangerous bug listed above. A reviewer with no memory of writing the code, fed the
  diff + the spec's acceptance criteria, is worth more than any amount of pre-code
  prose.
- **Frozen-contract invariants + lock-the-hub-first** eliminated downstream churn —
  the `Candidate`/`Order` contracts held across every consumer.
- **Durable artifacts → resumability.** The reason a multi-session AI build didn't
  collapse into incoherence. This alone may justify the method for long-horizon
  agentic work.
- **Invariants as a safety spine.** INV-01 (no Hermes order authority), INV-04
  (brackets), INV-05 (0.25% cap), INV-07 (demo-first) held across all five phases —
  no naked order, no UI/agent order surface, nothing flipped live. For a money-
  touching system, that unbroken discipline is the method's strongest argument.
- **The cross-spec audit, even prose-only, caught real architecture gaps** (the
  non-existent gateway, the INV-09 violation) that would have been far more expensive
  to discover at code time.

---

## The verdict

Half-cycle AI **delivered exactly what it promises — a large, coherent, type-clean,
heavily-tested, invariant-respecting system, built incrementally by an AI fleet
across many sessions with low defect-escape — and it did so efficiently for a system
of this size and risk profile.** Its review and invariant machinery repeatedly
caught real, dangerous bugs.

Its failures are **structural and predictable**, not incidental:
1. correctness validation lives only at the final layer (prose can't run);
2. specs drift from shipped reality because nothing forces a draft-time code check;
3. it taxes reuse (no design-for-extraction);
4. and — most importantly — **it validates construction, never premise**, so it can
   build the wrong thing impeccably.

For Fathom, the method's success and Fathom's worth are *independent variables*. The
codebase is done and safe; whether it should ever trade real money is unknown and
sits entirely on the human at INV-07. **That is the method working as designed — and
also its most dangerous property, because the polish invites confidence the evidence
does not support.**

**Net: an excellent construction method, frequently mistaken for a success method.**

---

## Concrete improvements (if we run it again)

1. **"Specs must cite shipped symbols."** Draft-time rule: every spec references the
   real `file:symbol` for everything it builds on; the consistency-check verifies they
   exist. Kills Root Cause A (the single biggest source of rework).
2. **Pull one executable check earlier.** A "contract smoke" at spec time — a
   compiling type-stub / failing test the spec must be written against — so
   correctness isn't 100% back-loaded to Layer 5 (Root Cause B).
3. **Design-for-extraction convention.** Layer 4 names reusable primitives up front
   and ships them as standalone, side-effect-free functions, not inlined — paying the
   reuse cost once instead of as a tax every later phase (Root Cause C).
4. **A premise gate at Layer 1, re-checked each phase.** An explicit, revisited
   "should this exist / what evidence would kill it?" separate from the build gates
   (Root Cause D). For Fathom that would have surfaced the thin-edge risk as a
   first-class concern, not a footnote.
5. **Adversarial check on lead rulings.** A second agent argues the opposite of any
   "lead ruling" before it enters a spec (Root Cause E).
6. **Ops-reality notes in the orchestration runbook** — e.g. the editable-install /
   git-worktree hazard that forced sequential-in-main dispatch here.

---

## See also

- [`PLAYBOOK.md`](PLAYBOOK.md) — phase status + the reproducible method.
- [`operator-acceptance.md`](operator-acceptance.md) — the remaining human gates (the work the method left to judgment).
- Per-phase audit reports (`phases/phase-N-spec-audit-*.md`) — the raw evidence behind Root Causes A/B.
