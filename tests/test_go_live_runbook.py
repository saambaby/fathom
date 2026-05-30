"""Artifact-lint tests for docs/go-live-runbook.md (P5-T-04).

Verifies that the runbook:
1. Exists at the expected path.
2. States the INV-07 prerequisite as a hard gate and lists the specific closed
   acceptances required (T-08, T-11, T-06).
3. References ONLY shipped controls (no invented commands).
4. Contains the hard-ordering statement (flag set only after passing preflight).
5. Has a rollback/stand-down procedure (flag-off instant + kill switch backstop).
6. Has a small-size-start and manual-ramp policy.
7. Has a monitoring-during-cutover section.
8. Has a dated go/no-go decision-record section.
9. States explicitly that going live is operator-only and deliberate (INV-07).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RUNBOOK_PATH = Path(__file__).resolve().parent.parent / "docs" / "go-live-runbook.md"


@pytest.fixture(scope="module")
def runbook_text() -> str:
    """Return the full text of the go-live-runbook.md file."""
    assert RUNBOOK_PATH.exists(), (
        f"docs/go-live-runbook.md not found at {RUNBOOK_PATH}. "
        "P5-T-04 requires this file to exist."
    )
    return RUNBOOK_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. File exists
# ---------------------------------------------------------------------------


def test_runbook_exists() -> None:
    """docs/go-live-runbook.md must exist."""
    assert RUNBOOK_PATH.exists(), (
        f"docs/go-live-runbook.md not found at {RUNBOOK_PATH}."
    )


# ---------------------------------------------------------------------------
# 2. INV-07 prerequisite is stated as a hard gate with specific acceptances
# ---------------------------------------------------------------------------


def test_inv07_hard_gate_mentioned(runbook_text: str) -> None:
    """The runbook must reference INV-07 as the gate."""
    assert "INV-07" in runbook_text, (
        "The runbook must mention INV-07 as the hard gate for the live cutover."
    )


def test_hard_gate_language(runbook_text: str) -> None:
    """The runbook must state the prerequisite is a hard gate (not a suggestion)."""
    lower = runbook_text.lower()
    assert "hard gate" in lower or "blocked" in lower, (
        "The runbook must state the INV-07 prerequisite is a hard gate / that "
        "the cutover is blocked until requirements are met."
    )


def test_t08_acceptance_referenced(runbook_text: str) -> None:
    """The runbook must reference Phase 2 T-08 acceptance."""
    assert "T-08" in runbook_text, (
        "The runbook must list Phase 2 T-08 (Discord) as a required closed acceptance."
    )


def test_t11_acceptance_referenced(runbook_text: str) -> None:
    """The runbook must reference Phase 3 T-11 acceptance."""
    assert "T-11" in runbook_text, (
        "The runbook must list Phase 3 T-11 (live demo loop) as a required closed acceptance."
    )


def test_t06_acceptance_referenced(runbook_text: str) -> None:
    """The runbook must reference Phase 4 T-06 acceptance."""
    assert "T-06" in runbook_text, (
        "The runbook must list Phase 4 T-06 (panel) as a required closed acceptance."
    )


# ---------------------------------------------------------------------------
# 3. Only shipped controls are referenced — no invented commands
# ---------------------------------------------------------------------------

# These are the real, shipped commands. The runbook MUST reference them.
REQUIRED_SHIPPED_CONTROLS = [
    "fathom preflight",
    "fathom execute",
    "fathom reconcile",
    "fathom positions",
    "run_monitor.py",
    "LIVE_TRADING_ENABLED",
    "LIVE_RISK_FRACTION",
    "ENV",
    "live_trading_enabled",
]

# These are invented / non-existent commands that must NOT appear.
INVENTED_COMMANDS = [
    "fathom go-live",
    "fathom cutover",
    "fathom live",
    "fathom activate",
    "fathom flip",
    "fathom deploy",
]


@pytest.mark.parametrize("control", REQUIRED_SHIPPED_CONTROLS)
def test_shipped_control_present(runbook_text: str, control: str) -> None:
    """Each shipped control must appear in the runbook."""
    assert control in runbook_text, (
        f"Shipped control '{control}' not found in the runbook. "
        "The runbook must reference only real, shipped controls."
    )


@pytest.mark.parametrize("invented", INVENTED_COMMANDS)
def test_no_invented_command(runbook_text: str, invented: str) -> None:
    """Invented commands must not appear in the runbook."""
    assert invented not in runbook_text, (
        f"Invented command '{invented}' found in the runbook. "
        "The runbook must reference only real, shipped controls."
    )


# ---------------------------------------------------------------------------
# 4. Hard-ordering requirement is explicitly stated (flag only after GO preflight)
# ---------------------------------------------------------------------------


def test_hard_ordering_requirement_stated(runbook_text: str) -> None:
    """The runbook must state the ordering as a hard prerequisite, not a suggestion."""
    lower = runbook_text.lower()
    # Must contain language about ordering being mandatory (not a suggestion).
    assert (
        "hard prerequisite" in lower
        or "not a suggestion" in lower
        or "critical ordering" in lower.replace("-", " ")
    ), (
        "The runbook must state that the cutover ordering is a hard prerequisite, "
        "not a suggestion (e.g. 'CRITICAL ORDERING REQUIREMENT' section)."
    )


def test_flag_only_after_preflight_go_stated(runbook_text: str) -> None:
    """The runbook must state that LIVE_TRADING_ENABLED is set ONLY after a GO preflight."""
    # Look for the key constraint: flag set after preflight, not before.
    # A variety of phrasings are acceptable — check for the substance.
    patterns = [
        r"only after.*go",
        r"only after.*preflight",
        r"never.*flag.*before.*preflight",
        r"never.*before.*passing.*preflight",
        r"flag.*is.*the.*attestation",
        r"never set.*live_trading_enabled.*before",
        r"never enable the flag without",
    ]
    lower = runbook_text.lower()
    matched = any(re.search(p, lower) for p in patterns)
    assert matched, (
        "The runbook must state that LIVE_TRADING_ENABLED=true is set ONLY after "
        "a passing 'fathom preflight --attest-track-record' run — never before."
    )


def test_flag_is_attestation_record_stated(runbook_text: str) -> None:
    """The runbook must state that the flag IS the attestation record."""
    assert "attestation record" in runbook_text.lower() or (
        "flag" in runbook_text.lower() and "attestation" in runbook_text.lower()
    ), (
        "The runbook must explain that LIVE_TRADING_ENABLED is the persisted "
        "attestation record — setting it without a prior passing preflight defeats "
        "the gate (D-P5-2)."
    )


# ---------------------------------------------------------------------------
# 5. Rollback / stand-down procedure present
# ---------------------------------------------------------------------------


def test_rollback_section_present(runbook_text: str) -> None:
    """The runbook must have a rollback/stand-down section."""
    lower = runbook_text.lower()
    assert "rollback" in lower or "stand-down" in lower or "stand down" in lower, (
        "The runbook must contain a rollback or stand-down section."
    )


def test_flag_off_is_instant_stated(runbook_text: str) -> None:
    """The runbook must state that setting the flag to false is instant."""
    lower = runbook_text.lower()
    assert "instant" in lower, (
        "The runbook must state that LIVE_TRADING_ENABLED=false is instant "
        "(the gate refuses immediately without any network call)."
    )


def test_kill_switch_backstop_mentioned(runbook_text: str) -> None:
    """The runbook must mention the daily-loss kill switch as the automated backstop."""
    lower = runbook_text.lower()
    assert "kill switch" in lower or "kill-switch" in lower, (
        "The runbook must mention the daily-loss kill switch as the automated backstop."
    )


# ---------------------------------------------------------------------------
# 6. Small-size start and manual ramp policy documented
# ---------------------------------------------------------------------------


def test_small_size_start_documented(runbook_text: str) -> None:
    """The runbook must document the small-size start (0.10% / 0.001)."""
    assert "0.001" in runbook_text or "0.10%" in runbook_text, (
        "The runbook must document the initial live_risk_fraction of 0.001 (0.10%)."
    )


def test_ramp_is_manual_stated(runbook_text: str) -> None:
    """The runbook must state the ramp is manual / deliberate (never automatic)."""
    lower = runbook_text.lower()
    assert "never automatic" in lower or "not automatic" in lower or (
        "deliberate" in lower and "ramp" in lower
    ), (
        "The runbook must state that the size ramp is a deliberate operator decision, "
        "never automatic."
    )


def test_inv05_cap_mentioned(runbook_text: str) -> None:
    """The runbook must reference the INV-05 0.25% cap and the validator."""
    assert "0.0025" in runbook_text or "0.25%" in runbook_text, (
        "The runbook must reference the INV-05 0.25% per-trade cap."
    )


def test_field_validator_mentioned(runbook_text: str) -> None:
    """The runbook must mention that the Field validator rejects values above the cap."""
    lower = runbook_text.lower()
    assert "validator" in lower or "validationerror" in lower or "field" in lower, (
        "The runbook must mention that Field(le=0.0025) rejects a ramp typo above "
        "the INV-05 cap at startup."
    )


# ---------------------------------------------------------------------------
# 7. Monitoring-during-cutover section present
# ---------------------------------------------------------------------------


def test_monitoring_section_present(runbook_text: str) -> None:
    """The runbook must contain a monitoring-during-cutover section."""
    lower = runbook_text.lower()
    assert "monitor" in lower, (
        "The runbook must contain a monitoring-during-cutover section."
    )


def test_slippage_watch_mentioned(runbook_text: str) -> None:
    """The runbook must mention watching slippage on first live fills."""
    assert "slippage" in runbook_text.lower(), (
        "The runbook must instruct the operator to watch slippage on first live fills."
    )


def test_feed_health_watch_mentioned(runbook_text: str) -> None:
    """The runbook must mention watching feed health."""
    lower = runbook_text.lower()
    assert "feed health" in lower or "heartbeat" in lower or "feed" in lower, (
        "The runbook must instruct the operator to watch feed health during the "
        "first live session."
    )


# ---------------------------------------------------------------------------
# 8. Dated go/no-go decision-record section present
# ---------------------------------------------------------------------------


def test_decision_record_section_present(runbook_text: str) -> None:
    """The runbook must contain a go/no-go decision-record section."""
    lower = runbook_text.lower()
    assert "decision record" in lower or "go/no-go" in lower or "no-go" in lower, (
        "The runbook must contain a dated go/no-go decision-record section."
    )


def test_date_field_in_decision_record(runbook_text: str) -> None:
    """The decision record must include a Date field."""
    assert "Date:" in runbook_text or "YYYY-MM-DD" in runbook_text, (
        "The decision record section must include a Date field for the dated record."
    )


def test_signed_off_by_in_decision_record(runbook_text: str) -> None:
    """The decision record must include a sign-off field."""
    lower = runbook_text.lower()
    assert "signed off" in lower or "sign-off" in lower or "reviewer" in lower, (
        "The decision record section must include a sign-off / reviewer field."
    )


# ---------------------------------------------------------------------------
# 9. Operator-only, deliberate — no automated cutover
# ---------------------------------------------------------------------------


def test_operator_only_stated(runbook_text: str) -> None:
    """The runbook must state that going live is operator-only."""
    lower = runbook_text.lower()
    assert "operator-only" in lower or "operator only" in lower, (
        "The runbook must state that going live is operator-only."
    )


def test_no_automated_cutover_stated(runbook_text: str) -> None:
    """The runbook must state that no automated step performs the cutover."""
    lower = runbook_text.lower()
    assert (
        "no automated" in lower
        or "never automated" in lower
        or "not automated" in lower
        or "no automated step" in lower
    ), (
        "The runbook must state explicitly that no automated step performs the "
        "live cutover (INV-07)."
    )


# ---------------------------------------------------------------------------
# 10. fathom preflight --attest-track-record is the exact shipped command
# ---------------------------------------------------------------------------


def test_attest_track_record_flag_referenced(runbook_text: str) -> None:
    """The runbook must reference the --attest-track-record flag exactly."""
    assert "--attest-track-record" in runbook_text, (
        "The runbook must reference 'fathom preflight --attest-track-record' exactly "
        "— this is the shipped flag for the operator attestation."
    )
