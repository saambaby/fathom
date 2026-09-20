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

``news_risk_check`` is the in-process call on the shared adapter.  The
parser is unchanged (INV-02).  Offline testability: inject a stub via
``client``.  No client + no ``LLM_API_KEY`` → skip default, zero network.

INV-08: ``LLM_API_KEY`` is never logged.  The adapter holds it privately.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ai.llm_client import OpenAICompatClient, _ClientAdapter
from signals.ranker import Candidate

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


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_PROMPT_PATH = _PROMPTS_DIR / "news_risk.md"


def _split_instrument(instrument: str) -> tuple[str, str]:
    """EUR_USD → (EUR, USD).  Unknown shapes still fill both placeholders."""
    parts = instrument.split("_", 1)
    base = parts[0] if parts else instrument
    quote = parts[1] if len(parts) > 1 else ""
    return base, quote


def _build_prompt(
    candidate: Candidate,
    calendar_events: str,
    entry_window_utc: str,
) -> str:
    """Render ``prompts/news_risk.md`` with all six placeholders substituted."""
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    base, quote = _split_instrument(candidate.instrument)
    rendered = template.replace("{{instrument}}", candidate.instrument)
    rendered = rendered.replace("{{base_currency}}", base)
    rendered = rendered.replace("{{quote_currency}}", quote)
    rendered = rendered.replace("{{direction}}", candidate.direction)
    rendered = rendered.replace("{{entry_window_utc}}", entry_window_utc)
    rendered = rendered.replace("{{calendar_events}}", calendar_events)
    return rendered


# ---------------------------------------------------------------------------
# news_risk_check — public API (pretrade_check algorithm, INV-02 skip default)
# ---------------------------------------------------------------------------


def news_risk_check(
    candidate: Candidate,
    calendar_events: str,
    entry_window_utc: str,
    *,
    client: _ClientAdapter | None = None,
) -> NewsRiskVerdict:
    """Run the in-process news-risk LLM call for a ranked ``Candidate``.

    Algorithm (mirrors ``pretrade_check``):
        1. If no ``client`` is provided and ``LLM_API_KEY`` is not set,
           return the safe default ``skip`` immediately (offline-safe).
        2. If no ``client`` is provided and a key is available, build the live
           client via ``OpenAICompatClient.from_env()``.
        3. Build the prompt from ``prompts/news_risk.md`` (six placeholders).
        4. Call ``client.complete(prompt)`` — any transport exception is
           caught, logged at WARNING, and returns the safe default ``skip``.
        5. Route the raw response through ``parse_news_risk`` (INV-02).

    Args:
        candidate: The ranked ``Candidate`` under review.
        calendar_events: Caller-rendered calendar text (this module does not
            fetch the store).
        entry_window_utc: Caller-computed entry window string.
        client: Injectable adapter.  Pass a stub in tests.

    Returns:
        A ``NewsRiskVerdict``.  Always ``skip`` on any failure path (INV-02).
    """
    if client is None and not os.environ.get("LLM_API_KEY"):
        _log.warning(
            "news_risk_check: no client and LLM_API_KEY not set — "
            "returning safe default skip (INV-02 offline path)"
        )
        return _safe_default()

    active_client: _ClientAdapter
    if client is None:
        try:
            from_env_client = OpenAICompatClient.from_env()
            if from_env_client is None:
                raise ValueError("LLM_API_KEY not set")
            active_client = from_env_client
        except Exception as exc:  # noqa: BLE001 — client construction failure
            _log.warning(
                "news_risk_check: failed to initialise live client (%s: %s) — "
                "returning safe default (INV-02)",
                type(exc).__name__,
                exc,
            )
            return _safe_default()
    else:
        active_client = client

    try:
        prompt = _build_prompt(candidate, calendar_events, entry_window_utc)
    except Exception as exc:  # noqa: BLE001 — prompt template missing / IO error
        _log.warning(
            "news_risk_check: failed to build prompt (%s: %s) — "
            "returning safe default (INV-02)",
            type(exc).__name__,
            exc,
        )
        return _safe_default()

    try:
        raw = active_client.complete(prompt)
    except Exception as exc:  # noqa: BLE001 — SDK/network error → safe default
        _log.warning(
            "news_risk_check: API call failed (%s: %s) — "
            "returning safe default (INV-02)",
            type(exc).__name__,
            exc,
        )
        return _safe_default()

    return parse_news_risk(raw)
