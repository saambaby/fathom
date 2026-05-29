"""Risk package — per-trade sizing + book-level limits/kill-switch.

Owns the safety-critical risk gates in Fathom:

* :mod:`risk.sizing` — the 0.25%-of-equity *per-trade* cap (INV-05, P3-T-03).
* :mod:`risk.limits` — the *book-level* gate + daily-loss kill switch
  (INV-05 backstop, P3-T-04): exposure caps, correlation-aware shared
  exposure, and the daily-loss halt.
"""

from risk.limits import (
    KillSwitchStatus,
    LimitDecision,
    LimitsConfig,
    check_limits,
    kill_switch_status,
    position_risk,
)
from risk.sizing import SizingResult, size_position

__all__ = [
    "SizingResult",
    "size_position",
    "LimitsConfig",
    "LimitDecision",
    "KillSwitchStatus",
    "check_limits",
    "kill_switch_status",
    "position_risk",
]
