"""Lint tests for hermes_integration/jobs/daily.md (P2-T-06).

Verifies the artefact's structural requirements without invoking Hermes,
Claude, Discord, or any live service. Pure file-content assertions.

Invariants checked:
    INV-01: job references only allowed tools (scan / watchlist / chart);
            no execute / order / risk tool references.
    INV-02: skip→veto / reduce_size→flag / proceed→keep mapping present;
            malformed-Claude safe path specified.
    INV-10: empty-watchlist safe path specified ("no candidates").
    INV-08: operator runbook present (credentials section); no hardcoded secrets.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# ---------------------------------------------------------------------------
# Locate the artefact
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_JOB_PATH = _REPO_ROOT / "hermes_integration" / "jobs" / "daily.md"


@pytest.fixture(scope="module")
def job_text() -> str:
    """Return the full text of daily.md."""
    assert _JOB_PATH.exists(), (
        f"hermes_integration/jobs/daily.md not found at {_JOB_PATH}. "
        "The artefact must be created before running this test."
    )
    return _JOB_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. File existence
# ---------------------------------------------------------------------------


def test_daily_md_exists() -> None:
    """daily.md must exist in hermes_integration/jobs/."""
    assert _JOB_PATH.exists(), f"Missing: {_JOB_PATH}"


# ---------------------------------------------------------------------------
# 2. Ordered steps: scan → news-risk → chart → narration → deliver
# ---------------------------------------------------------------------------


class TestOrderedSteps:
    """The five pipeline steps must appear in document order.

    We anchor on the numbered Step headings (e.g. "### Step 1", "Step 2 —")
    rather than on first-occurrence of a keyword, so that introductory prose
    that mentions all five terms doesn't confuse the ordering check.
    """

    def _step_position(self, text: str, step_num: int) -> int:
        """Return the character offset of the numbered step heading (section header only).

        Matches "### Step N" or "## Step N" heading lines, not inline references
        to a step number (e.g. "go to Step 5" in body text).
        """
        # Require the step to appear at the start of a line after '#' heading markers.
        pattern = rf"^#+\s+Step\s+{step_num}\b"
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        assert m is not None, f"Step {step_num} section heading not found in daily.md"
        return m.start()

    def _section_text(self, text: str, step_num: int) -> str:
        """Return the text of one numbered step section (heading-anchored)."""
        start = self._step_position(text, step_num)
        # Find the next step heading (anchored) or a horizontal rule
        next_match = re.search(
            rf"^#+\s+Step\s+{step_num + 1}\b|^---",
            text[start + 1 :],
            re.IGNORECASE | re.MULTILINE,
        )
        end = start + 1 + next_match.start() if next_match else len(text)
        return text[start:end]

    def test_scan_step_present(self, job_text: str) -> None:
        assert re.search(r"fathom scan", job_text), "scan step missing"

    def test_news_risk_step_present(self, job_text: str) -> None:
        # Step 2 heading must mention news-risk
        step2 = self._section_text(job_text, 2)
        assert re.search(r"news.?risk", step2, re.IGNORECASE), (
            "news-risk content missing from Step 2"
        )

    def test_chart_step_present(self, job_text: str) -> None:
        assert re.search(r"fathom chart", job_text), "chart step missing"

    def test_narration_step_present(self, job_text: str) -> None:
        assert re.search(r"narration", job_text, re.IGNORECASE), "narration step missing"

    def test_deliver_step_present(self, job_text: str) -> None:
        assert re.search(r"deliver|discord", job_text, re.IGNORECASE), (
            "delivery step (Discord) missing"
        )

    def test_steps_in_order(self, job_text: str) -> None:
        """Step 1 (scan) < Step 2 (news-risk) < Step 3 (chart) < Step 4 (narration)
        < Step 5 (deliver) — using the numbered step headings as anchors."""
        pos_step1 = self._step_position(job_text, 1)
        pos_step2 = self._step_position(job_text, 2)
        pos_step3 = self._step_position(job_text, 3)
        pos_step4 = self._step_position(job_text, 4)
        pos_step5 = self._step_position(job_text, 5)

        assert pos_step1 < pos_step2, "Step 1 (scan) must precede Step 2 (news-risk)"
        assert pos_step2 < pos_step3, "Step 2 (news-risk) must precede Step 3 (chart)"
        assert pos_step3 < pos_step4, "Step 3 (chart) must precede Step 4 (narration)"
        assert pos_step4 < pos_step5, "Step 4 (narration) must precede Step 5 (deliver)"

    def test_step1_is_scan(self, job_text: str) -> None:
        step1 = self._section_text(job_text, 1)
        assert re.search(r"fathom scan", step1), "Step 1 must contain 'fathom scan'"

    def test_step2_is_news_risk(self, job_text: str) -> None:
        step2 = self._section_text(job_text, 2)
        assert re.search(r"news.?risk", step2, re.IGNORECASE), (
            "Step 2 must be the news-risk assessment step"
        )

    def test_step3_is_chart(self, job_text: str) -> None:
        step3 = self._section_text(job_text, 3)
        assert re.search(r"fathom chart", step3), "Step 3 must contain 'fathom chart'"

    def test_step4_is_narration(self, job_text: str) -> None:
        step4 = self._section_text(job_text, 4)
        assert re.search(r"narration", step4, re.IGNORECASE), (
            "Step 4 must be the narration step"
        )

    def test_step5_is_deliver(self, job_text: str) -> None:
        step5 = self._section_text(job_text, 5)
        assert re.search(r"deliver|discord", step5, re.IGNORECASE), (
            "Step 5 must be the delivery step"
        )


# ---------------------------------------------------------------------------
# 3. Allowed tools — scan / watchlist / chart present (INV-01)
# ---------------------------------------------------------------------------


class TestAllowedTools:
    def test_scan_referenced(self, job_text: str) -> None:
        assert "scan" in job_text, "daily.md must reference fathom scan"

    def test_watchlist_referenced(self, job_text: str) -> None:
        assert "watchlist" in job_text, "daily.md must reference fathom watchlist"

    def test_chart_referenced(self, job_text: str) -> None:
        assert "chart" in job_text, "daily.md must reference fathom chart"


# ---------------------------------------------------------------------------
# 4. Forbidden tool references (INV-01) — no execute / orders / risk as tools
# ---------------------------------------------------------------------------


class TestForbiddenTools:
    """INV-01: execution/order tools must not be callable from Hermes.

    We grep for the specific patterns that would indicate Hermes has been
    granted order-placement authority. We do NOT ban the words in general
    (e.g. the runbook legitimately mentions 'orders.py' to say it is
    off-limits) — we ban them appearing as Hermes *tool* registrations or
    as callable CLI commands.

    Strategy: look for positive tool-granting phrases around these keywords.
    Also assert that no fathom subcommand named 'execute', 'order', or 'risk'
    is registered as a tool.
    """

    def test_no_fathom_execute_tool(self, job_text: str) -> None:
        """fathom execute must not appear as a registered/callable tool."""
        # Allow 'fathom execute' only if it is inside a note that it is forbidden.
        # Simplest: the phrase 'fathom execute' must not appear at all — the CLI
        # has no such subcommand; if it appears it's a spec error.
        assert not re.search(r"\bfathom execute\b", job_text), (
            "INV-01 violation: 'fathom execute' must not appear in daily.md"
        )

    def test_no_fathom_orders_tool(self, job_text: str) -> None:
        """fathom orders must not appear as a registered/callable tool."""
        assert not re.search(r"\bfathom orders\b", job_text), (
            "INV-01 violation: 'fathom orders' must not appear in daily.md"
        )

    def test_no_fathom_risk_tool(self, job_text: str) -> None:
        """fathom risk must not appear as a registered/callable tool."""
        assert not re.search(r"\bfathom risk\b", job_text), (
            "INV-01 violation: 'fathom risk' must not appear in daily.md"
        )

    def test_no_execute_in_allowed_tools_table(self, job_text: str) -> None:
        """The allowed-tools table must not list 'execute' as an allowed tool."""
        # Find the allowed-tools section and assert 'execute' is not in it.
        # The table heading is "Allowed Fathom CLI tools".
        m = re.search(
            r"Allowed Fathom CLI tools.*?(?=\n#|\Z)",
            job_text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            section = m.group(0)
            assert "execute" not in section.lower(), (
                "INV-01 violation: 'execute' listed in allowed-tools section"
            )

    def test_no_order_in_allowed_tools_table(self, job_text: str) -> None:
        """The allowed-tools table must not list 'order' as an allowed entry."""
        m = re.search(
            r"Allowed Fathom CLI tools.*?(?=\n#|\Z)",
            job_text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            section = m.group(0)
            # 'order' in a tool-listing context — not in an exclusion note
            # We look for 'fathom_order' or '| order' table cell patterns
            assert not re.search(r"fathom_order|\|\s*order\b", section, re.IGNORECASE), (
                "INV-01 violation: order tool listed in allowed-tools section"
            )

    def test_hermes_never_places_orders_stated(self, job_text: str) -> None:
        """The doc must explicitly state Hermes never places orders (INV-01)."""
        assert re.search(
            r"hermes.{0,40}(never|not|no).{0,40}(order|trade|place|execute)",
            job_text,
            re.IGNORECASE,
        ), (
            "INV-01 boundary statement missing: "
            "document must state Hermes never places orders"
        )


# ---------------------------------------------------------------------------
# 5. News-risk verdict mapping: skip→veto / reduce_size→flag / proceed→keep
# ---------------------------------------------------------------------------


class TestNewsRiskMapping:
    def test_skip_veto_mapping(self, job_text: str) -> None:
        assert re.search(r"skip.{0,80}veto|veto.{0,80}skip", job_text, re.IGNORECASE), (
            "skip→veto mapping missing from daily.md"
        )

    def test_reduce_size_flag_mapping(self, job_text: str) -> None:
        assert re.search(
            r"reduce_size.{0,80}flag|flag.{0,80}reduce_size",
            job_text,
            re.IGNORECASE,
        ), "reduce_size→flag mapping missing from daily.md"

    def test_proceed_keep_mapping(self, job_text: str) -> None:
        assert re.search(
            r"proceed.{0,80}keep|keep.{0,80}proceed",
            job_text,
            re.IGNORECASE,
        ), "proceed→keep mapping missing from daily.md"

    def test_all_three_verdicts_in_table(self, job_text: str) -> None:
        """All three verdict values must be mentioned."""
        for verdict in ("skip", "reduce_size", "proceed"):
            assert verdict in job_text, f"verdict '{verdict}' not in daily.md"


# ---------------------------------------------------------------------------
# 6. Empty-watchlist safe path (INV-10)
# ---------------------------------------------------------------------------


class TestEmptyWatchlist:
    def test_empty_watchlist_path_specified(self, job_text: str) -> None:
        assert re.search(
            r"empty.{0,80}watchlist|no candidates",
            job_text,
            re.IGNORECASE,
        ), "Empty-watchlist safe path missing from daily.md"

    def test_no_candidates_today_message(self, job_text: str) -> None:
        assert re.search(r"no candidates today", job_text, re.IGNORECASE), (
            "daily.md must specify 'no candidates today' message for empty watchlist"
        )

    def test_empty_watchlist_exits_zero(self, job_text: str) -> None:
        assert re.search(r"exit 0", job_text, re.IGNORECASE), (
            "daily.md must specify exit 0 for empty watchlist (INV-10)"
        )


# ---------------------------------------------------------------------------
# 7. Malformed-Claude safe path (INV-02)
# ---------------------------------------------------------------------------


class TestMalformedClaudeSafePath:
    def test_malformed_claude_path_specified(self, job_text: str) -> None:
        assert re.search(
            r"malformed|unavailable|parse.{0,30}fail",
            job_text,
            re.IGNORECASE,
        ), "Malformed-Claude safe path missing from daily.md"

    def test_inv02_safe_default_skip_stated(self, job_text: str) -> None:
        """doc must state that malformed Claude response defaults to skip."""
        assert re.search(
            r"(malformed|unavailable|parse).{0,120}skip",
            job_text,
            re.IGNORECASE,
        ), (
            "INV-02 safe default (malformed→skip) not stated in daily.md"
        )

    def test_narration_fallback_path_specified(self, job_text: str) -> None:
        """Narration fallback (not a veto) must be specified."""
        assert re.search(
            r"fallback_narration|fallback narration",
            job_text,
            re.IGNORECASE,
        ), "fallback_narration safe path missing from daily.md"

    def test_narration_failure_keeps_candidate(self, job_text: str) -> None:
        """Narration failure must keep the candidate (NOT INV-02 veto)."""
        assert re.search(
            r"narration.{0,120}(keep|kept|always kept|candidate.{0,40}kept)",
            job_text,
            re.IGNORECASE,
        ), (
            "daily.md must state that narration failure keeps the candidate "
            "(narration is cosmetic — not an INV-02 veto)"
        )


# ---------------------------------------------------------------------------
# 8. Operator runbook section present (INV-08)
# ---------------------------------------------------------------------------


class TestOperatorRunbook:
    def test_runbook_section_present(self, job_text: str) -> None:
        assert re.search(r"operator runbook", job_text, re.IGNORECASE), (
            "Operator runbook section missing from daily.md"
        )

    def test_runbook_register_job_instruction(self, job_text: str) -> None:
        assert re.search(
            r"register.{0,60}(job|hermes)|hermes.{0,60}register",
            job_text,
            re.IGNORECASE,
        ), "Runbook must explain how to register the job in Hermes"

    def test_runbook_fathom_cli_as_tool(self, job_text: str) -> None:
        assert re.search(
            r"(fathom.{0,20}(cli|tool)|tool.{0,20}fathom)",
            job_text,
            re.IGNORECASE,
        ), "Runbook must explain how to point Hermes at the fathom CLI as a tool"

    def test_runbook_discord_gateway(self, job_text: str) -> None:
        assert re.search(r"discord", job_text, re.IGNORECASE), (
            "Runbook must mention Discord gateway connection"
        )

    def test_runbook_anthropic_key(self, job_text: str) -> None:
        assert re.search(r"anthropic.{0,30}key|ANTHROPIC_API_KEY", job_text), (
            "Runbook must mention Anthropic key configuration"
        )

    def test_secrets_in_env_not_committed(self, job_text: str) -> None:
        """Runbook must state secrets go in .env and are never committed (INV-08)."""
        assert re.search(
            r"\.env.{0,80}(never commit|not commit|gitignore)|"
            r"(never commit|not commit).{0,80}\.env",
            job_text,
            re.IGNORECASE,
        ), (
            "INV-08: runbook must state credentials live in .env and are never committed"
        )

    def test_no_hardcoded_secrets_in_file(self, job_text: str) -> None:
        """The file must contain no hardcoded secret values (INV-08)."""
        # Reject patterns like sk-ant-<20+ chars>, Bearer <token>, etc.
        # Allow the placeholder 'sk-ant-...' used as an example.
        assert not re.search(r"sk-ant-[A-Za-z0-9_-]{20,}", job_text), (
            "INV-08 violation: hardcoded Anthropic API key in daily.md"
        )
        assert not re.search(r"Bearer [A-Za-z0-9._-]{20,}", job_text), (
            "INV-08 violation: hardcoded bearer token in daily.md"
        )

    def test_inv01_boundary_in_runbook(self, job_text: str) -> None:
        """The runbook must explicitly state that order/execute tools must not
        be registered (INV-01).

        Matches a sentence that combines:
          - a prohibition word (never / not / must not / do not)
          - an order/execute/risk keyword
          - a tool/register/grant keyword
        in any order within a 120-character window.
        """
        runbook_match = re.search(
            r"operator runbook.*",
            job_text,
            re.IGNORECASE | re.DOTALL,
        )
        assert runbook_match is not None, "Operator runbook section not found"
        runbook_section = runbook_match.group(0)
        assert re.search(
            # Pattern A: prohibition → order/execute/risk → tool/register/grant
            r"(never|not|do not|must not).{0,80}(order|execute|risk).{0,80}(tool|register|grant)|"
            # Pattern B: order/execute/risk → tool/register/grant → prohibition
            r"(order|execute|risk).{0,80}(tool|register|grant).{0,80}(never|not|must not)|"
            # Pattern C: "never register any order/execute/risk tool"
            r"never register.{0,40}(order|execute|risk).{0,40}tool|"
            # Pattern D: "do not grant … execute or order tool"
            r"(do not|must not) grant.{0,60}(execute|order|risk)",
            runbook_section,
            re.IGNORECASE,
        ), (
            "INV-01: runbook must explicitly state order/execute tools must not be registered"
        )


# ---------------------------------------------------------------------------
# 9. scan stdout is the primary source (not fathom watchlist)
# ---------------------------------------------------------------------------


class TestScanStdoutSource:
    def test_scan_stdout_is_primary(self, job_text: str) -> None:
        assert re.search(r"stdout", job_text, re.IGNORECASE), (
            "daily.md must state that fathom scan's stdout is the primary watchlist source"
        )

    def test_watchlist_not_primary_source(self, job_text: str) -> None:
        """The doc must clarify fathom watchlist is not the primary source."""
        assert re.search(
            r"(not|do not|NOT).{0,30}(fathom watchlist|watchlist.{0,20}primary)|"
            r"(persisted.?read|re.?read)",
            job_text,
            re.IGNORECASE,
        ), (
            "daily.md must clarify fathom watchlist is the persisted-read accessor, "
            "not the primary daily source"
        )
