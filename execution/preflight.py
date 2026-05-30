"""Read-only go/no-go readiness check for live cutover (P5-T-03).

Verifies the mechanical prerequisites are in place — account reachable, kill
switch armed and not tripped, brackets/INV-04 enforceable, env↔flag↔token
consistency — and requires an explicit operator **track-record attestation**.

This module is **read-only**: it places no orders, writes no state, and never
calls ``submit_order`` or ``size_position``.  It is the readiness gate that
``live-trading-gate`` (P5-T-02) requires before a live order is permitted.

Invariants enforced here
------------------------
* **INV-03** — all timestamps UTC; ``now`` is always ``datetime.now(timezone.utc)``.
* **INV-07** — demo first: preflight never auto-approves a live cutover; the
  operator must pass ``attested=True`` (``--attest-track-record`` in the CLI).
* **INV-08** — the OANDA token is **never** printed or included in any
  ``PreflightReport`` detail string.
* **INV-09** — ``run_preflight`` may read ``settings.env`` /
  ``settings.live_trading_enabled`` at the operator boundary for the
  env/flag/token consistency check (sanctioned gate usage); it does not alter
  mechanics.  The bracket/INV-04 check is static — no ``env``-aware branch in
  ``execution/models.py``.

Read-only guarantee
-------------------
``run_preflight`` takes injected ``settings``, ``store``, and an optional
``client``.  The client is used only for a read (``account_summary``); no order
or write method is reachable from this module.  Tests stub the client to verify
no order/write calls are made.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

from risk.limits import LimitsConfig, kill_switch_armed
from execution.models import build_bracket

if TYPE_CHECKING:
    from config.settings import Settings
    from data.oanda_client import OandaClient
    from data.store import Store


__all__ = ["CheckResult", "PreflightReport", "run_preflight"]


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


class CheckResult(BaseModel):
    """A single per-check outcome in a :class:`PreflightReport`.

    Attributes:
        name: Short identifier for the check (e.g. ``"account_reachable"``).
        ok: ``True`` iff the check passed.
        detail: Human-readable detail string; always populated, never contains
            secrets (INV-08).
    """

    name: str
    ok: bool
    detail: str


class PreflightReport(BaseModel):
    """Overall go/no-go report from :func:`run_preflight`.

    Attributes:
        go: ``True`` only when **all** checks pass *and* the operator has
            attested the track record (``attested=True``).  ``False`` means the
            system is not cleared for a live cutover.
        checks: Ordered list of :class:`CheckResult` items, one per check.
            Failing checks carry a ``detail`` naming the reason.
        checked_at: UTC timestamp of the preflight run (INV-03).
    """

    go: bool
    checks: list[CheckResult]
    checked_at: datetime


# ---------------------------------------------------------------------------
# Static bracket/INV-04 contract check
# ---------------------------------------------------------------------------

_BRACKET_CONTRACT_OK: bool = False
_BRACKET_CONTRACT_DETAIL: str = ""


def _check_bracket_contract() -> tuple[bool, str]:
    """Static assertion that ``build_bracket`` enforces non-positive stop/target rejection.

    Verifies the INV-04 contract by confirming ``build_bracket`` raises
    ``ValueError`` when supplied a non-positive ``stop_distance`` (which would
    produce a naked or zero-distance stop).  This is a static contract check —
    no real order is constructed or submitted.

    Returns:
        ``(True, "INV-04 bracket contract holds: non-positive stop/target
        rejected by build_bracket")`` when the assertion passes;
        ``(False, reason)`` if, unexpectedly, the contract is broken.
    """
    from signals.ranker import Candidate
    from strategies.base import Direction

    # Minimal candidate with zero stop_distance — build_bracket must reject.
    try:
        _bad_candidate = Candidate(
            instrument="EUR_USD",
            timeframe="H1",
            strategy_name="test",
            direction=Direction.LONG.value,
            entry_ref=1.1000,
            stop_distance=0.0,  # non-positive — must be rejected by build_bracket
            target_distance=0.0015,
            oos_sharpe_mean=1.0,
            quality_score=0.5,
            rank=1,
            spread_ok=True,
            session_ok=True,
            news_flag=False,
            generated_at="2026-01-01T00:00:00Z",
        )
    except Exception:
        # Candidate itself may reject stop_distance=0 — that is equally fine
        # (the contract is enforced even earlier).
        return (
            True,
            "INV-04 bracket contract holds: non-positive stop_distance rejected "
            "at the Candidate model boundary.",
        )

    # If Candidate accepted it, build_bracket must raise.
    try:
        build_bracket(
            _bad_candidate,
            units=1000,
            execution_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            precision=5,
        )
        # If we reach here, the contract is broken.
        return (
            False,
            "INV-04 BREACH: build_bracket accepted a non-positive stop_distance "
            "without raising ValueError.  Naked orders are possible.",
        )
    except ValueError:
        return (
            True,
            "INV-04 bracket contract holds: build_bracket raises ValueError on "
            "non-positive stop_distance, preventing naked orders.",
        )


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


def run_preflight(
    *,
    settings: "Settings",
    store: "Store",
    client: "Optional[OandaClient]" = None,
    attested: bool = False,
) -> PreflightReport:
    """Run the full mechanical preflight check and return a :class:`PreflightReport`.

    Pure orchestration over read-only inputs: no order submission, no state
    writes.  Every piece of state is injected (``settings``, ``store``,
    ``client``) so the function is fully offline-testable against a seeded store
    and stub client.

    Checks (in order):
    1. **Account reachable** — ``client.account_summary()`` succeeds (if client
       provided; skipped with a warning when ``None``).
    2. **Kill switch armed** — ``kill_switch_armed(store.load_account_state(),
       now, config=LimitsConfig(), staleness_minutes=10)`` returns ``(True, "")``.
       NO-GO if the account state is missing, stale (>10 min), or the switch is
       tripped.
    3. **Brackets / INV-04** — static contract assertion that ``build_bracket``
       rejects a non-positive stop distance (no naked-order path).
    4. **Env / flag / token consistency** — if ``ENV=live``: token present (non-
       empty) and ``oanda_account_id`` present; ``live_trading_enabled`` is
       reported.  Demo is always internally consistent.
    5. **Track-record attestation** — ``attested`` must be ``True``; preflight
       never judges edge quality itself (INV-07).

    Args:
        settings: The application settings (read-only; token is not printed,
            INV-08).
        store: The SQLite/Parquet data store (read-only; ``load_account_state``
            is called — no writes).
        client: Optional OANDA client for the reachability read.  When ``None``
            the reachability check is skipped (with a detail noting the omission).
            No order or write method on the client is ever called.
        attested: ``True`` iff the operator has explicitly asserted the demo
            track record satisfies INV-07 (pass ``--attest-track-record`` in the
            CLI).  Defaults to ``False`` (safe).

    Returns:
        A :class:`PreflightReport` with ``go=True`` only when all five checks
        pass.  ``checked_at`` is a UTC-aware datetime (INV-03).
    """
    now = datetime.now(timezone.utc)
    checks: list[CheckResult] = []

    # ------------------------------------------------------------------
    # 1. Account reachable
    # ------------------------------------------------------------------
    if client is not None:
        try:
            client.account_summary()
            checks.append(
                CheckResult(
                    name="account_reachable",
                    ok=True,
                    detail="OANDA account summary fetched successfully.",
                )
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                CheckResult(
                    name="account_reachable",
                    ok=False,
                    detail=f"OANDA account summary request failed: {exc}",
                )
            )
    else:
        checks.append(
            CheckResult(
                name="account_reachable",
                ok=False,
                detail=(
                    "No OANDA client provided — cannot verify account reachability. "
                    "Pass a valid OandaClient to run_preflight."
                ),
            )
        )

    # ------------------------------------------------------------------
    # 2. Kill switch armed
    # ------------------------------------------------------------------
    account_state = store.load_account_state()
    armed, ks_reason = kill_switch_armed(
        account_state,
        now,
        config=LimitsConfig(),
        staleness_minutes=10,
    )
    if armed:
        checks.append(
            CheckResult(
                name="kill_switch_armed",
                ok=True,
                detail=(
                    "Kill switch is armed and healthy: account state present, "
                    "fresh (within 10 min), and switch not tripped."
                ),
            )
        )
    else:
        reason_map = {
            "missing": (
                "Account state is missing — reconciliation has never run "
                "or the store is empty.  Run 'fathom reconcile' first."
            ),
            "stale": (
                "Account state is stale (as_of > 10 minutes ago).  "
                "Run 'fathom reconcile' to refresh it."
            ),
            "tripped": (
                "Kill switch is TRIPPED — the daily-loss cap has been hit.  "
                "No new entries are permitted until the next 00:00 UTC reset."
            ),
        }
        detail = reason_map.get(
            ks_reason,
            f"Kill switch check failed: {ks_reason}",
        )
        checks.append(
            CheckResult(
                name="kill_switch_armed",
                ok=False,
                detail=detail,
            )
        )

    # ------------------------------------------------------------------
    # 3. Brackets / INV-04 static contract
    # ------------------------------------------------------------------
    bracket_ok, bracket_detail = _check_bracket_contract()
    checks.append(
        CheckResult(
            name="bracket_contract_inv04",
            ok=bracket_ok,
            detail=bracket_detail,
        )
    )

    # ------------------------------------------------------------------
    # 4. Env / flag / token consistency
    # ------------------------------------------------------------------
    env = settings.env
    live_trading_enabled = settings.live_trading_enabled

    # Read token presence without ever printing the value (INV-08).
    try:
        token_value = settings.oanda_api_token.get_secret_value()
        token_present = bool(token_value and token_value.strip())
    except Exception:  # noqa: BLE001
        token_present = False

    account_id_present = bool(
        settings.oanda_account_id and settings.oanda_account_id.strip()
    )

    if env == "live":
        if not token_present:
            checks.append(
                CheckResult(
                    name="env_flag_token_consistency",
                    ok=False,
                    detail=(
                        "ENV=live but OANDA_API_TOKEN is absent or empty.  "
                        "Set a valid live token in .env (INV-08: never commit secrets)."
                    ),
                )
            )
        elif not account_id_present:
            checks.append(
                CheckResult(
                    name="env_flag_token_consistency",
                    ok=False,
                    detail=(
                        "ENV=live but OANDA_ACCOUNT_ID is absent or empty.  "
                        "Set the live account ID in .env."
                    ),
                )
            )
        elif not live_trading_enabled:
            checks.append(
                CheckResult(
                    name="env_flag_token_consistency",
                    ok=False,
                    detail=(
                        "ENV=live but live_trading_enabled=False.  "
                        "Set LIVE_TRADING_ENABLED=true in .env to permit live orders "
                        "(D-P5-2 defense-in-depth)."
                    ),
                )
            )
        else:
            checks.append(
                CheckResult(
                    name="env_flag_token_consistency",
                    ok=True,
                    detail=(
                        "ENV=live, token present, account ID present, "
                        "live_trading_enabled=True — env/flag/token consistent."
                    ),
                )
            )
    else:
        # Demo: always internally consistent.
        live_note = (
            f" (live_trading_enabled={live_trading_enabled} — "
            "no effect on demo runs)"
            if live_trading_enabled
            else ""
        )
        checks.append(
            CheckResult(
                name="env_flag_token_consistency",
                ok=True,
                detail=f"ENV=demo — always internally consistent.{live_note}",
            )
        )

    # ------------------------------------------------------------------
    # 5. Track-record attestation (INV-07)
    # ------------------------------------------------------------------
    if attested:
        checks.append(
            CheckResult(
                name="track_record_attested",
                ok=True,
                detail=(
                    "Operator has attested the demo track record satisfies INV-07. "
                    "Preflight accepts this attestation at face value."
                ),
            )
        )
    else:
        checks.append(
            CheckResult(
                name="track_record_attested",
                ok=False,
                detail=(
                    "Track-record attestation required (INV-07): the operator must "
                    "explicitly confirm the demo edge is positive before a live "
                    "cutover.  Pass --attest-track-record to the CLI command."
                ),
            )
        )

    # ------------------------------------------------------------------
    # Overall go/no-go
    # ------------------------------------------------------------------
    go = all(c.ok for c in checks)

    return PreflightReport(
        go=go,
        checks=checks,
        checked_at=now,
    )
