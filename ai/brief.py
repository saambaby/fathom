"""Session-level market brief — advisory analysis (NOT INV-02).

Owns the Fathom-side contract for one structured LLM call that returns a
market brief, a skip-the-day session verdict, and per-instrument regime tags.

**CRITICAL distinction from news_risk (NOT INV-02):**
    The brief, session verdict, and regime tags are *advisory* — presentation
    and operator context only.  They do not feed any automated decision
    (filtering, ranking, sizing, or order submission).  Therefore:

    - Invalid JSON, a missing field, an out-of-enum value (including a raw
      ``"unavailable"``), a transport error, or no client + no ``LLM_API_KEY``
      → the caller uses the deterministic fallback
      (``verdict="unavailable"``, every regime ``"unavailable"``, brief text
      ``"analysis unavailable"``); **candidates are kept**.
    - There is **no safe-skip veto here**.  INV-02's "fail safe = drop the
      candidate" rule applies *only* to outputs that feed automated decisions
      (e.g. news-risk verdicts).  Applying INV-02's veto to this layer would
      let an advisory hiccup silently shrink the watchlist — exactly what
      the spec prohibits.
    - ``stand_aside`` prints prominently but **never vetoes**.  The operator
      decides.
    - A future reader must NOT add a skip/veto default here.  If analysis
      fails, mint ``"analysis unavailable"`` and move on.

Wire enums exclude ``unavailable``; only fallback constructors and the
missing-instrument fill mint it.  A stored ``unavailable`` always means
"no valid analysis", never "the model said so".

INV-01: this module takes three pre-rendered strings.  It imports no store,
    execution, risk, or signals module.
INV-03: ``{{utc_now}}`` is minted here as UTC RFC-3339, not a caller input.
INV-08: ``LLM_API_KEY`` is never logged.  The shared ``OpenAICompatClient``
    holds it privately and excludes it from ``repr``.
INV-20: one adapter (imported from ``pretrade_check``); no client + no key
    means zero network I/O.

Offline testability: inject a stub via the ``client`` parameter.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, ValidationError

# Current path (T3 has not renamed yet). Importing here works until the
# package move; T3 must not touch this file.
from hermes_integration.pretrade_check import OpenAICompatClient

_log = logging.getLogger(__name__)

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_PROMPT_PATH = _PROMPTS_DIR / "session.md"

_FALLBACK_BRIEF_TEXT: str = "analysis unavailable"

_INSTRUMENT_RE = re.compile(r"\b[A-Z]{3}_[A-Z]{3}\b")

WireRegimeTag = Literal["trending", "ranging", "high_vol", "quiet", "pre_event"]
RegimeTag = Literal[
    "trending", "ranging", "high_vol", "quiet", "pre_event", "unavailable"
]
WireSessionVerdictLiteral = Literal["normal", "caution", "stand_aside"]
SessionVerdictLiteral = Literal["normal", "caution", "stand_aside", "unavailable"]


class MarketBrief(BaseModel):
    """Display-only brief: summary paragraph plus landmines and invalidators."""

    summary: str
    landmines: list[str]
    invalidators: list[str]

    model_config = {"extra": "forbid"}


class _WireSessionVerdict(BaseModel):
    """Wire verdict — ``unavailable`` is out-of-enum and fails validation."""

    verdict: WireSessionVerdictLiteral
    reasons: list[str]

    model_config = {"extra": "forbid"}


class _WireSessionAnalysis(BaseModel):
    """LLM JSON object.  Regime values exclude ``unavailable``."""

    brief: MarketBrief
    session: _WireSessionVerdict
    regimes: dict[str, WireRegimeTag]

    model_config = {"extra": "forbid"}


class SessionVerdict(BaseModel):
    """Returned session verdict; ``unavailable`` is minted only by fallbacks."""

    verdict: SessionVerdictLiteral
    reasons: list[str]

    model_config = {"extra": "forbid"}


class SessionAnalysis(BaseModel):
    """Parsed session analysis returned to analyze-command."""

    brief: MarketBrief
    session: SessionVerdict
    regimes: dict[str, RegimeTag]

    model_config = {"extra": "forbid"}


class _ClientAdapter(Protocol):
    def complete(self, prompt: str) -> str:
        ...


def _utc_now_rfc3339() -> str:
    """UTC RFC-3339 timestamp with ``Z`` suffix (INV-03)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_instruments(*texts: str) -> list[str]:
    """OANDA-style ``XXX_YYY`` codes, first-seen order, from caller strings."""
    seen: list[str] = []
    found: set[str] = set()
    for text in texts:
        for match in _INSTRUMENT_RE.findall(text):
            if match not in found:
                found.add(match)
                seen.append(match)
    return seen


def _fallback(instruments: Sequence[str] = ()) -> SessionAnalysis:
    """Deterministic advisory fallback — never a skip/veto (NOT INV-02)."""
    return SessionAnalysis(
        brief=MarketBrief(
            summary=_FALLBACK_BRIEF_TEXT,
            landmines=[],
            invalidators=[],
        ),
        session=SessionVerdict(
            verdict="unavailable",
            reasons=[_FALLBACK_BRIEF_TEXT],
        ),
        regimes={inst: "unavailable" for inst in instruments},
    )


def parse_session_analysis(
    raw: str,
    *,
    instruments: Sequence[str] = (),
) -> SessionAnalysis:
    """Parse the LLM JSON into ``SessionAnalysis``.

    Fail-closed to the advisory fallback (never raises).  A raw
    ``"unavailable"`` is out-of-enum on the wire models and takes this path.
    Instruments missing from a valid ``regimes`` map are filled with
    ``unavailable``; superfluous keys are ignored.
    """
    expected = list(instruments)

    if not raw or not raw.strip():
        _log.warning(
            "parse_session_analysis: empty response — returning advisory fallback"
        )
        return _fallback(expected)

    try:
        data: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        _log.warning(
            "parse_session_analysis: JSON decode failed (%s) — "
            "returning advisory fallback",
            type(exc).__name__,
        )
        return _fallback(expected)

    if not isinstance(data, dict):
        _log.warning(
            "parse_session_analysis: expected a JSON object, got %s — "
            "returning advisory fallback",
            type(data).__name__,
        )
        return _fallback(expected)

    try:
        wire = _WireSessionAnalysis.model_validate(data)
    except ValidationError as exc:
        _log.warning(
            "parse_session_analysis: pydantic validation failed (%s) — "
            "returning advisory fallback",
            type(exc).__name__,
        )
        return _fallback(expected)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "parse_session_analysis: unexpected error (%s) — "
            "returning advisory fallback",
            type(exc).__name__,
        )
        return _fallback(expected)

    regimes: dict[str, RegimeTag]
    if expected:
        regimes = {inst: "unavailable" for inst in expected}
        for inst, tag in wire.regimes.items():
            if inst in regimes:
                regimes[inst] = tag
    else:
        regimes = dict(wire.regimes)

    return SessionAnalysis(
        brief=wire.brief,
        session=SessionVerdict(
            verdict=wire.session.verdict,
            reasons=wire.session.reasons,
        ),
        regimes=regimes,
    )


def _build_prompt(
    candidates_summary: str,
    calendar_events: str,
    market_stats: str,
    utc_now: str,
) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    rendered = template.replace("{{candidates_summary}}", candidates_summary)
    rendered = rendered.replace("{{calendar_events}}", calendar_events)
    rendered = rendered.replace("{{market_stats}}", market_stats)
    rendered = rendered.replace("{{utc_now}}", utc_now)
    return rendered


def session_analysis(
    candidates_summary: str,
    calendar_events: str,
    market_stats: str,
    *,
    client: _ClientAdapter | None = None,
) -> SessionAnalysis:
    """One advisory LLM call for the session brief, verdict, and regimes.

    Algorithm (pretrade_check 5-step, advisory fallback instead of a veto):
        1. No ``client`` and no ``LLM_API_KEY`` → fallback immediately
           (zero network I/O).
        2. No ``client`` but a key is set → ``OpenAICompatClient.from_env()``.
        3. Build the prompt from ``ai/prompts/session.md``; mint ``{{utc_now}}``.
        4. ``client.complete(prompt)`` — any exception → fallback.
        5. ``parse_session_analysis`` (advisory parse boundary).
    """
    instruments = _extract_instruments(candidates_summary, market_stats)

    if client is None and not os.environ.get("LLM_API_KEY"):
        _log.warning(
            "session_analysis: no client and LLM_API_KEY not set — "
            "returning advisory fallback (offline path)"
        )
        return _fallback(instruments)

    active_client: _ClientAdapter
    if client is None:
        try:
            from_env_client: Optional[OpenAICompatClient] = (
                OpenAICompatClient.from_env()
            )
            if from_env_client is None:
                raise ValueError("LLM_API_KEY not set")
            active_client = from_env_client
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "session_analysis: failed to initialise live client (%s) — "
                "returning advisory fallback",
                type(exc).__name__,
            )
            return _fallback(instruments)
    else:
        active_client = client

    try:
        prompt = _build_prompt(
            candidates_summary,
            calendar_events,
            market_stats,
            _utc_now_rfc3339(),
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "session_analysis: failed to build prompt (%s) — "
            "returning advisory fallback",
            type(exc).__name__,
        )
        return _fallback(instruments)

    try:
        raw = active_client.complete(prompt)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "session_analysis: API call failed (%s) — returning advisory fallback",
            type(exc).__name__,
        )
        return _fallback(instruments)

    return parse_session_analysis(raw, instruments=instruments)
