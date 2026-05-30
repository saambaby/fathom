"""Admin panel package — read-only view models for the Fathom dashboard.

INV-01: this package must not import execution.orders, risk.sizing,
execution.models.build_bracket, or cli — directly or transitively.
The panel is a read surface only.
"""
