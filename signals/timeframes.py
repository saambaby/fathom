"""INV-21 bar-length map — single definition site (stdlib-only).

Consumers (``cli`` freshness TTL, ``fathom analyze`` entry windows, later
``fathom ask`` stale disclosure) import ``TIMEFRAME_BAR_LENGTH`` from here.
"""

from __future__ import annotations

from datetime import timedelta

#: Bar length per timeframe. Keys must match ``Candidate.timeframe``.
TIMEFRAME_BAR_LENGTH: dict[str, timedelta] = {
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D": timedelta(hours=24),
}
