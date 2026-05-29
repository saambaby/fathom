"""Hermes integration — prompt templates, response models, and parse helpers.

This package owns the Fathom-side contract for Claude-powered daily watchlist
generation and the in-process pre-trade veto gate (Phase 3).

Modules:
    news_risk: NewsRiskVerdict model + parse_news_risk() — INV-02 enforcement
        (Phase 2, Hermes-side; no anthropic SDK dependency here).
    pretrade_check: PretradeVerdict model + parse_pretrade_verdict() +
        pretrade_check() — INV-02 in-process Claude veto before order
        submission (Phase 3).  Uses the anthropic SDK via an injectable
        _LiveClient adapter; fully testable offline.
    narration: fallback_narration() — cosmetic watchlist one-liner (Phase 2).
"""
