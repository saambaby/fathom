# Feature: companion-core

**Status:** ready
**Phase:** phase-08
**Owner:** saambaby
**Last updated:** 2026-09-01

## Summary

The shared plumbing all four phase-08 companion commands (`review`, `journal`, `ask`,
and the deviation-log explainer folded into `review`) build on: one context-pack
builder + prompt-call shape + bounded/parsed response + deterministic offline fallback,
so `ai/companion.py` is a single reviewable pattern instead of four near-duplicate LLM
call sites. This spec ships no user-facing command itself — it is the `Depends on`
target of the other three phase-08 specs.

## User-facing behaviour

None directly. `ai/companion.py` exposes a library surface the other three specs call:

- `build_context_pack(kind, **data) -> ContextPack` — assembles a small, typed,
  serializable dict of store data plus a manifest of what was included/excluded
  (`ContextPack.sources: list[str]`, e.g. `["positions:3", "deviation_log:2"]`) so a
  refusal or a citation can point at what was actually available.
- `run_companion_call(prompt, *, response_model, client=None, fallback) -> T` — the
  one call-shape wrapper: builds the OpenAI chat-completions payload, calls
  `OpenAICompatClient` (imported from `ai/llm_client.py`, the same adapter
  `ai/pretrade_check.py` uses — see Grounded claims), parses the response
  against `response_model` (pydantic, `extra="forbid"`), and returns `fallback` — not
  an exception, not `None` — on any of: no `client` and no `LLM_API_KEY`, HTTP error,
  timeout, or parse/validation failure. Every failure path logs at WARNING (never logs
  `LLM_API_KEY` — INV-08) and returns the caller-supplied `fallback` instance so the
  command still prints something and exits 0.

## Acceptance criteria

1. `build_context_pack` accepts only store-read data structures (dicts/pydantic
   models already loaded by the caller) — it does not itself open a `Store` or an
   `OandaClient`, so it cannot acquire write authority by construction.
2. `run_companion_call` with `client=None` and `LLM_API_KEY` unset returns the given
   `fallback` unchanged, logs one WARNING, and raises nothing (mirrors
   `pretrade_check`'s `client is None and no key` branch,
   `hermes_integration/pretrade_check.py:386-391`).
3. `run_companion_call` with an injected stub client returning valid JSON for
   `response_model` returns the parsed, validated instance (offline-testable, no
   network, no key — same pattern as `pretrade_check`'s stub-client test).
4. `run_companion_call` with an injected stub client returning malformed/non-conforming
   JSON returns `fallback`, not a partially-populated model and not an exception.
5. An AST boundary test (extending the pattern in
   `tests/test_admin_panel.py:56-110`) asserts `ai/companion.py` contains no import of
   `execution.orders`, `execution.models.build_bracket`, `execution.reconcile`,
   `risk.sizing`, or `risk.limits`, and does not import `cli` — proving the module
   that all four companion commands share cannot reach order authority even
   transitively. Callers (`ai/review.py`, journal, ask) add their own AST files
   with at least this set.
6. `ContextPack.sources` is populated for every call (never empty when the pack has
   any data) so a downstream refusal (`ask-command`) can name what was actually
   grounded.

## Sequence diagram

Skip — see Artefact verdicts.

## Component design

`ai/companion.py` (new module in the `ai/` package created by phase-07's
`ai-package-migration`; this spec assumes that rename has landed and imports
`ai.llm_client.OpenAICompatClient` — per `ai-package-migration.md`'s Component
design, `OpenAICompatClient` is owned by `ai/llm_client.py` and `ai/pretrade_check.py`
itself imports it from there, not the reverse — rather than
`hermes_integration.pretrade_check.OpenAICompatClient`; see Grounded claims and
Depends on).

- `ContextPack` (pydantic, not frozen — it's an internal builder output, not a
  cross-module wire contract): `kind: str`, `data: dict[str, object]`,
  `sources: list[str]`, `generated_at: datetime` (UTC, INV-03).
- `run_companion_call(prompt: str, *, response_model: type[T], client: OpenAICompatClient | None, fallback: T) -> T`
  mirrors `pretrade_check()`'s three-branch shape (`hermes_integration/pretrade_check.py:355-420`):
  no-client-no-key → fallback; stub/live client → call → parse; parse failure →
  fallback. It is generic over `response_model` so `review-command`, `journal`, and
  `ask-command` each pass their own pydantic model instead of re-implementing the
  three branches.
- No caching, no retry — a single best-effort call per invocation (these are
  on-demand CLI commands, not automation; ADR-004).

## User flow

Skip — see Artefact verdicts.

## Artefact verdicts

- Sequence diagram: skip — one synchronous in-process call (prompt → LLM adapter →
  parse → fallback-or-value), same shape as the already-shipped `pretrade_check`
  sequence; no new actor, no cross-service coordination.
- Component design: include — this spec's entire purpose is pinning the shared
  call-shape three other specs depend on; the field/behaviour contract must be
  explicit so it doesn't drift per-caller.
- User flow: skip — no frontend surface; this is a backend library module only.

## Non-goals

- No command-specific prompt templates or response models — each of `review`,
  `journal`, `ask` defines its own `response_model` and prompt text.
- No caching, batching, or retry logic — one best-effort call per invocation.
- No new store tables — `ContextPack` is an in-memory assembly of data the caller
  already loaded.

## Touches

- INV-01 — `ai/companion.py` and every module built on it must not import order/risk
  placement code, directly or transitively (companion commands are read-only advisory
  text only, per phase-08's Purpose).
- INV-02 — applies the INV-02 discipline (structured parse, malformed/unparseable
  response never becomes a fabricated answer, always the safe `fallback`) by
  extension: companion commands are advisory-only and not yet covered by the
  invariant's letter, which scopes to outputs "feeding an automated decision" — see
  Notes' invariant-promotion candidate.
- INV-03 — `ContextPack.generated_at` and all context-pack timestamps are UTC
  RFC 3339.
- INV-08 — `LLM_API_KEY` is never logged, mirroring `OpenAICompatClient`'s existing
  guarantee (`hermes_integration/pretrade_check.py:24`).

## Events

- Written: none (no store table owned by this spec).
- Consumed: none directly — callers pass already-loaded store data into
  `build_context_pack`.

## Environment variables

| Var | Purpose | Arg type (build-arg / runtime) | Where set |
|---|---|---|---|
| `LLM_API_KEY` | Enables the live companion LLM call; unset → every companion command uses its deterministic fallback | runtime | `.env` (already defined by phase-06's adapter; reused, not introduced) |
| `LLM_BASE_URL` | OpenAI-compatible endpoint for the companion call | runtime | `.env` (existing) |
| `LLM_MODEL` | Model id for the companion call | runtime | `.env` (existing) |

No new env vars — this spec reuses the three already defined by `pretrade-check`
(`config/settings.py:90-99`).

## Wire-format contract

`run_companion_call` builds the same OpenAI chat-completions request shape
`OpenAICompatClient` already sends (`hermes_integration/pretrade_check.py:231-` —
`{"model": ..., "messages": [{"role": "user", "content": prompt}], ...}` POSTed to
`{LLM_BASE_URL}/chat/completions`) and reads `choices[0].message.content` as the raw
text to parse against `response_model`. Per-command request/response field shapes are
specified in `review-command.md`, `journal.md`, and `ask-command.md` respectively —
this spec pins only the transport envelope, not any command's payload fields.

## Depends on

- `ai.llm_client.OpenAICompatClient` (post-`ai-package-migration` name/location per
  `docs/features/ai-package-migration.md`'s Component design; currently
  `hermes_integration/pretrade_check.py:231-272` pre-migration) — reused, not
  reimplemented.
- phase-07's `ai-package-migration` spec landing first (the `hermes_integration/` →
  `ai/` rename, and specifically the `OpenAICompatClient` extraction into
  `ai/llm_client.py`) — cross-phase assumption, see below.

## Approach

Add `ai/companion.py` with `ContextPack` + `run_companion_call`, built directly on
top of the existing `OpenAICompatClient`. Unit-test the three branches
(no-key-fallback, stub-success, stub-malformed-fallback) exactly as
`tests/test_pretrade_check.py` already does for `pretrade_check`, then add the AST
boundary test as an extension of `tests/test_admin_panel.py`'s pattern, scoped to
`ai/companion.py` (and, once they exist, `review-command`/`journal`/`ask-command`'s
modules).

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| `pretrade_check`'s three-branch shape (no-client-no-key → block; client → call → parse; malformed → block) is the pattern to mirror | `hermes_integration/pretrade_check.py:355-420` (`pretrade_check` function body, incl. the `client is None and not os.environ.get("LLM_API_KEY")` branch at line 386) | Opened file, read function |
| `OpenAICompatClient` builds itself from `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` and never logs the key | `hermes_integration/pretrade_check.py:231-272` (class body + `from_env` env-based constructor at line 258) | Opened file, read class body |
| `Settings` already defines `llm_api_key`, `llm_base_url`, `llm_model` | `config/settings.py:90-99` | Opened file, read field definitions |
| The AST forbidden-import probe pattern this spec's boundary test extends | `tests/test_admin_panel.py:56-110` (`forbidden_imports`/`forbidden_names` sets + `ast.walk` over `Import`/`ImportFrom`/`Attribute`/`Name` nodes) | Opened file, read the walker |
| `ai/` package does not yet exist in the repo; `hermes_integration/` is still the live module path | `ls hermes_integration/` at repo root (worktree, 2026-09-01) shows `pretrade_check.py` etc.; no `ai/` directory present | Ran `ls` in the worktree during spec-time verification |
| phase-07 plans the `hermes_integration/` → `ai/` rename as its own scoped spec (`ai-package-migration`), landing before phase-08's commands need the module | `docs/phases/phase-07/phase.md` "Anticipated specs" table, row `ai-package-migration` | Opened file, read table |

## Constraint blast radius

- New constraint: the AST boundary test forbids `ai/companion.py` (and its callers)
  from importing `execution.orders`, `execution.models.build_bracket`,
  `execution.reconcile`, `risk.sizing`,
  `risk.limits`, or `cli`. What it protects: INV-01 — no companion command can gain
  transitive order authority. What it blocks (legitimate-looking but disallowed): a
  future companion feature that wants to show "what would sizing produce for this
  candidate" by calling `risk.sizing.size_position` directly — that must instead take
  a pre-computed sizing result as already-loaded context-pack data (mirroring how
  `panel/` is barred from `risk.sizing`'s placement paths per INV-01's Phase 4
  enforcement clause) rather than import the module itself.

## Smoke checklist hooks

- Run any one phase-08 command (once shipped) with `LLM_API_KEY` unset; confirm it
  prints the deterministic fallback text and exits 0 (proves `run_companion_call`'s
  safe-default path end-to-end, not just in unit tests).
- `pytest tests/ -k companion` green, including the AST boundary test.

## Open questions

- Should `run_companion_call` enforce a response byte-size cap before parsing (defense
  against a misbehaving/malicious OpenAI-compatible endpoint returning an enormous
  body)? Propose yes, a generous fixed cap (e.g. 32KB), enforced in
  `OpenAICompatClient` itself rather than duplicated per caller — flagged as an
  invariant-promotion candidate below rather than decided here, since it touches the
  already-shipped `pretrade_check` call path too.

## Out of scope

- Prompt templates and response models for any specific command — see
  `review-command.md`, `journal.md`, `ask-command.md`.
- Retry/backoff on transient LLM HTTP errors — single best-effort call, same posture
  as `pretrade_check`.

## Notes

**Cross-phase assumption — reconcile at drift radar:** this spec assumes phase-07's
`ai-package-migration` has landed and `hermes_integration/pretrade_check.py` is now
`ai/pretrade_check.py` with an unchanged `OpenAICompatClient` API surface (per the
phase-07 phase doc's scoping: "the INV-02 parse boundaries ... and safe defaults are
unchanged"). At spec-time-of-writing the code still lives at
`hermes_integration/pretrade_check.py` (verified above) — the module-path references
in this spec use the current, ground-truth path for anchors and the post-migration
`ai.` path for the module this spec adds, per the coordinator's cross-phase framing.
If phase-07 lands with a different `OpenAICompatClient` call signature than what's
anchored here, `run_companion_call`'s adapter call needs a matching update — flag at
the taskgraph stage, not assumed away here.

**INV-02 / INV-20:** the 2026-09-01 INV-02 scope note plus INV-20 already
classify advisory LLM sites (structured parse, `"analysis unavailable"` /
deterministic fallback, never skip). This spec follows that; it is not a new
promotion candidate.
