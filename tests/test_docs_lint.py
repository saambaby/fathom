"""Doc-lint guard (WS0-T07): dead names must never reappear in operator docs.

Plain file-content assertions — no markdown parsing needed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

_OPERATOR_DOCS = (
    REPO_ROOT / "docs" / "operator-acceptance.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs" / "go-live-runbook.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _index_row(text: str, feature: str) -> str:
    prefix = f"| {feature} "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"docs/features/INDEX.md missing row for {feature!r}")


class TestNoDeadEnvVarNames:
    """OANDA_API_KEY / OANDA_ENV were renamed to OANDA_API_TOKEN / ENV."""

    def test_operator_acceptance_does_not_use_dead_env_var_names(self) -> None:
        text = _read(REPO_ROOT / "docs" / "operator-acceptance.md")
        assert "OANDA_API_KEY" not in text
        assert "OANDA_ENV" not in text


class TestNoInventedCandidateRefFormat:
    """The real candidate-ref format is instrument:timeframe:StrategyName(params)."""

    def test_claude_md_does_not_use_invented_candidate_ref(self) -> None:
        text = _read(REPO_ROOT / "CLAUDE.md")
        assert "macrossover_10_50" not in text

    def test_go_live_runbook_does_not_use_invented_candidate_ref(self) -> None:
        text = _read(REPO_ROOT / "docs" / "go-live-runbook.md")
        assert "macrossover_10_50" not in text

    def test_cli_does_not_use_invented_candidate_ref(self) -> None:
        text = _read(REPO_ROOT / "cli.py")
        assert "macrossover_10_50" not in text


class TestRetiredSurfaceDeadNames:
    """Retired names cannot reappear in operator docs (phase-07 teardown)."""

    def test_fathom_chart_and_daily_md_absent_from_operator_docs(self) -> None:
        for path in _OPERATOR_DOCS:
            text = _read(path)
            assert "fathom chart" not in text, (
                f"{path} still mentions retired command 'fathom chart'"
            )
            assert "daily.md" not in text, (
                f"{path} still mentions retired job doc 'daily.md'"
            )

    def test_t08_only_on_retire_or_supersede_lines(self) -> None:
        for path in _OPERATOR_DOCS:
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                if re.search(r"(?<!P1A-)T-08", line) is None:
                    continue
                lowered = line.lower()
                assert "retire" in lowered or "supersede" in lowered, (
                    f"{path}:{lineno} mentions T-08 without retire/supersede: {line!r}"
                )


class TestIndexAmendments:
    """INDEX rows flipped by this teardown have executable wording checks."""

    def test_chart_generation_retired(self) -> None:
        row = _index_row(_read(REPO_ROOT / "docs" / "features" / "INDEX.md"), "chart-generation")
        assert "retired (phase-07)" in row

    def test_job_definitions_retired(self) -> None:
        feature = "her" "mes-job-definitions"
        row = _index_row(
            _read(REPO_ROOT / "docs" / "features" / "INDEX.md"), feature
        )
        assert "retired (phase-07)" in row

    def test_cli_commands_standalone_vocab(self) -> None:
        row = _index_row(_read(REPO_ROOT / "docs" / "features" / "INDEX.md"), "cli-commands")
        assert "chart" not in row
        assert "Her" "mes" not in row

    def test_monitor_alerts_direct_webhook(self) -> None:
        row = _index_row(_read(REPO_ROOT / "docs" / "features" / "INDEX.md"), "monitor-alerts")
        assert "Her" "mes" not in row
        assert "gateway" not in row.lower()
