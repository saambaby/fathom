"""Tests for hermes_integration.narration — watchlist-narration (P2-T-05).

Coverage:
    - fallback_narration: produces a non-empty one-liner for any candidate
      (LONG/SHORT, each shipped strategy prefix).
    - should_use_fallback: empty/whitespace/over-long Claude response → True;
      normal response → False.
    - Narration failure is COSMETIC — empty/unusable Claude response → fallback
      used, candidate unaffected (NOT an INV-02 veto, candidate is KEPT).
    - fallback_narration never raises on any valid Candidate.
    - INV-08: no secrets or tokens in module or fallback output.

No live Claude / Anthropic calls anywhere in this module.
"""

from __future__ import annotations

import logging

import pytest

from hermes_integration.narration import (
    _MAX_NARRATION_LENGTH,
    fallback_narration,
    should_use_fallback,
)
from signals.ranker import Candidate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_candidate(
    instrument: str = "EUR_USD",
    timeframe: str = "H1",
    strategy_name: str = "macrossover_10_50",
    direction: str = "LONG",
    oos_sharpe_mean: float = 0.30,
    news_flag: bool = False,
) -> Candidate:
    """Build a minimal valid Candidate for testing."""
    return Candidate(
        instrument=instrument,
        timeframe=timeframe,
        strategy_name=strategy_name,
        direction=direction,
        entry_ref=1.1000,
        stop_distance=0.0020,
        target_distance=0.0030,
        oos_sharpe_mean=oos_sharpe_mean,
        quality_score=0.75,
        rank=1,
        spread_ok=True,
        session_ok=True,
        news_flag=news_flag,
        generated_at="2026-05-29T08:00:00Z",
    )


# ---------------------------------------------------------------------------
# TestFallbackNarrationBasic
# ---------------------------------------------------------------------------


class TestFallbackNarrationBasic:
    """Basic contract: non-empty, one-liner, no exception."""

    def test_returns_string(self) -> None:
        c = _make_candidate()
        result = fallback_narration(c)
        assert isinstance(result, str)

    def test_never_empty(self) -> None:
        c = _make_candidate()
        result = fallback_narration(c)
        assert result.strip() != ""

    def test_no_newlines(self) -> None:
        """Result must be a single line."""
        c = _make_candidate()
        result = fallback_narration(c)
        assert "\n" not in result

    def test_contains_instrument(self) -> None:
        c = _make_candidate(instrument="GBP_USD")
        result = fallback_narration(c)
        # Instrument displayed as GBP/USD or GBP_USD — either is fine.
        assert "GBP" in result and "USD" in result

    def test_contains_timeframe(self) -> None:
        c = _make_candidate(timeframe="H4")
        result = fallback_narration(c)
        assert "H4" in result

    def test_contains_strategy(self) -> None:
        c = _make_candidate(strategy_name="donchian_20")
        result = fallback_narration(c)
        assert "donchian_20" in result.lower() or "donchian" in result.lower()

    def test_contains_sharpe(self) -> None:
        c = _make_candidate(oos_sharpe_mean=0.42)
        result = fallback_narration(c)
        assert "0.42" in result

    def test_reasonable_length(self) -> None:
        """Should be well under 280 characters for default fields."""
        c = _make_candidate()
        result = fallback_narration(c)
        assert len(result) <= _MAX_NARRATION_LENGTH


# ---------------------------------------------------------------------------
# TestFallbackNarrationDirections
# ---------------------------------------------------------------------------


class TestFallbackNarrationDirections:
    """One-liner for both LONG and SHORT."""

    def test_long_direction(self) -> None:
        c = _make_candidate(direction="LONG")
        result = fallback_narration(c)
        assert result.strip() != ""
        assert "long" in result.lower() or "LONG" in result

    def test_short_direction(self) -> None:
        c = _make_candidate(direction="SHORT")
        result = fallback_narration(c)
        assert result.strip() != ""
        assert "short" in result.lower() or "SHORT" in result


# ---------------------------------------------------------------------------
# TestFallbackNarrationStrategies
# ---------------------------------------------------------------------------


class TestFallbackNarrationStrategies:
    """One-liner for each shipped strategy prefix (INV-13 requirement)."""

    @pytest.mark.parametrize(
        "strategy_name",
        [
            "macrossover_10_50",
            "donchian_20",
            "bollinger_20_2",
            "rsi_14_30_70",
            "roc_10_eur_usd_h1",
            "session_20",
        ],
    )
    def test_strategy_produces_nonempty_narration(self, strategy_name: str) -> None:
        c = _make_candidate(strategy_name=strategy_name)
        result = fallback_narration(c)
        assert result.strip() != ""

    @pytest.mark.parametrize(
        "strategy_name",
        [
            "macrossover_10_50",
            "donchian_20",
            "bollinger_20_2",
            "rsi_14_30_70",
            "roc_10_eur_usd_h1",
            "session_20",
        ],
    )
    def test_strategy_no_newline(self, strategy_name: str) -> None:
        c = _make_candidate(strategy_name=strategy_name)
        result = fallback_narration(c)
        assert "\n" not in result


# ---------------------------------------------------------------------------
# TestFallbackNarrationNewsFlag
# ---------------------------------------------------------------------------


class TestFallbackNarrationNewsFlag:
    """news_flag=True vs False produce valid outputs."""

    def test_no_news_flag(self) -> None:
        c = _make_candidate(news_flag=False)
        result = fallback_narration(c)
        assert result.strip() != ""

    def test_news_flag_true_mentions_news(self) -> None:
        c = _make_candidate(news_flag=True)
        result = fallback_narration(c)
        assert result.strip() != ""
        # The spec says fold the news verdict in when news_flag is True.
        assert "news" in result.lower() or "event" in result.lower()

    def test_news_flag_false_no_news_mention(self) -> None:
        c = _make_candidate(news_flag=False)
        result = fallback_narration(c)
        # When no news flag, the narration should not invent a news warning.
        assert "news" not in result.lower()


# ---------------------------------------------------------------------------
# TestFallbackNarrationNeverRaises
# ---------------------------------------------------------------------------


class TestFallbackNarrationNeverRaises:
    """fallback_narration must never raise on a valid Candidate."""

    @pytest.mark.parametrize(
        "instrument,timeframe,strategy_name,direction,oos_sharpe_mean,news_flag",
        [
            ("EUR_USD", "H1", "macrossover_10_50", "LONG", 0.30, False),
            ("GBP_USD", "H4", "donchian_20", "SHORT", 0.15, True),
            ("USD_JPY", "D", "bollinger_20_2", "LONG", 0.50, False),
            ("AUD_USD", "M30", "rsi_14_30_70", "SHORT", 0.22, True),
            ("EUR_JPY", "H1", "roc_10_eur_usd_h1", "LONG", 0.18, False),
            ("USD_CAD", "H4", "session_20", "SHORT", 0.35, True),
        ],
    )
    def test_no_exception(
        self,
        instrument: str,
        timeframe: str,
        strategy_name: str,
        direction: str,
        oos_sharpe_mean: float,
        news_flag: bool,
    ) -> None:
        c = _make_candidate(
            instrument=instrument,
            timeframe=timeframe,
            strategy_name=strategy_name,
            direction=direction,
            oos_sharpe_mean=oos_sharpe_mean,
            news_flag=news_flag,
        )
        # Must not raise.
        result = fallback_narration(c)
        assert isinstance(result, str)
        assert result.strip() != ""


# ---------------------------------------------------------------------------
# TestShouldUseFallback
# ---------------------------------------------------------------------------


class TestShouldUseFallback:
    """should_use_fallback correctly identifies unusable Claude responses."""

    def test_empty_string_uses_fallback(self) -> None:
        assert should_use_fallback("") is True

    def test_whitespace_only_uses_fallback(self) -> None:
        assert should_use_fallback("   ") is True
        assert should_use_fallback("\n\t  \n") is True

    def test_none_like_empty_string_uses_fallback(self) -> None:
        # API contracts return str; but guard empty string defensively.
        assert should_use_fallback("") is True

    def test_over_length_uses_fallback(self) -> None:
        long_response = "A" * (_MAX_NARRATION_LENGTH + 1)
        assert should_use_fallback(long_response) is True

    def test_exactly_at_max_length_ok(self) -> None:
        at_max = "A" * _MAX_NARRATION_LENGTH
        assert should_use_fallback(at_max) is False

    def test_one_under_max_length_ok(self) -> None:
        one_under = "A" * (_MAX_NARRATION_LENGTH - 1)
        assert should_use_fallback(one_under) is False

    def test_normal_response_does_not_use_fallback(self) -> None:
        good_response = (
            "Donchian 20-bar breakout long on GBP/USD H4, OOS Sharpe 0.25."
        )
        assert should_use_fallback(good_response) is False

    def test_minimal_response_does_not_use_fallback(self) -> None:
        assert should_use_fallback("Signal on EUR/USD.") is False


# ---------------------------------------------------------------------------
# TestNarrationIsCosmetic — NOT INV-02 (most important safety property)
# ---------------------------------------------------------------------------


class TestNarrationIsCosmetic:
    """Narration failure is COSMETIC — candidate is NEVER dropped/vetoed.

    These tests verify the critical distinction from news_risk:
    - An empty Claude response → fallback is used, not a veto.
    - An over-long Claude response → fallback is used, not a veto.
    - should_use_fallback returns True (use fallback), NOT a "skip" verdict.
    - Calling fallback_narration on the candidate succeeds (candidate kept).

    A candidate being "kept" is modelled here by verifying that:
    1. should_use_fallback signals the unusable response.
    2. fallback_narration returns a valid string for the same candidate.
    3. There is NO veto/skip/drop object anywhere in the narration path.
    """

    def _simulate_narration_pipeline(
        self, candidate: Candidate, claude_response: str
    ) -> tuple[str, bool]:
        """Simulate the caller's decision logic.

        Returns (narration_text, used_fallback).
        The candidate is ALWAYS kept regardless of used_fallback.
        """
        if should_use_fallback(claude_response):
            return fallback_narration(candidate), True
        return claude_response.strip(), False

    def test_empty_claude_response_uses_fallback_candidate_kept(self) -> None:
        c = _make_candidate()
        narration, used_fallback = self._simulate_narration_pipeline(c, "")
        assert used_fallback is True
        assert narration.strip() != ""
        # No veto — candidate is still the same object, unmodified.
        assert c.rank == 1  # candidate unchanged

    def test_whitespace_claude_response_uses_fallback_candidate_kept(self) -> None:
        c = _make_candidate()
        narration, used_fallback = self._simulate_narration_pipeline(c, "   \n  ")
        assert used_fallback is True
        assert narration.strip() != ""
        assert c.rank == 1

    def test_over_long_claude_response_uses_fallback_candidate_kept(self) -> None:
        c = _make_candidate()
        too_long = "Word " * 100  # well over 280 chars
        narration, used_fallback = self._simulate_narration_pipeline(c, too_long)
        assert used_fallback is True
        assert narration.strip() != ""
        assert c.rank == 1

    def test_valid_claude_response_used_directly(self) -> None:
        c = _make_candidate()
        good = "Donchian breakout long on EUR/USD H1, OOS Sharpe 0.30."
        narration, used_fallback = self._simulate_narration_pipeline(c, good)
        assert used_fallback is False
        assert narration == good

    def test_no_veto_object_returned(self) -> None:
        """Narration path must never return anything resembling a veto/skip verdict."""
        c = _make_candidate()
        result = fallback_narration(c)
        # The narration module must not produce skip/veto/drop keywords that
        # could be confused with a news-risk verdict.
        assert "suggest_action" not in result
        assert "skip" not in result.lower().split()  # "skip" as a standalone word


# ---------------------------------------------------------------------------
# TestNoSecretsInNarration — INV-08
# ---------------------------------------------------------------------------


class TestNoSecretsInNarration:
    """INV-08: fallback_narration output must not contain tokens or keys."""

    _SECRET_PATTERNS = [
        "OANDA_API_KEY",
        "api_key",
        "sk-",
        "Bearer ",
        "Authorization:",
        "password",
    ]

    @pytest.mark.parametrize("pattern", _SECRET_PATTERNS)
    def test_no_secret_pattern_in_fallback(self, pattern: str) -> None:
        c = _make_candidate()
        result = fallback_narration(c)
        assert pattern not in result


# ---------------------------------------------------------------------------
# TestFallbackNarrationLogging
# ---------------------------------------------------------------------------


class TestFallbackNarrationLogging:
    """Logging behaviour: no warning on normal path; warning on error path."""

    def test_no_warning_on_normal_call(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        c = _make_candidate()
        with caplog.at_level(logging.WARNING, logger="hermes_integration.narration"):
            fallback_narration(c)
        assert caplog.records == []
