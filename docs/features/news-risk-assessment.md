# Feature: news-risk-assessment

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The Claude layer that assesses per-pair news/event risk for a watchlist candidate and returns a **structured, validated verdict** that can down-rank or veto the candidate. This is the first Claude output in the production decision path, so it is governed by **INV-02**: the response is a pydantic-validated `{event_risk, reason, suggest_action}` object, and any malformed / low-confidence / unparseable response defaults to the safe action (`skip`) — never "trade anyway." The Claude call itself runs inside the Hermes daily session (Hermes is configured, not coded — D-P2-3); **this feature owns the Fathom-side contract**: the prompt template, the pydantic response model, and the parse-and-default validator — all unit-testable without a live Claude call.

## User-facing behaviour

Two Fathom artefacts + one contract:
- `hermes_integration/prompts/news_risk.md` — the prompt template Hermes fills per candidate (given the pair's two currencies, the upcoming `CalendarEvent`s, and recent headlines).
- `NewsRiskVerdict` (pydantic): `event_risk: Literal["high","medium","low"]`, `reason: str`, `suggest_action: Literal["proceed","reduce_size","skip"]`.
- `parse_news_risk(raw: str) -> NewsRiskVerdict` — parses Claude's JSON; **on any failure (malformed JSON, missing field, invalid enum, empty) returns the safe default `NewsRiskVerdict(event_risk="high", reason="unparseable response — defaulting to skip", suggest_action="skip")`** and logs (INV-02).

## Acceptance criteria

- [ ] `NewsRiskVerdict` validates the three fields with strict enums; rejects out-of-enum values.
- [ ] `parse_news_risk` returns a valid verdict for well-formed Claude JSON.
- [ ] **Malformed input → safe default (`suggest_action="skip"`), never an exception that aborts the run, never "proceed"** (INV-02). Tested with: invalid JSON, missing field, wrong enum value, empty string, and a low-confidence/ambiguous response.
- [ ] The prompt template instructs Claude to emit *only* the JSON object matching the schema (no prose), and to default to higher risk when uncertain.
- [ ] No secret/token in the template or logs (INV-08).
- [ ] The verdict maps to a ranking effect: `skip` ⇒ veto the candidate; `reduce_size` ⇒ flag (Phase 3 sizing consumes it); `proceed` ⇒ no change. (The application of this mapping lives in the Hermes job [[hermes-job-definitions]]; this spec defines the contract + helper.)

## Component design

`hermes_integration/` new package. `NewsRiskVerdict` + `parse_news_risk` in e.g. `hermes_integration/news_risk.py`; the prompt in `prompts/news_risk.md`. The parser is the INV-02 enforcement point — a single function with a try/except-everything fallback to the skip default. **No `anthropic` SDK dependency is added in Phase 2** (D-P2-3): the live model call is Hermes-side; Fathom supplies the prompt + validates whatever string comes back. This keeps C1 fully unit-testable offline.

**Wire-format contract:** `{event_risk: "high"|"medium"|"low", reason: string, suggest_action: "proceed"|"reduce_size"|"skip"}` — snake_case, exact enum spellings. Pin here so the prompt, the model, and the Hermes job agree.

## Non-goals

- No live Claude/Anthropic API client in Fathom (Hermes invokes Claude; D-P2-3).
- No calendar fetching (consumes [[economic-calendar]] output, which Hermes passes into the prompt).
- No narration (that is [[watchlist-narration]] — cosmetic, not INV-02-governed).

## Touches

- [INV-02] — **this is the canonical INV-02 feature**: structured JSON, validated, malformed → safe default (`skip`).
- [INV-01] — the verdict can only down-rank/veto a *suggestion*; it never reaches the (non-existent in Phase 2) execution path.
- [INV-08] — no secrets in prompt/logs.

## Depends on

- `data/calendar.py` (`CalendarEvent`, `Impact`) — the events the prompt reasons over (shipped).
- Hermes (configured, not coded) invokes the prompt and feeds the raw response to `parse_news_risk`.

## Approach

Define the schema first (it's the contract). Write the parser as a defensive boundary: attempt `json.loads` → pydantic validate; on *any* exception or validation error, log and return the skip default. Author the prompt to demand schema-only output and to bias toward `skip`/`reduce_size` under uncertainty (a false skip costs an opportunity; a false proceed costs money — INV-02's asymmetry). Unit-test the parser exhaustively with malformed inputs.

## Open questions

- **D-P2-3 — RESOLVED (recommended, overridable):** Claude call runs Hermes-side; Fathom owns model+validator+prompt, no `anthropic` dep. (If a deterministic pre-trade check is later wanted in-pipeline — Phase 3 — that's when the SDK enters.)
- Headlines source: the prompt references "recent headlines" — Phase 2's calendar feed has no headlines (deferred). For now the prompt reasons over calendar events only; headlines are a later enrichment.

## Out of scope

- The daily job wiring ([[hermes-job-definitions]]), narration ([[watchlist-narration]]), the deterministic calendar news-gate (that's in [[signal-ranker]]).
