"""The real-money safety gate (P5-T-02) — pure, default-refuse, exhaustively tested.

This is the highest-stakes module in the system: a bug here is an accidental
real-money trade.  Everything here is **pure** (no I/O, no clock, no network);
the CLI (`fathom execute`) is a thin wrapper that injects the already-resolved
``settings``, ``preflight_report``, and ``confirmed`` flag.

A live order requires **four independent gates**, all of which must pass:

1. ``settings.env == "live"``                  — the env switch
2. ``settings.live_trading_enabled is True``   — the explicit opt-in (default False)
3. ``preflight_report.go is True``             — a passing ``fathom preflight``
4. ``confirmed is True``                        — the operator's typed confirmation

The bias is **always to refuse** (default-refuse):

* On **demo** (``settings.env != "live"``) the gate is a no-op — the demo path
  is byte-identical to Phase 3 (no new friction).
* On **live**, any gate that is not *exactly* satisfied raises
  :class:`LiveTradingBlocked`, naming the **first** failing gate.
* **B-1:** a ``preflight_report`` that is ``None``, not a :class:`PreflightReport`,
  or whose ``.go`` is not exactly ``True`` is treated as a *failed* preflight
  gate (raise).  The caller wraps ``run_preflight`` so any exception in the live
  path becomes a refuse — an exception is never interpreted as GO.

Invariants
----------
* **INV-07** — demo first; live is never automatic, it requires the four gates.
* **INV-05** — :func:`effective_risk_fraction` returns ``live_risk_fraction``
  (validated ``≤ 0.0025`` at Settings construction) on live, never larger.
* **INV-09 (operator-boundary clause)** — this module is the *sanctioned*
  exception that may read ``settings.env`` / ``settings.live_trading_enabled`` /
  ``settings.live_risk_fraction``.  It only selects the gate behaviour and the
  *fraction input*; the sizing/orders/reconcile/monitor **mechanics** are
  unchanged (the same ``size_position`` runs demo and live).
* **INV-08** — the confirmation token is the ``oanda_account_id`` (a plain
  ``str``, safe to echo), never the ``SecretStr`` API token; no secret is read
  or logged here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from execution.preflight import PreflightReport
from risk.sizing import DEFAULT_RISK_FRACTION

if TYPE_CHECKING:
    from config.settings import Settings


__all__ = [
    "LiveTradingBlocked",
    "assert_live_allowed",
    "effective_risk_fraction",
]


class LiveTradingBlocked(Exception):
    """Raised by :func:`assert_live_allowed` when a live order is not permitted.

    The message names the **first** failing gate so the operator sees exactly
    which condition blocked the order.  This exception is the refuse signal: the
    caller catches it, prints the reason, exits non-zero, and places **no** order.
    """


def assert_live_allowed(
    *,
    settings: "Settings",
    preflight_report: object,
    confirmed: bool,
) -> None:
    """Permit a live order only when all four gates pass; otherwise refuse.

    On **demo** (``settings.env != "live"``) this is a **no-op** — it returns
    immediately and the demo path is unchanged (no preflight, no confirmation,
    no new friction).

    On **live** (``settings.env == "live"``) it raises :class:`LiveTradingBlocked`
    (naming the first failing gate) unless **all** of the following hold:

    * ``settings.live_trading_enabled is True`` (the explicit opt-in), and
    * ``preflight_report`` is a :class:`PreflightReport` with ``.go is True``
      (**B-1 default-refuse**: ``None`` / non-``PreflightReport`` / ``.go`` not
      exactly ``True`` → treated as a failed preflight gate, raise), and
    * ``confirmed is True`` (the operator's typed account-id confirmation).

    Args:
        settings: the application settings (read-only).  Only ``env`` and
            ``live_trading_enabled`` are consulted — no secret is read.
        preflight_report: the result of ``run_preflight``.  Accepted as
            ``object`` deliberately so a malformed / ``None`` value is handled by
            default-refuse rather than a ``TypeError`` (B-1).
        confirmed: ``True`` iff the operator typed the correct ``oanda_account_id``
            confirmation.  ``False`` (or anything not exactly ``True``) refuses.

    Returns:
        ``None`` when the order is permitted (live: all four gates pass; demo:
        always).

    Raises:
        LiveTradingBlocked: on live when any gate fails, with a reason naming the
            first failing gate.
    """
    # Gate 1: env.  On demo this is a no-op — the demo path is unchanged.
    if settings.env != "live":
        return

    # Gate 2: explicit opt-in flag (default False).  Must be exactly True.
    if settings.live_trading_enabled is not True:
        raise LiveTradingBlocked(
            "live_trading_enabled is not True (default-refuse): set "
            "LIVE_TRADING_ENABLED=true in .env to permit live orders (D-P5-2)."
        )

    # Gate 3: passing preflight (B-1 default-refuse on bad/None/malformed report).
    if not isinstance(preflight_report, PreflightReport):
        raise LiveTradingBlocked(
            "preflight gate failed: no valid PreflightReport "
            f"(got {type(preflight_report).__name__}) — default-refuse. "
            "Run 'fathom preflight --attest-track-record' and ensure it is GO."
        )
    if preflight_report.go is not True:
        raise LiveTradingBlocked(
            "preflight gate failed: preflight is NO-GO (preflight_report.go is "
            "not True) — default-refuse.  Resolve the failing preflight checks."
        )

    # Gate 4: operator's typed confirmation (the oanda_account_id).
    if confirmed is not True:
        raise LiveTradingBlocked(
            "live confirmation gate failed: the operator did not type the "
            "correct account id — default-refuse, no order placed."
        )

    # All four gates passed — permit the live order.
    return


def effective_risk_fraction(settings: "Settings") -> float:
    """Select the per-trade risk fraction for the current ``env`` (INV-05 / INV-09).

    The **only** place the env-dependent fraction is chosen.  The sizing function
    itself is unchanged — it receives this value as its ``risk_fraction`` input
    (per the INV-09 operator-boundary clause).

    Args:
        settings: the application settings; ``env`` and (on live)
            ``live_risk_fraction`` are read.

    Returns:
        ``settings.live_risk_fraction`` (validated ``> 0`` and ``≤ 0.0025`` at
        Settings construction, so never above the INV-05 cap) when
        ``settings.env == "live"``; otherwise the demo
        :data:`~risk.sizing.DEFAULT_RISK_FRACTION` (``0.0025``).
    """
    if settings.env == "live":
        return settings.live_risk_fraction
    return DEFAULT_RISK_FRACTION
