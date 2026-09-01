"""Doc-lint guard (WS0-T07): dead names must never reappear in operator docs.

Plain file-content assertions — no markdown parsing needed.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestNoDeadEnvVarNames:
    """OANDA_API_KEY / OANDA_ENV were renamed to OANDA_API_TOKEN / ENV."""

    def test_hermes_jobs_docs_do_not_use_dead_env_var_names(self) -> None:
        jobs_dir = REPO_ROOT / "hermes_integration" / "jobs"
        for md_path in jobs_dir.glob("*.md"):
            text = _read(md_path)
            assert "OANDA_API_KEY" not in text, (
                f"{md_path} still references dead env var OANDA_API_KEY "
                "(use OANDA_API_TOKEN)"
            )
            assert "OANDA_ENV" not in text, (
                f"{md_path} still references dead env var OANDA_ENV "
                "(use ENV)"
            )

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
