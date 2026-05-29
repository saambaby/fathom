"""News-risk assessment — INV-02 enforcement boundary.

Owns the Fathom-side contract for Claude's per-pair news/event risk verdict:

  * ``NewsRiskVerdict`` (pydantic v2) — typed, enum-validated response model.
  * ``parse_news_risk(raw)`` — the INV-02 safe-default boundary.  Any failure
    (invalid JSON, missing field, out-of-enum value, empty string, wrong type)
    returns the skip default and logs.  It **never raises**, and it **never
    returns** ``suggest_action="proceed"`` on a parse/validation failure.

The asymmetry (INV-02):
    A false ``skip`` costs an opportunity; a false ``proceed`` costs money.
    The parser is therefore the safe boundary — wrap everything in
    try/except → skip default, not raise.

No ``anthropic`` SDK dependency (D-P2-3): Claude is invoked Hermes-side.
This module is fully unit-testable offline.

INV-08: No secrets, tokens, or API keys are referenced here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safe default (INV-02) — returned whenever parse_news_risk fails for ANY reason
# ---------------------------------------------------------------------------

_SAFE_DEFAULT_EVENT_RISK: Literal["high"] = "high"
_SAFE_DEFAULT_REASON: str = "unparseable response — defaulting to skip"
_SAFE_DEFAULT_ACTION: Literal["skip"] = "skip"


# ---------------------------------------------------------------------------
# NewsRiskVerdict pydantic model
# ---------------------------------------------------------------------------


class NewsRiskVerdict(BaseModel):
    """Structured news/event-risk verdict returned by Claude (via Hermes).

    Wire format (snake_case, exact enum spellings):
        ``{"event_risk": "high"|"medium"|"low", "reason": "...",
           "suggest_action": "proceed"|"reduce_size"|"skip"}``

    Pydantic v2 strict enum validation rejects any value outside the declared
    literals at construction time, satisfying the INV-02 strict-enum requirement.

    Attributes:
        event_risk: Assessed risk level for the upcoming event window.
        reason: Human-readable explanation from Claude.
        suggest_action: Recommended pipeline action:
            - ``"proceed"`` — no change to candidacy.
            - ``"reduce_size"`` — flag for Phase 3 sizing; candidate kept.
            - ``"skip"`` — veto the candidate entirely.
    """

    event_risk: Literal["high", "medium", "low"]
    reason: str
    suggest_action: Literal["proceed", "reduce_size", "skip"]

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Safe-default factory
# ---------------------------------------------------------------------------


def _safe_default() -> NewsRiskVerdict:
    """Return the INV-02 safe default verdict (skip, high-risk)."""
    return NewsRiskVerdict(
        event_risk=_SAFE_DEFAULT_EVENT_RISK,
        reason=_SAFE_DEFAULT_REASON,
        suggest_action=_SAFE_DEFAULT_ACTION,
    )


# ---------------------------------------------------------------------------
# parse_news_risk — INV-02 enforcement boundary
# ---------------------------------------------------------------------------


def parse_news_risk(raw: str) -> NewsRiskVerdict:
    """Parse Claude's JSON response into a ``NewsRiskVerdict``.

    This is the INV-02 enforcement boundary.  **On ANY failure** — invalid
    JSON, missing required field, out-of-enum value, empty string, wrong
    JSON type — returns the safe default
    ``NewsRiskVerdict(event_risk="high", reason="unparseable response — defaulting to skip",
    suggest_action="skip")`` and logs at WARNING level.  It **never raises**
    and **never returns** ``suggest_action="proceed"`` on a failure path.

    Args:
        raw: The raw string returned by Claude (expected to be a JSON object).

    Returns:
        A validated ``NewsRiskVerdict``.  On any failure, the safe-default
        ``skip`` verdict is returned instead.

    Examples:
        >>> parse_news_risk('{"event_risk":"low","reason":"quiet week","suggest_action":"proceed"}')
        NewsRiskVerdict(event_risk='low', reason='quiet week', suggest_action='proceed')

        >>> parse_news_risk("this is not json")
        NewsRiskVerdict(event_risk='high', reason='unparseable response — defaulting to skip', suggest_action='skip')
    """
    # Guard: reject empty / whitespace-only input immediately.
    if not raw or not raw.strip():
        _log.warning(
            "parse_news_risk: empty response — returning safe default (INV-02)"
        )
        return _safe_default()

    try:
        data: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        _log.warning(
            "parse_news_risk: JSON decode failed (%s) — returning safe default (INV-02)",
            exc,
        )
        return _safe_default()

    # json.loads can return non-dict values (e.g. a bare string, list, or null).
    if not isinstance(data, dict):
        _log.warning(
            "parse_news_risk: expected a JSON object, got %s — returning safe default (INV-02)",
            type(data).__name__,
        )
        return _safe_default()

    try:
        verdict = NewsRiskVerdict.model_validate(data)
    except ValidationError as exc:
        _log.warning(
            "parse_news_risk: pydantic validation failed (%s) — returning safe default (INV-02)",
            exc,
        )
        return _safe_default()
    except Exception as exc:  # noqa: BLE001 — catch-all for absolute safety (INV-02)
        _log.warning(
            "parse_news_risk: unexpected error (%s: %s) — returning safe default (INV-02)",
            type(exc).__name__,
            exc,
        )
        return _safe_default()

    return verdict
