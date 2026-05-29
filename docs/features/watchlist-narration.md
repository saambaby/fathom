# Feature: watchlist-narration

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Turn each ranked candidate into a one-line, human-readable rationale so the trader understands *why* a pair is on the list (e.g. "Donchian 20-bar breakout long on GBP/USD H4, OOS Sharpe 0.25, no high-impact news for 6h"). Claude writes the narration inside the Hermes daily session. Unlike [[news-risk-assessment]], narration is **cosmetic — it does not feed any automated decision**, so it is **not** governed by INV-02; a missing/malformed narration falls back to a deterministic template string rather than the safe-skip default.

## User-facing behaviour

Two Fathom artefacts:
- `hermes_integration/prompts/narration.md` — the prompt template Hermes fills with a candidate's facts (the flat `Candidate` fields per INV-13: `instrument, timeframe, strategy_name, direction, oos_sharpe_mean, news_flag` — *not* "expectancy") and asks for one concise plain-English line.
- `fallback_narration(candidate: Candidate) -> str` — a deterministic one-liner built from the candidate's own fields, used when Claude is unavailable or returns something unusable. Always produces a sensible line (never empty, never an exception).

## Acceptance criteria

- [ ] The prompt template instructs Claude to return exactly one line (no JSON, no markdown), grounded only in the supplied facts (no invented numbers).
- [ ] `fallback_narration` produces a clear one-line summary from the candidate's fields for any candidate (tested across LONG/SHORT, each strategy).
- [ ] An empty/whitespace/over-long Claude response → caller uses `fallback_narration` (graceful, cosmetic — NOT a safety veto; the candidate stays on the list).
- [ ] Narration never alters ranking or filtering — it is presentation only.
- [ ] No secret/token in the template or logs (INV-08).

## Component design

`hermes_integration/narration.py` for `fallback_narration` + the prompt in `prompts/narration.md`. Deliberately mirrors [[news-risk-assessment]]'s prompt+helper shape so the two are consistent — **but the failure semantics differ on purpose**: news-risk fails *safe* (skip, INV-02); narration fails *cosmetic* (template fallback, candidate unaffected). This distinction must be explicit so a future reader doesn't apply INV-02's veto-on-failure to narration.

## Non-goals

- No INV-02 safe-default veto — narration failure must NOT drop a candidate (that would let a cosmetic-layer hiccup silently shrink the watchlist).
- No ranking/scoring influence.
- No live Claude client in Fathom (Hermes invokes Claude; D-P2-3).

## Touches

- [INV-08] — no secrets in prompt/logs.
- *(Explicitly NOT INV-02)* — narration is cosmetic; documented here to prevent mis-application of the safe-default rule.

## Depends on

- [[signal-ranker]] — the `Candidate` being narrated.
- Hermes (configured) invokes the prompt; falls back to `fallback_narration` on failure.

## Approach

Write `fallback_narration` first (it's the guaranteed path) from the candidate's fields. Author the prompt to be tightly grounded (one line, only supplied facts) to avoid hallucinated levels. The Hermes job ([[hermes-job-definitions]]) calls Claude with the prompt and substitutes `fallback_narration` on any unusable result.

## Open questions

- Should narration mention the news-risk verdict ([[news-risk-assessment]]) inline (e.g. "⚠ high-impact USD news in 3h")? Lean: yes — fold the verdict's `reason` into the line when `event_risk != low`, since they're presented together. Decide in Plan.

## Out of scope

- News-risk *assessment*/veto ([[news-risk-assessment]]), the daily job wiring ([[hermes-job-definitions]]).
