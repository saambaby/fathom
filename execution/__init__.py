"""Execution package — the Phase 3 order/fill/position contract and bracket maths.

Public surface (INV-14 frozen execution contract):

* :class:`~execution.models.Order` — intent to open a bracketed position.
* :class:`~execution.models.Fill` — broker confirmation of an order.
* :class:`~execution.models.Position` — current/closed open state.
* :func:`~execution.models.build_bracket` — pure ``Candidate`` → ``Order`` maths.

This package holds no I/O, no OANDA calls, and no clock beyond the timestamps
passed into it.  Submission lives in ``order-placement``; sizing in
``position-sizing``; persistence with ``order-placement``/``reconciliation``.
"""

from __future__ import annotations

from execution.models import Fill, Order, Position, build_bracket

__all__ = ["Fill", "Order", "Position", "build_bracket"]
