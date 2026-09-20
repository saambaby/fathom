"""Watchlist narration — cosmetic presentation layer (NOT INV-02).

Owns the Fathom-side support for Claude's per-candidate one-line narration:

  * ``fallback_narration(candidate)`` — a deterministic one-liner built from
    the candidate's flat fields.  Always returns a non-empty string; never
    raises.  Used when Claude is unavailable or returns an unusable response.

**CRITICAL distinction from news_risk (NOT INV-02):**
    Narration is a *cosmetic* layer — it is presentation only and does not
    feed any automated decision (filtering, ranking, or sizing).  Therefore:

    - An empty, whitespace-only, or over-long Claude response → caller uses
      ``fallback_narration``; **the candidate is kept on the watchlist**.
    - There is **no safe-skip veto here**.  INV-02's "fail safe = drop the
      candidate" rule applies *only* to outputs that feed automated decisions
      (e.g. news-risk verdicts).  Applying INV-02's veto to narration would
      let a cosmetic-layer hiccup silently shrink the watchlist — exactly what
      the spec prohibits.
    - A future reader must NOT add a ``suggest_action="skip"`` default here.
      If narration fails, use ``fallback_narration`` and move on.

``narrate`` is the in-process call on the shared adapter.  Cosmetic only
(NOT INV-02): unusable model output → ``fallback_narration``; the candidate
is kept.  Offline: no client + no ``LLM_API_KEY`` → fallback, zero network.

INV-08: ``LLM_API_KEY`` is never logged.  The adapter holds it privately.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Literal

from pydantic import BaseModel

from ai.llm_client import OpenAICompatClient, _ClientAdapter
from signals.ranker import Candidate

_log = logging.getLogger(__name__)

# Maximum character length for a Claude-supplied narration to be considered
# usable.  Anything longer is treated as unusable → fallback.
_MAX_NARRATION_LENGTH: int = 280


# ---------------------------------------------------------------------------
# fallback_narration — the always-safe, deterministic path
# ---------------------------------------------------------------------------


def fallback_narration(candidate: Candidate) -> str:
    """Build a deterministic one-line narration from the candidate's flat fields.

    This is the guaranteed fallback path used when Claude is unavailable or
    returns an unusable response.  It is derived entirely from the candidate's
    own fields — no invented numbers, no external calls.

    **Never raises.  Never returns an empty string.**

    The candidate is **never dropped** because of a narration failure; this
    function exists solely so the caller always has *something* to show the
    trader.  See module docstring for the INV-02 non-applicability note.

    Args:
        candidate: A ranked ``Candidate`` from ``signals.ranker``.

    Returns:
        A concise plain-English one-liner describing the candidate.

    Examples:
        >>> from signals.ranker import Candidate
        >>> c = Candidate(
        ...     instrument="GBP_USD", timeframe="H4", strategy_name="donchian_20",
        ...     direction="LONG", entry_ref=1.2750, stop_distance=0.0030,
        ...     target_distance=0.0045, oos_sharpe_mean=0.25, quality_score=0.8,
        ...     rank=1, spread_ok=True, session_ok=True, news_flag=False,
        ...     generated_at="2026-05-29T06:00:00Z",
        ... )
        >>> fallback_narration(c)
        'Donchian_20 long on GBP/USD H4, OOS Sharpe 0.25.'
    """
    try:
        instrument_display = candidate.instrument.replace("_", "/")
        direction_display = candidate.direction.capitalize()
        sharpe_display = f"{candidate.oos_sharpe_mean:.2f}"
        news_suffix = (
            " Medium-impact news nearby." if candidate.news_flag else ""
        )
        line = (
            f"{candidate.strategy_name} {direction_display} on "
            f"{instrument_display} {candidate.timeframe}, "
            f"OOS Sharpe {sharpe_display}.{news_suffix}"
        )
        # Defensive: strip leading/trailing whitespace; should always be
        # non-empty given the template, but guard explicitly.
        line = line.strip()
        if not line:
            # Should never happen — but if it does, return the bare minimum.
            return (
                f"Signal on {candidate.instrument} "
                f"({candidate.timeframe}): {candidate.direction}."
            )
        return line
    except Exception as exc:  # noqa: BLE001 — catch-all; narration must never raise
        _log.warning(
            "fallback_narration: unexpected error (%s: %s) — returning bare fallback.",
            type(exc).__name__,
            exc,
        )
        # Ultra-minimal last resort — uses only basic attribute access.
        return f"Signal on {getattr(candidate, 'instrument', '?')}."


# ---------------------------------------------------------------------------
# should_use_fallback — helper for callers
# ---------------------------------------------------------------------------


def should_use_fallback(claude_response: str) -> bool:
    """Return True when a Claude narration response is unusable.

    Callers should use this to decide whether to display ``claude_response``
    or substitute ``fallback_narration(candidate)`` instead.

    A response is unusable if it is:
    - Empty or whitespace-only.
    - Over ``_MAX_NARRATION_LENGTH`` characters (likely a hallucination or
      multi-sentence output instead of the required one-liner).

    **This function never drops a candidate** — it only signals to the caller
    whether to swap in the fallback string.  The candidate is always kept.
    (NOT INV-02 — see module docstring.)

    Args:
        claude_response: The raw string returned by Claude.

    Returns:
        ``True`` if the fallback should be used; ``False`` if the response is
        usable as-is.
    """
    stripped = claude_response.strip() if claude_response else ""
    if not stripped:
        return True
    if len(stripped) > _MAX_NARRATION_LENGTH:
        return True
    return False


# ---------------------------------------------------------------------------
# NarrationResult + narrate — in-process call (NOT INV-02)
# ---------------------------------------------------------------------------

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_PROMPT_PATH = _PROMPTS_DIR / "narration.md"


class NarrationResult(BaseModel):
    """One-line watchlist narration plus whether the model or fallback produced it."""

    text: str
    source: Literal["model", "fallback"]

    model_config = {"frozen": True, "extra": "forbid"}


def _build_prompt(candidate: Candidate) -> str:
    """Render ``prompts/narration.md`` from the candidate's facts."""
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    rendered = template.replace("{{instrument}}", candidate.instrument)
    rendered = rendered.replace("{{timeframe}}", candidate.timeframe)
    rendered = rendered.replace("{{strategy_name}}", candidate.strategy_name)
    rendered = rendered.replace("{{direction}}", candidate.direction)
    rendered = rendered.replace("{{oos_sharpe_mean}}", str(candidate.oos_sharpe_mean))
    rendered = rendered.replace("{{news_flag}}", str(candidate.news_flag).lower())
    return rendered


def narrate(
    candidate: Candidate,
    *,
    client: _ClientAdapter | None = None,
) -> NarrationResult:
    """Produce a one-line narration for ``candidate``.

    Never raises.  ``text`` is never empty.  NOT INV-02 — a failed call
    returns ``fallback_narration`` with ``source="fallback"``; the caller
    keeps the candidate.
    """
    fallback_text = fallback_narration(candidate)
    fallback = NarrationResult(text=fallback_text, source="fallback")

    try:
        if client is None and not os.environ.get("LLM_API_KEY"):
            _log.warning(
                "narrate: no client and LLM_API_KEY not set — "
                "returning fallback narration (offline path)"
            )
            return fallback

        active_client: _ClientAdapter
        if client is None:
            from_env_client = OpenAICompatClient.from_env()
            if from_env_client is None:
                return fallback
            active_client = from_env_client
        else:
            active_client = client

        prompt = _build_prompt(candidate)
        raw = active_client.complete(prompt)
        if should_use_fallback(raw):
            return fallback
        text = raw.strip()
        if not text:
            return fallback
        return NarrationResult(text=text, source="model")
    except Exception as exc:  # noqa: BLE001 — narrate must never raise
        _log.warning(
            "narrate: %s: %s — returning fallback narration",
            type(exc).__name__,
            exc,
        )
        return NarrationResult(text=fallback_narration(candidate), source="fallback")
