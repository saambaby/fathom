"""Risk package — per-trade position sizing (P3-T-03).

Owns the most safety-critical calculation in Fathom: the 0.25%-of-equity
per-trade risk cap (INV-05).  See :mod:`risk.sizing`.
"""

from risk.sizing import SizingResult, size_position

__all__ = ["SizingResult", "size_position"]
