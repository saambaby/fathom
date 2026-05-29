"""Fathom ``signals`` package — the Phase 2 watchlist pipeline.

Public surface:
- ``Candidate`` — the frozen Hermes-facing wire contract (INV-13).
- ``Ranker`` — gate → evaluate → filter → news → conflict → rank.
"""

from __future__ import annotations

from signals.ranker import Candidate, Ranker

__all__ = ["Candidate", "Ranker"]
