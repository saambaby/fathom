"""Fathom deviation monitor — always-on watcher for open positions.

Exports:
    DeviationEvent  — the pydantic model (the producer; monitor-alerts T-09 consumes it).
    Watcher         — the always-on loop: ticks → rule evaluation → alerter.
"""

from monitoring.watcher import DeviationEvent, Watcher, WatcherConfig

__all__ = ["DeviationEvent", "Watcher", "WatcherConfig"]
