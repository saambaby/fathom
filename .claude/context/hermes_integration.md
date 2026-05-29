# Hermes integration context

## P2-T-04 — 2026-05-29 (feat/p2-t-04)

**What was done:**
- Created `hermes_integration/__init__.py` (package marker with module-level docstring).
- Created `hermes_integration/news_risk.py` with:
  - `NewsRiskVerdict` pydantic v2 model:
    - `event_risk: Literal["high","medium","low"]`
    - `reason: str`
    - `suggest_action: Literal["proceed","reduce_size","skip"]`
    - `model_config = {"extra": "forbid"}` — rejects unknown fields at validation time.
  - `_safe_default()` — private factory returning the INV-02 safe default
    `NewsRiskVerdict(event_risk="high", reason="unparseable response — defaulting to skip", suggest_action="skip")`.
  - `parse_news_risk(raw: str) -> NewsRiskVerdict` — INV-02 enforcement boundary:
    empty-string guard → `json.loads` → `isinstance(dict)` check → `NewsRiskVerdict.model_validate`.
    Any failure at any stage: `_log.warning(...)` + return `_safe_default()`. Never raises. Never returns `proceed` on failure.
- Created `hermes_integration/prompts/news_risk.md` — prompt template with `{{instrument}}`,
  `{{base_currency}}`, `{{quote_currency}}`, `{{direction}}`, `{{entry_window_utc}}`,
  `{{calendar_events}}` placeholders. Instructs Claude to output ONLY the JSON object
  matching the schema; no prose, no markdown fences. Bias table: high-impact ≤ 4h → skip;
  medium ≤ 1h or high 4–12h → reduce_size; ambiguous/uncertain → skip. No secrets (INV-08).
- Created `tests/test_news_risk.py` — 50 tests, zero live Claude/Anthropic calls:
  - `TestNewsRiskVerdictModel` (14 tests): valid construction, all enum values, rejects
    out-of-enum event_risk, out-of-enum suggest_action, missing fields, extra fields.
  - `TestParseNewsRiskWellFormed` (6 tests): proceed/skip/reduce_size verdicts, type check,
    all enum round-trips.
  - `TestParseNewsRiskMalformedInputs` (26 tests): invalid JSON, partial JSON, prose response,
    markdown-fenced JSON, empty string, whitespace, missing each required field, missing all
    fields, bad event_risk enum ("catastrophic", "extreme"), bad suggest_action enum
    ("trade", "allow", "go"), JSON array, null, bare string, number, boolean, null field
    values, extra field, ambiguous text. Plus `test_never_returns_proceed_on_failure` and
    `test_never_raises_on_any_input` (INV-02 invariant sweeps over 8+ inputs each).
  - `TestParseNewsRiskLogging` (4 tests): warning logged on bad JSON/empty/bad-enum;
    no secret token leaks in logs (INV-08).

**INV-02 design decision:**
- `parse_news_risk` has three layers of defence: (1) empty-string guard, (2) JSON parse
  try/except, (3) pydantic validate try/except + bare `except Exception` catch-all.
  Each layer independently returns the skip default and logs — no failure can propagate.
- `model_config = {"extra": "forbid"}` means a Claude response with extra fields
  (e.g. `"confidence": 0.9`) triggers a `ValidationError` → skip default. This is intentional:
  the wire contract is exactly `{event_risk, reason, suggest_action}`.

**No anthropic SDK dep** (D-P2-3 enforced — Hermes calls Claude; Fathom owns validation).
**pyproject.toml untouched** — no new dependencies. `hermes_integration` is importable via
the editable install path (worktree root on sys.path); setuptools `packages.find.include`
does not need updating for offline unit tests.

**AC verification results:**
- `pytest tests/test_news_risk.py -v` → 50 passed, exit 0
- `pytest -q` (full suite) → 473 passed, exit 0
- `mypy hermes_integration/` → "Success: no issues found in 2 source files", exit 0

**New dependency added to pyproject.toml?** NO (no new deps; pydantic already present).
**CLAUDE.md trigger-table check:** no new dep, no new CLI command — CLAUDE.md not edited.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)

---

## P2-T-05 — 2026-05-29 (feat/p2-t-05)

**What was done:**
- Created `hermes_integration/narration.py` with:
  - `fallback_narration(candidate: Candidate) -> str` — deterministic one-liner from the
    candidate's flat INV-13 fields (`strategy_name`, `direction`, `instrument`, `timeframe`,
    `oos_sharpe_mean`, `news_flag`). Always returns a non-empty string; never raises.
    Wraps the entire body in `try/except Exception` for an ultra-minimal last-resort path.
  - `should_use_fallback(claude_response: str) -> bool` — helper for callers: True if the
    Claude response is empty/whitespace-only or exceeds `_MAX_NARRATION_LENGTH` (280 chars).
- Created `hermes_integration/prompts/narration.md` — prompt template instructing Claude to
  return exactly one plain-English line (no JSON, no markdown), grounded only in the six
  supplied fields: `{{instrument}}`, `{{timeframe}}`, `{{strategy_name}}`, `{{direction}}`,
  `{{oos_sharpe_mean}}`, `{{news_flag}}`. No secrets (INV-08).
- Created `tests/test_narration.py` — 51 tests, zero live Claude/Anthropic calls:
  - `TestFallbackNarrationBasic` (8 tests): returns string, never empty, no newlines,
    contains instrument/timeframe/strategy/sharpe, reasonable length.
  - `TestFallbackNarrationDirections` (2 tests): LONG/SHORT both produce valid output.
  - `TestFallbackNarrationStrategies` (12 tests): all 6 shipped strategy prefixes × 2
    checks (non-empty, no newline).
  - `TestFallbackNarrationNewsFlag` (3 tests): news_flag False/True; True mentions news.
  - `TestFallbackNarrationNeverRaises` (6 tests): parametrised across all strategy/direction
    combos — no exception.
  - `TestShouldUseFallback` (8 tests): empty/whitespace/over-length → True; at-max/below →
    False; normal response → False.
  - `TestNarrationIsCosmetic` (5 tests): the critical NOT-INV-02 battery — empty/whitespace/
    over-long Claude response → fallback used, candidate rank unchanged (kept); valid response
    used directly; no veto/skip/suggest_action in fallback output.
  - `TestNoSecretsInNarration` (6 tests): INV-08 sweep over 6 secret patterns.
  - `TestFallbackNarrationLogging` (1 test): no WARNING logged on normal path.

**CRITICAL distinction from news_risk (NOT INV-02):**
- Narration is cosmetic presentation only — it does not feed any automated decision.
- An unusable Claude response → `should_use_fallback` returns True → caller uses
  `fallback_narration`; the **candidate is kept on the watchlist** (no veto, no drop).
- INV-02's safe-skip default must NOT be applied here. Documented explicitly in the module
  docstring so a future reader does not mis-apply the news-risk pattern to narration.
- `fallback_narration` never returns anything resembling a `suggest_action` verdict.

**No anthropic SDK dep** (D-P2-3 enforced).
**pyproject.toml untouched** — no new dependencies.
**CLAUDE.md trigger-table check:** no new dep, no new CLI command — CLAUDE.md not edited.

**AC verification results:**
- `pytest tests/test_narration.py -v` → 51 passed, exit 0
- `pytest -q` (full suite) → 547 passed, exit 0
- `mypy hermes_integration/` → "Success: no issues found in 3 source files", exit 0

**New dependency added to pyproject.toml?** NO (no new deps; pydantic already present).
**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass)

---

## P2-T-06 — 2026-05-29 (feat/p2-t-06)

**What was done:**
- Created `hermes_integration/jobs/daily.md` — the plain-English Hermes daily job
  definition with:
  - Trigger: weekday, post-NY-close (22:00 UTC default), cron `0 22 * * 1-5`.
  - 5 ordered step headings:
    1. `fathom scan` (stdout JSON directly — NOT `fathom watchlist`)
    2. Per-candidate Claude news-risk via `parse_news_risk` (INV-02):
       skip→veto / reduce_size→flag / proceed→keep
    3. `fathom chart <instrument>` per surviving candidate
    4. Claude narration via `should_use_fallback` + `fallback_narration` (cosmetic, NOT INV-02)
    5. Deliver ranked watchlist + charts to Discord via Hermes gateway
  - Failure modes table: empty watchlist → "no candidates today" (INV-10); malformed Claude
    → skip (INV-02); narration failure → fallback (candidate kept, not vetoed); chart failure
    → skip chart, candidate kept; Discord failure → retry per Hermes gateway.
  - Operator runbook: register job in Hermes, wire CLI as tool (scan/watchlist/chart ONLY —
    INV-01), connect Discord gateway + Anthropic key via .env (never committed — INV-08),
    dry-run verification steps, T-08 acceptance gate note.
  - Allowed tools table: `fathom scan`, `fathom watchlist`, `fathom chart`. No execute/orders/risk.
- Created `tests/test_hermes_job.py` — 42 lint assertions:
  - File existence.
  - Ordered step headings (anchored to `^### Step N` — avoids inline "go to Step 5" false matches).
  - Each step contains its expected content (scan/news-risk/chart/narration/deliver).
  - Allowed tools referenced (scan/watchlist/chart).
  - Forbidden tools absent: `fathom execute`, `fathom orders`, `fathom risk` (INV-01).
  - Hermes-never-places-orders statement present (INV-01).
  - INV-01 boundary in operator runbook section.
  - skip→veto / reduce_size→flag / proceed→keep mapping.
  - Empty-watchlist path + "no candidates today" message + exit 0 (INV-10).
  - Malformed-Claude → skip (INV-02); fallback_narration present; narration keeps candidate.
  - Operator runbook: registration, CLI-as-tool, Discord, Anthropic key, .env-never-committed.
  - No hardcoded secrets in file (INV-08).
  - scan stdout primary / watchlist as persisted-read accessor.

**INV-01 design decision:**
- `daily.md` allowed-tools table and operator runbook both explicitly name `scan`, `watchlist`,
  `chart` as the only permitted tools. The runbook states: "Never register any order, execute,
  or risk tool". `fathom execute`, `fathom orders`, `fathom risk` do not appear in the file.
- The lint test has 5 dedicated INV-01 assertions + an ordered-step content check.

**AMBIGUOUS-03 resolution confirmed:**
- Step 1 explicitly states: "Use `fathom scan`'s stdout directly — do NOT call `fathom watchlist`
  as the primary source." (`fathom watchlist` is the persisted-read accessor for re-reads / Phase 5.)

**AMBIGUOUS-01 resolution confirmed:**
- Step 2 notes that `fathom scan` has already applied the deterministic news gate (high-impact →
  dropped, medium → `news_flag: true`); the Claude news-risk step is the finer qualitative veto
  on survivors. Two layers explicitly described as non-contradictory.

**No anthropic SDK dep** (D-P2-3 enforced — Hermes calls Claude; Fathom owns parsers).
**pyproject.toml untouched** — configuration artefact only; no new dependencies.
**CLAUDE.md trigger-table check:** no new dep, no new CLI command — CLAUDE.md not edited.

**AC verification results:**
- `pytest tests/test_hermes_job.py -v` → 42 passed, exit 0
- `pytest -q` (full suite) → 667 passed, exit 0
- `mypy tests/test_hermes_job.py` → "Success: no issues found in 1 source file", exit 0
- `mypy hermes_integration/` → "Success: no issues found in 3 source files", exit 0
- Pre-existing mypy errors (87, in test_news_risk/test_ranker/test_charts from prior tasks) — not introduced by T-06.

**New dependency added to pyproject.toml?** NO.
**New CLI command?** NO.
**PR:** https://github.com/saambaby/fathom/pull/67
**Merge plan:** `gh pr merge 67 --squash --delete-branch` (lead action after reviewer pass)
