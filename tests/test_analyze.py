"""Acceptance tests for ``fathom analyze`` (phase-07 T5 / analyze-command).

One test per spec AC, plus one per invariant the ACs actually touch.
Variants of a single behaviour stay in one test.
"""

from __future__ import annotations

import ast
import io
import json
import re
import socket
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import cli
from data.calendar import FairEconomyCalendar
from data.store import Store
from signals.ranker import Candidate

_REPO_ROOT = Path(__file__).parent.parent.resolve()
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PLACEHOLDERS = (
    "{{instrument}}",
    "{{base_currency}}",
    "{{quote_currency}}",
    "{{direction}}",
    "{{entry_window_utc}}",
    "{{calendar_events}}",
)
_SAFE_DEFAULT_REASON = "unparseable response — defaulting to skip"
_ANALYSIS_COLUMNS = (
    "run_ts",
    "watchlist_ts",
    "instrument",
    "timeframe",
    "strategy_name",
    "event_risk",
    "suggest_action",
    "reason",
    "narration",
    "narration_source",
    "regime",
    "model_id",
)
_NFP_TITLE = "Non-Farm Payrolls"


def _candidate(**overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "instrument": "EUR_USD",
        "timeframe": "H1",
        "strategy_name": "donchian_breakout",
        "direction": "LONG",
        "entry_ref": 1.1050,
        "stop_distance": 0.0020,
        "target_distance": 0.0030,
        "oos_sharpe_mean": 1.5,
        "quality_score": 0.75,
        "rank": 1,
        "spread_ok": True,
        "session_ok": True,
        "news_flag": False,
        "generated_at": "2026-09-20T06:00:00Z",
    }
    fields.update(overrides)
    return Candidate(**fields)


def _three_candidates() -> list[Candidate]:
    return [
        _candidate(instrument="EUR_USD", strategy_name="proceed_me", rank=1),
        _candidate(
            instrument="GBP_USD",
            strategy_name="size_me",
            rank=2,
            direction="SHORT",
            entry_ref=1.2600,
            stop_distance=0.0025,
            target_distance=0.0038,
        ),
        _candidate(
            instrument="AUD_USD",
            strategy_name="skip_me",
            rank=3,
            entry_ref=0.6600,
            stop_distance=0.0015,
            target_distance=0.0025,
        ),
    ]


def _persist_watchlist(db_path: str, candidates: list[Candidate]) -> str | None:
    store = Store(db_path)
    try:
        run_ts = datetime(2026, 9, 20, 7, 0, 0, tzinfo=timezone.utc)
        store.write_watchlist(candidates, run_timestamp=run_ts)
        return store.latest_watchlist_run_ts()
    finally:
        store.close()


def _seed_calendar(db_path: str) -> None:
    cal = FairEconomyCalendar(db_path=db_path)
    event_time = datetime.now(timezone.utc) + timedelta(minutes=30)
    cal._conn.execute(
        """
        INSERT OR REPLACE INTO calendar_events
            (currency, event_name, time, impact, actual, forecast, previous)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "USD",
            _NFP_TITLE,
            event_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "high",
            None,
            None,
            None,
        ),
    )
    cal._conn.commit()
    cal._conn.close()


class _ScanStub:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        db_path: str,
        instruments: str = "ALL",
        timeframes: str = "H1,H4,D",
        history_years: int = 3,
        dry_run: bool = False,
    ) -> list[Candidate]:
        self.calls.append(
            {
                "db_path": db_path,
                "instruments": instruments,
                "timeframes": timeframes,
                "history_years": history_years,
                "dry_run": dry_run,
            }
        )
        _persist_watchlist(db_path, self.candidates)
        return list(self.candidates)


def _patch_scan(candidates: list[Candidate]) -> _ScanStub:
    return _ScanStub(candidates)


class _PipelineStub:
    """Stub LLM: session JSON, per-instrument news-risk, short narration."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        lowered = prompt.lower()
        if "advisory" in lowered and "session" in lowered:
            return json.dumps(
                {
                    "brief": {
                        "summary": "Quiet session.",
                        "landmines": ["USD NFP"],
                        "invalidators": ["Dollar spike"],
                    },
                    "session": {"verdict": "caution", "reasons": ["NFP nearby"]},
                    "regimes": {
                        "EUR_USD": "trending",
                        "GBP_USD": "ranging",
                        "AUD_USD": "pre_event",
                    },
                }
            )
        if "event_risk" in prompt and "suggest_action" in prompt:
            if "GBP_USD" in prompt:
                return json.dumps(
                    {
                        "event_risk": "medium",
                        "reason": "CPI nearby; reduce exposure",
                        "suggest_action": "reduce_size",
                    }
                )
            if "AUD_USD" in prompt:
                return json.dumps(
                    {
                        "event_risk": "high",
                        "reason": "RBA speaker in window",
                        "suggest_action": "skip",
                    }
                )
            return json.dumps(
                {
                    "event_risk": "low",
                    "reason": "quiet calendar",
                    "suggest_action": "proceed",
                }
            )
        return "Donchian long, clean structure."


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        rc = cli.main(argv)
    return rc, buf_out.getvalue(), buf_err.getvalue()


def _inject_client(stub: _PipelineStub) -> Any:
    import signals.analyze as analyze_mod

    real = analyze_mod.run_analysis

    def _wrapped(**kwargs: Any) -> Any:
        kwargs.setdefault("client", stub)
        return real(**kwargs)

    return _wrapped


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "analyze.db")


def test_ac1_stub_pipeline_splits_survivors_and_logs(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1: 3-candidate stub → six placeholders, split, reduce_size, one log row."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    candidates = _three_candidates()
    _seed_calendar(db_path)
    stub = _PipelineStub()
    fake_scan = _patch_scan(candidates)

    from signals.analyze import run_analysis

    with patch("signals.analyze.run_scan", fake_scan):
        result = run_analysis(db_path=db_path, dry_run=True, client=stub)

    news_prompts = [
        p for p in stub.prompts if "event_risk" in p and "suggest_action" in p
    ]
    assert len(news_prompts) == 3
    for prompt in news_prompts:
        for leftover in _PLACEHOLDERS:
            assert leftover not in prompt
        assert _NFP_TITLE in prompt

    survivor_names = {row.candidate.strategy_name for row in result.survivors}
    vetoed_names = {row.candidate.strategy_name for row in result.vetoed}
    assert survivor_names == {"proceed_me", "size_me"}
    assert vetoed_names == {"skip_me"}
    reduce = next(
        row for row in result.survivors if row.candidate.strategy_name == "size_me"
    )
    assert reduce.verdict.suggest_action == "reduce_size"
    assert result.vetoed[0].narration is None
    assert result.vetoed[0].narration_source == "none"

    store = Store(db_path)
    try:
        watchlist_ts = store.latest_watchlist_run_ts()
        rows = store.load_latest_analysis(watchlist_ts)
    finally:
        store.close()
    assert len(rows) == 3

    wrapped = _inject_client(stub)
    with (
        patch("signals.analyze.run_scan", fake_scan),
        patch("signals.analyze.run_analysis", wrapped),
        patch("cli._copy_script_to_clipboard", lambda script: None),
    ):
        rc, out, _ = _run_cli(["analyze", "--db-path", db_path, "--dry-run"])
    assert rc == 0
    assert "⚠ reduce size" in out
    pine = out[out.index("//@version") :]
    assert "proceed_me" in pine
    assert "size_me" in pine
    assert "reduce size" in pine
    assert "skip_me" not in pine


def test_ac2_offline_all_vetoed_zero_network(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2 / INV-02 / INV-20: no key, no client → all skip, zero LLM-pipeline I/O."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    sock_calls = {"n": 0}

    class _GuardedSocket(socket.socket):
        def __init__(self, *args: Any, **kw: Any) -> None:
            sock_calls["n"] += 1
            raise AssertionError("network I/O on analyze offline path")

    class _NoHttpx:
        def __init__(self, *args: Any, **kw: Any) -> None:
            raise AssertionError("httpx Client constructed on offline path")

    monkeypatch.setattr(socket, "socket", _GuardedSocket)
    monkeypatch.setattr("httpx.Client", _NoHttpx)
    fake_scan = _patch_scan(_three_candidates())

    from signals.analyze import run_analysis

    with patch("signals.analyze.run_scan", fake_scan):
        result = run_analysis(db_path=db_path, dry_run=True, client=None)

    assert result.survivors == []
    assert len(result.vetoed) == 3
    for row in result.vetoed:
        assert row.verdict.suggest_action == "skip"
        assert _SAFE_DEFAULT_REASON in row.verdict.reason
        assert row.narration_source == "none"
    assert result.session is not None
    assert "analysis unavailable" in result.session.brief.summary
    assert sock_calls["n"] == 0

    with (
        patch("signals.analyze.run_scan", fake_scan),
        patch("cli._copy_script_to_clipboard", lambda script: None),
    ):
        rc, out, _ = _run_cli(
            ["analyze", "--db-path", db_path, "--dry-run", "--no-pine"]
        )
    assert rc == 0
    assert "analysis unavailable" in out


def test_ac3_empty_watchlist_no_llm_no_log(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3 / INV-10: empty scan → message, zero LLM, zero analysis_log, exit 0."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    stub = _PipelineStub()
    fake_scan = _patch_scan([])

    from signals.analyze import run_analysis

    with patch("signals.analyze.run_scan", fake_scan):
        result = run_analysis(db_path=db_path, dry_run=True, client=stub)

    assert stub.prompts == []
    assert result.survivors == []
    assert result.vetoed == []
    store = Store(db_path)
    try:
        cur = store._conn.execute("SELECT COUNT(*) FROM analysis_log")
        assert cur.fetchone()[0] == 0
    finally:
        store.close()

    with patch("signals.analyze.run_scan", fake_scan):
        rc, out, _ = _run_cli(
            ["analyze", "--db-path", db_path, "--dry-run", "--no-pine"]
        )
    assert rc == 0
    assert "no candidates" in out.lower()
    assert "INV-10" in out


def test_ac4_analysis_log_wire_format_append_only_utc(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 4 / INV-03 / INV-22: wire columns, RFC-3339, INSERT-only, join guard."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    stub = _PipelineStub()
    fake_scan = _patch_scan(_three_candidates())

    from signals.analyze import run_analysis

    with patch("signals.analyze.run_scan", fake_scan):
        first = run_analysis(db_path=db_path, dry_run=True, client=stub)
        second = run_analysis(db_path=db_path, dry_run=True, client=stub)

    assert first.run_ts != second.run_ts
    store = Store(db_path)
    try:
        assert not hasattr(store, "update_analysis")
        src = (_REPO_ROOT / "data" / "store.py").read_text(encoding="utf-8")
        assert "UPDATE analysis_log" not in src
        assert re.search(
            r"INSERT OR REPLACE INTO\s+analysis_log", src, re.IGNORECASE
        ) is None

        watchlist_ts = store.latest_watchlist_run_ts()
        rows = store.load_latest_analysis(watchlist_ts)
        assert len(rows) == 3
        for row in rows:
            for col in _ANALYSIS_COLUMNS:
                assert col in row
            assert _RFC3339.match(str(row["run_ts"]))
            assert _RFC3339.match(str(row["watchlist_ts"]))
            assert row["run_ts"] == second.run_ts
            assert row["suggest_action"] in {"proceed", "reduce_size", "skip"}
            assert row["event_risk"] in {"high", "medium", "low"}
            assert row["narration_source"] in {"model", "fallback", "none"}
            if row["suggest_action"] == "skip":
                assert row["narration"] is None
                assert row["narration_source"] == "none"
            else:
                assert row["narration"]
                assert row["narration_source"] in {"model", "fallback"}

        assert store.load_latest_analysis("1999-01-01T00:00:00Z") == []
        cur = store._conn.execute("SELECT COUNT(*) FROM analysis_log")
        assert cur.fetchone()[0] == 6
    finally:
        store.close()


def test_ac5_analyze_module_order_free_ast() -> None:
    """AC 5 / INV-01: signals/analyze.py imports none of execution.*, risk.*, cli."""
    path = _REPO_ROOT / "signals" / "analyze.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = ("execution", "risk", "cli")
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                for prefix in forbidden:
                    if mod == prefix or mod.startswith(prefix + "."):
                        violations.append(f"import {mod}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for prefix in forbidden:
                if mod == prefix or mod.startswith(prefix + "."):
                    violations.append(f"from {mod} import ...")
    assert violations == [], f"INV-01 violations in signals/analyze.py: {violations}"


def test_ac6_dry_run_reaches_run_scan_and_annotates(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 6: --dry-run → run_scan(dry_run=True); annotation pipeline still runs."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    fake_scan = _patch_scan(_three_candidates())
    with (
        patch("signals.analyze.run_scan", fake_scan),
        patch("cli._copy_script_to_clipboard", lambda script: None),
    ):
        rc, _, _ = _run_cli(
            [
                "analyze",
                "--db-path",
                db_path,
                "--dry-run",
                "--instruments",
                "EUR_USD,GBP_USD",
                "--timeframes",
                "H1,H4",
                "--no-pine",
            ]
        )
    assert rc == 0
    assert fake_scan.calls
    call = fake_scan.calls[0]
    assert call["dry_run"] is True
    assert call["instruments"] == "EUR_USD,GBP_USD"
    assert call["timeframes"] == "H1,H4"
    store = Store(db_path)
    try:
        n = store._conn.execute("SELECT COUNT(*) FROM analysis_log").fetchone()[0]
    finally:
        store.close()
    assert n == 3


def test_ac7_emitted_pine_is_survivor_set(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 7: absent --no-pine, Pine contains exactly the survivor set."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    stub = _PipelineStub()
    fake_scan = _patch_scan(_three_candidates())
    wrapped = _inject_client(stub)
    with (
        patch("signals.analyze.run_scan", fake_scan),
        patch("signals.analyze.run_analysis", wrapped),
        patch("cli._copy_script_to_clipboard", lambda script: None),
    ):
        rc, out, _ = _run_cli(["analyze", "--db-path", db_path, "--dry-run"])
    assert rc == 0
    assert "//@version=6" in out
    pine = out[out.index("//@version") :]
    assert "AUDUSD" not in pine
    assert "skip_me" not in pine
    assert "EURUSD" in pine
    assert "GBPUSD" in pine


def test_ac8_scan_and_watchlist_unchanged(db_path: str) -> None:
    """AC 8: fathom scan / watchlist still empty-INV-10 JSON paths."""
    scan_rc, scan_out, _ = _run_cli(
        ["scan", "--db-path", db_path, "--dry-run", "--instruments", "EUR_USD"]
    )
    assert scan_rc == 0
    assert "INV-10" in scan_out or scan_out.strip().startswith("[")
    wl_rc, wl_out, _ = _run_cli(["watchlist", "--db-path", db_path])
    assert wl_rc == 0
    payload = json.loads(wl_out)
    assert payload == []


def test_inv08_api_key_absent_from_analyze_output(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-08: LLM_API_KEY never appears in analyze stdout/stderr."""
    secret = "sk-test-never-print-this-key"
    monkeypatch.setenv("LLM_API_KEY", secret)
    fake_scan = _patch_scan(_three_candidates())
    stub = _PipelineStub()
    wrapped = _inject_client(stub)
    with (
        patch("signals.analyze.run_scan", fake_scan),
        patch("signals.analyze.run_analysis", wrapped),
        patch("cli._copy_script_to_clipboard", lambda script: None),
    ):
        rc, out, err = _run_cli(
            ["analyze", "--db-path", db_path, "--dry-run", "--no-pine"]
        )
    assert rc == 0
    assert secret not in out
    assert secret not in err


def test_inv09_live_env_skips_veto_ledger(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-09: ENV=live still writes analysis_log; never writes veto_ledger."""
    monkeypatch.setenv("ENV", "live")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    stub = _PipelineStub()
    fake_scan = _patch_scan(_three_candidates())
    from signals.analyze import run_analysis

    with patch("signals.analyze.run_scan", fake_scan):
        run_analysis(db_path=db_path, dry_run=True, client=stub)

    store = Store(db_path)
    try:
        n = store._conn.execute("SELECT COUNT(*) FROM analysis_log").fetchone()[0]
        assert n == 3
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "veto_ledger" not in tables
    finally:
        store.close()


def test_inv21_bar_length_single_definition_site() -> None:
    """INV-21: TIMEFRAME_BAR_LENGTH is defined only in signals/timeframes.py."""
    from signals.timeframes import TIMEFRAME_BAR_LENGTH

    assert TIMEFRAME_BAR_LENGTH["H1"] == timedelta(hours=1)
    assert TIMEFRAME_BAR_LENGTH["H4"] == timedelta(hours=4)
    assert TIMEFRAME_BAR_LENGTH["D"] == timedelta(hours=24)
    cli_src = (_REPO_ROOT / "cli.py").read_text(encoding="utf-8")
    assert "TIMEFRAME_BAR_LENGTH: dict" not in cli_src
    assert "from signals.timeframes import TIMEFRAME_BAR_LENGTH" in cli_src
    tf_src = (_REPO_ROOT / "signals" / "timeframes.py").read_text(encoding="utf-8")
    tree = ast.parse(tf_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name == "datetime"
        elif isinstance(node, ast.ImportFrom):
            assert node.module in {"datetime", "__future__"}
