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
