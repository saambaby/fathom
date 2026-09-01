# Feature: market-brief

**Status:** draft
**Phase:** phase-07
**Owner:** operator + Claude
**Last updated:** 2026-09-01

## Summary

The session-level AI layer of `fathom analyze`: one structured LLM call that returns a
**market brief** (regime read, event landmines, invalidators), a per-instrument **regime
tag**, and a **skip-the-day session verdict** ("is today a low-quality day for these
setups?"). All three are **advisory text/tags** — like narration and *unlike* news-risk,
they feed no automated decision: a failure degrades to "unavailable", never to a veto or
a dropped candidate. This spec owns the response models, the parse boundary, the new
prompt template, and the deterministic fallbacks; analyze-command consumes them.

## User-facing behaviour

Rendered by analyze-command's session block (this feature has no CLI surface of its own):

- **Brief** — a short paragraph plus two bullet lists: `landmines` (dated calendar risks
  in the trade window) and `invalidators` (what would make today's setups wrong).
- **Session verdict** — `normal` | `caution` | `stand_aside` with reasons. Advisory
  only: `stand_aside` prints prominently but vetoes nothing; the operator decides.
- **Regime tag** per watchlist instrument — `trending` | `ranging` | `high_vol` |
  `quiet` | `pre_event`, shown beside each candidate and persisted to
  `analysis_log.regime`.
- Offline / no key / malformed response: brief text "analysis unavailable", verdict
  `unavailable`, every regime `unavailable` — deterministic, non-empty, never raises.

## Acceptance criteria

1. `session_analysis(candidates_summary, calendar_events, market_stats, *,
   client=None)` with a stub client returning valid JSON yields parsed
   `SessionAnalysis` with all three parts; instruments missing from the model's
   `regimes` map get `unavailable` (partial responses tolerated per-instrument, not
   failed whole).
2. Fail-safe (NOT INV-02 veto semantics, explicitly): invalid JSON, missing field,
   out-of-enum value, transport error, or no client + no `LLM_API_KEY` → the full
   deterministic fallback (`verdict="unavailable"`, all regimes `unavailable`, fallback
   brief text); the function never raises and the caller can always render — proven for
   each failure class offline. A future reader must NOT add a skip/veto default here
   (narration-style guard note in the module docstring).
3. Prompt: `ai/prompts/session.md` placeholders (`{{candidates_summary}}`,
   `{{calendar_events}}`, `{{market_stats}}`, `{{utc_now}}`) all substituted; the
   template instructs JSON-only output matching the wire format below.
4. Strict validation: pydantic models with `extra="forbid"` and `Literal` enums; any
   validation failure routes to the fallback (single parse boundary,
   `parse_session_analysis`, same pattern as `parse_news_risk`).
5. Boundary: `ai/brief.py` imports no store, execution, risk, or signals module — its
   inputs are three pre-rendered strings; the boundary scan extends to it (INV-01
   posture; store access for stats lives in analyze-command's orchestration).
6. INV-08: no key in logs/repr on this path (key-hygiene test extended).

## Component design

- **`ai/brief.py`**
  - Models: `SessionAnalysis { brief: MarketBrief, session: SessionVerdict,
    regimes: dict[str, RegimeTag] }`; `MarketBrief { summary: str, landmines:
    list[str], invalidators: list[str] }`; `SessionVerdict { verdict:
    Literal["normal","caution","stand_aside","unavailable"], reasons: list[str] }`;
    `RegimeTag = Literal["trending","ranging","high_vol","quiet","pre_event",
    "unavailable"]`.
  - `parse_session_analysis(raw) -> SessionAnalysis` — fail-closed to the fallback
    (never raises); `session_analysis(...)` — build prompt → `client.complete` →
    parse; the `pretrade_check` 5-step algorithm with the *advisory* fallback instead
    of a veto.
  - One LLM call per analyze run (not per candidate) — regime for every instrument
    comes back in the single `regimes` map; call volume stays O(1) + O(n) news-risk.
- **Inputs are caller-rendered strings** (same boundary rule as `news_risk_check`):
  analyze-command computes `market_stats` deterministically from stored candles (per
  instrument: last close, ATR(14), position-in-N-bar-range, realized-vol vs 30-day
  median) and renders `candidates_summary` from the `Candidate` list. This spec pins the
  *placeholder names*; the stats formula details live in analyze-command's
  implementation and may grow without amending this contract.
- **`ai/prompts/session.md`** — new template (the one new prompt of phase-07); includes
  the JSON schema, the enum values verbatim, and an explicit "advisory only — you cannot
  block trades" framing to keep outputs calibrated.

## Artefact verdicts

- Sequence diagram: **skip** — one synchronous call inside analyze-command's already-
  diagrammed pipeline; no coordination of its own.
- Component design: **include** — the single-call/regimes-map decision and the
  strings-in boundary are the choices an implementer would otherwise improvise.
- User flow: **skip** — no surface of its own; analyze-command owns the terminal block.

## Non-goals

- No veto authority: `stand_aside` never removes a candidate; regime tags never alter
  rank or sizing. (The deliberate mirror of watchlist-narration's NOT-INV-02 stance.)
- No numeric forecasts, price targets, or entry/exit suggestions in any output field —
  the prompt forbids them; free-text fields are display-only.
- No per-candidate LLM calls — news-risk (per-candidate) stays in `ai/news_risk.py`.
- No persistence — analyze-command writes `analysis_log.regime`; the brief/verdict are
  print-only in phase-07 (journaling them is phase-08 territory).
- No market-data fetching — stats come from candles the store already holds.

## Touches

- INV-01 — advisory module, no order path; boundary-scanned (AC 5).
- INV-02 — **deliberately not applicable** to the verdict/brief (nothing here feeds an
  automated decision); the spec inherits the narration precedent and documents the
  asymmetry to prevent future misapplication (AC 2).
- INV-03 — `{{utc_now}}` and any rendered times are UTC RFC-3339.
- INV-08 — key hygiene (AC 6).

## Events

- Written: none (analyze-command persists the regime tag). — Consumed: none.

## Environment variables

| Var | Purpose | Arg type | Where set |
|---|---|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | the session-analysis call (existing adapter; no new vars) | runtime secret / runtime / runtime | operator `.env` |

## Wire-format contract

LLM response (JSON object, snake_case, strict):

```json
{
  "brief": {"summary": "…", "landmines": ["…"], "invalidators": ["…"]},
  "session": {"verdict": "normal|caution|stand_aside", "reasons": ["…"]},
  "regimes": {"EUR_USD": "trending|ranging|high_vol|quiet|pre_event", "…": "…"}
}
```

- `unavailable` is **never** a legal model output for `verdict`/regime values — it is
  reserved for the parser's fallback, so a stored `unavailable` always means "no valid
  analysis", never "the model said so" (`extra="forbid"`; out-of-enum → full fallback).
- Consumer contract: `analysis_log.regime` (analyze-command) stores `RegimeTag` values
  verbatim — the enum here is the single source for that column's domain.

## Depends on

- ai-package-migration — `ai/llm_client.py` adapter + the package this module lives in.
- analyze-command (forward, mutual) — sole caller; renders the three input strings and
  persists regimes. The two specs pin the same placeholder names and `RegimeTag` enum.

## Approach

1. Models + `parse_session_analysis` (TDD: valid, partial-regimes, each failure class).
2. `session_analysis` + prompt template (TDD with stub client; placeholder substitution).
3. Boundary + key-hygiene test extensions.

## Grounded claims

| Claim | Anchor | Verified how |
|---|---|---|
| The advisory-fallback precedent (cosmetic layer must not inherit INV-02's veto) is established | hermes_integration/narration.py:9-21 | read the CRITICAL-distinction docstring |
| The 5-step call algorithm to reuse | hermes_integration/pretrade_check.py:355-436 | read function |
| Candles + ATR inputs are computable from the store | data/store.py:527 (`load_candles`) | signature exists; stats formulas are analyze-command implementation detail |
| CalendarEvent carries currency/time/impact/name for the landmines rendering | data/calendar.py:91-103 | read dataclass fields |
| `analysis_log.regime` column expects this enum + `unavailable` | docs/features/analyze-command.md (wire-format table) | same-sprint spec, authored consistently |

## Depends-on note for reviewers

`docs/features/analyze-command.md` is a same-sprint draft, not shipped code — the
`analysis_log` claim above is a cross-spec agreement, not a code anchor, and the drift
radar checks it.

## Smoke checklist hooks

- Within the `fathom analyze` acceptance run: session block shows brief + verdict +
  reasons; each candidate line carries a regime tag; offline rerun shows `unavailable`
  everywhere without error.

## Open questions

1. Should `stand_aside` also stamp a marker into the Pine status cell? Deferred —
  cosmetic; decide during the analyze acceptance walk (pine's status cell contract
  already exists and would take one extra flag).

## Out of scope

- Persisting brief text; historical brief comparison ("what changed since yesterday") —
  phase-08 journal/companion territory.
- Any change to news-risk semantics or prompts.

## Notes

Fourth spec of the sprint. The `RegimeTag` enum is shared vocabulary with
analyze-command's `analysis_log` — flagged for the sprint drift radar.
