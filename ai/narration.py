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

This module is fully unit-testable offline.  No ``anthropic`` SDK dependency
(D-P2-3): Claude is invoked Hermes-side.

INV-08: No secrets, tokens, or API keys are referenced here.
"""

from __future__ import annotations

import logging

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
