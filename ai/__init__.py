"""Standalone AI analysis — shared LLM adapter, verdict parsers, advisory surfaces.

This package owns the Fathom-side contract for in-process LLM analysis
(news-risk, pre-trade veto, narration, session brief).  No external
orchestrator is required.

Modules:
    llm_client: OpenAICompatClient + _ClientAdapter (INV-20 single adapter).
    news_risk: NewsRiskVerdict + parse_news_risk() + news_risk_check() — INV-02.
    pretrade_check: PretradeVerdict + parse_pretrade_verdict() +
        pretrade_check() — INV-02 in-process veto; re-exports the adapter.
    narration: fallback_narration() + narrate() — cosmetic (NOT INV-02).
    brief: session_analysis() — advisory session brief (NOT INV-02).
"""
