# Feature: pretrade-check

**Status.** draft
**Phase.** Phase 3
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The final structured Claude veto, run in-process immediately before order
submission. Given an approved `Candidate`, it asks Claude for a `{decision, reason}`
verdict; a `block` (or any malformed/unavailable response) **aborts the trade**.
This is the second Claude boundary in Fathom (the first is Phase 2's Hermes-side
news-risk), and the **first in-process `anthropic` SDK call**. Per INV-02 it is a
hard structured boundary that fails safe to abort — it can only subtract a trade,
never authorise one.

## User-facing behaviour

Backend module `hermes_integration/pretrade_check.py`:

- `PretradeVerdict` (pydantic) — `{decision: "proceed"|"block", reason: str}`.
- `parse_pretrade_verdict(raw: str) -> PretradeVerdict` — the INV-02 boundary: any
  parse/validation/enum failure or empty input → `decision="block"` (safe default),
  logs at WARNING, **never raises**, **never returns `proceed`** on a failure path.
- `pretrade_check(candidate, *, client=None) -> PretradeVerdict` — builds the prompt
  from `prompts/pretrade.md`, calls Claude via an injectable `client` adapter, and
  routes the raw response through `parse_pretrade_verdict`. With `client=None` and no
  key configured, it returns the safe-default `block` (so the gate is testable and
  safe offline). The live `anthropic` call lives behind the adapter.

The execution CLI treats `proceed` as the only value that lets the trade continue.

## Acceptance criteria

- [ ] `parse_pretrade_verdict` returns `block` on: invalid JSON, missing field, out-of-enum `decision`, empty/whitespace input, wrong JSON type — exercised per case; it never raises and never returns `proceed` on failure (INV-02).
- [ ] A valid `{"decision":"proceed","reason":"..."}` parses to `proceed`; a valid `block` parses to `block`.
- [ ] `pretrade_check` with an injected stub client returning a fixed payload routes through the parser and returns the corresponding verdict (offline-testable, no key).
- [ ] With no client and no `ANTHROPIC_API_KEY`, `pretrade_check` returns the safe default `block` (fails safe; does not crash the gate).
- [ ] The live adapter, when a key is present, calls the `anthropic` SDK and returns a typed model; no secret is logged (INV-08).
- [ ] No order, execution, or risk function is importable/callable from this module — it returns a verdict only.

## Sequence diagram

```mermaid
sequenceDiagram
    participant CLI as fathom execute
    participant PT as pretrade_check
    participant CL as Anthropic API (Claude)
    participant P as parse_pretrade_verdict

    CLI->>PT: pretrade_check(candidate)
    PT->>CL: structured prompt (via adapter)
    alt response received
        CL-->>PT: raw text/JSON
        PT->>P: parse_pretrade_verdict(raw)
        P-->>PT: proceed | block (block on any failure)
    else unavailable / error / no key
        PT->>P: parse_pretrade_verdict("")
        P-->>PT: block (safe default)
    end
    PT-->>CLI: PretradeVerdict
    Note over CLI: proceed → continue to sizing; block → abort
```

## Component design

Mirrors Phase 2's `news_risk.py` structure (parser as the safe boundary; model with
`extra="forbid"`; `_safe_default()` factory) so the two Claude boundaries are
consistent and reviewable side-by-side. The `anthropic` SDK import is isolated in a
thin adapter (`_LiveClient`) injected into `pretrade_check`; tests inject a stub.
Adds the `anthropic` dependency (coordinator-branch edit to `pyproject.toml` +
`CLAUDE.md` before this task — like Phase 2's matplotlib edit).

## Non-goals

- No sizing/limits/orders — verdict only; the CLI enforces the abort.
- No Hermes involvement — this is the deterministic in-process path, distinct from the Hermes-side news-risk (INV-01).

## Touches

- [INV-02] — structured JSON, validated, malformed → safe default (abort).
- [INV-08] — no key/secret logged; key from `.env`.

## Depends on

- `Candidate` (INV-13), `anthropic` SDK (new dep, coordinator edit). Prompt template `hermes_integration/prompts/pretrade.md` (this spec adds it).

## Approach

`hermes_integration/pretrade_check.py` + `prompts/pretrade.md`. Build the parser
and stub-client path first (full offline test coverage), then the live adapter. The
real `ANTHROPIC_API_KEY` is wired only at the Phase 3 acceptance gate.

## Open questions

- Model + token budget for the call — propose a small, fast Claude model; confirm.
- Does the prompt receive the chart, or text-only candidate facts? Propose
  text-only for Phase 3 (cheaper, deterministic); revisit.

## Out of scope

- The deterministic risk gate ([[position-sizing]], [[risk-limits-kill-switch]]), submission ([[order-placement]]).
