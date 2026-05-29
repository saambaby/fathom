"""Fathom strategies package.

Contains the Strategy ABC, Signal model, Direction enum, and concrete strategy implementations.
"""

from strategies.base import Direction, Signal, Strategy
from strategies.trend import DonchianBreakout, MACrossover

__all__ = ["Direction", "DonchianBreakout", "MACrossover", "Signal", "Strategy"]
