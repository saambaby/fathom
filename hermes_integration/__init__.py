"""Hermes integration — prompt templates, response models, and parse helpers.

This package owns the Fathom-side contract for Claude-powered daily watchlist
generation.  No ``anthropic`` SDK dependency (D-P2-3): Claude is invoked
Hermes-side; Fathom supplies the prompt templates, validates the JSON strings
that come back, and exposes typed pydantic models to downstream pipeline steps.

Modules:
    news_risk: NewsRiskVerdict model + parse_news_risk() — INV-02 enforcement.
"""
