"""Acceptance tests for pine-generation (phase-07 T1).

One test per auto AC (spec ACs 2–8). Variants of one behaviour are
table-driven. AC 1 (TradingView paste) is human_admin and is not gated here.
"""

from __future__ import annotations

import ast
import io
import re
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cli
from data.store import Store
from signals.pine import PineItem, levels_for, render_pine
from signals.ranker import Candidate

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_FIXED_NOW = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)


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
        "generated_at": "2026-04-10T13:00:00Z",
    }
    fields.update(overrides)
    return Candidate(**fields)


def _seed(
    db_path: str,
    candidates: list[Candidate],
    run_ts: datetime | None = None,
) -> None:
    store = Store(db_path)
    try:
        store.write_watchlist(
            candidates,
            run_timestamp=run_ts
            or datetime(2026, 4, 10, 13, 0, 0, tzinfo=timezone.utc),
        )
    finally:
        store.close()


def _run_pine(
    db_path: str,
    *,
    extra: list[str] | None = None,
    now: datetime = _FIXED_NOW,
) -> tuple[int, str, str]:
    argv = ["pine", "--db-path", db_path, "--no-clipboard", *(extra or [])]
    mock_settings = MagicMock()
    mock_settings.max_candidate_age_bars = 1.0
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch("cli.Settings", return_value=mock_settings),
        patch("cli._utc_now", return_value=now),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        rc = cli.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def test_ac2_level_arithmetic_direction_and_precision() -> None:
    """AC 2: LONG stop < entry < target; SHORT mirrored; prices at display precision."""
    rows = [
        (
            _candidate(
                direction="LONG",
                entry_ref=1.1050,
                stop_distance=0.0020,
                target_distance=0.0030,
                instrument="EUR_USD",
            ),
            1.1050,
            1.1030,
            1.1080,
            "1.10500",
            "1.10300",
            "1.10800",
        ),
        (
            _candidate(
                direction="SHORT",
                entry_ref=1.2600,
                stop_distance=0.0025,
                target_distance=0.0038,
                instrument="GBP_USD",
                rank=2,
            ),
            1.2600,
            1.2625,
            1.2562,
            "1.26000",
            "1.26250",
            "1.25620",
        ),
        (
            _candidate(
                direction="LONG",
                entry_ref=150.25,
                stop_distance=0.20,
                target_distance=0.40,
                instrument="USD_JPY",
                rank=3,
            ),
            150.25,
            150.05,
            150.65,
            "150.250",
            "150.050",
            "150.650",
        ),
    ]
    for cand, entry, stop, target, entry_s, stop_s, target_s in rows:
        got_entry, got_stop, got_target = levels_for(cand)
        assert got_entry == pytest.approx(entry)
        assert got_stop == pytest.approx(stop)
        assert got_target == pytest.approx(target)
        if cand.direction == "LONG":
            assert got_stop < got_entry < got_target
        else:
            assert got_target < got_entry < got_stop
        script = render_pine(
            [PineItem(candidate=cand, stale=False, reduce_size=False)]
        )
        assert entry_s in script
        assert stop_s in script
        assert target_s in script


def test_ac3_empty_watchlist_and_missing_db(tmp_path: Path) -> None:
    """AC 3: zero rows → compiling empty script, stderr, exit 0; missing db ≠ 0."""
    empty_db = str(tmp_path / "empty.db")
    Store(empty_db).close()
    rc, out, err = _run_pine(empty_db)
    assert rc == 0
    assert "//@version=6" in out
    assert 'indicator("Fathom watchlist"' in out
    assert "Fathom: no candidates" in out
    assert "Fathom: no candidates" in err
    assert _TS_RE.search(out) is None

    missing = str(tmp_path / "no-such.db")
    rc_m, _, err_m = _run_pine(missing)
    assert rc_m != 0
    assert "missing or unreadable" in err_m

    unreadable = tmp_path / "not-a-db"
    unreadable.mkdir()
    rc_u, _, err_u = _run_pine(str(unreadable))
    assert rc_u != 0
    assert "missing or unreadable" in err_u


def test_ac4_determinism_rank_order_stored_timestamps_only(tmp_path: Path) -> None:
    """AC 4: identical inputs → byte-identical scripts; rank order; stored ts only."""
    later = _candidate(
        rank=2,
        instrument="GBP_USD",
        generated_at="2026-04-10T13:30:00Z",
        strategy_name="rsi_reversion",
    )
    earlier = _candidate(
        rank=1,
        instrument="EUR_USD",
        generated_at="2026-04-10T13:00:00Z",
        strategy_name="donchian_breakout",
    )
    items = [
        PineItem(candidate=later, stale=False, reduce_size=False),
        PineItem(candidate=earlier, stale=False, reduce_size=False),
    ]
    a = render_pine(items)
    b = render_pine(list(reversed(items)))
    assert a == b
    assert a.index("donchian_breakout") < a.index("rsi_reversion")

    db_path = str(tmp_path / "det.db")
    run_ts = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    _seed(db_path, [later, earlier], run_ts=run_ts)
    rc1, out1, _ = _run_pine(db_path)
    rc2, out2, _ = _run_pine(db_path)
    assert rc1 == rc2 == 0
    assert out1 == out2
    found = set(_TS_RE.findall(out1))
    stored = {earlier.generated_at, later.generated_at}
    assert found <= stored
    assert "2026-04-10T12:00:00Z" not in out1


def test_ac5_clipboard_degradation(tmp_path: Path) -> None:
    """AC 5: missing pbcopy warns + succeeds; --no-clipboard never calls pbcopy."""
    db_path = str(tmp_path / "clip.db")
    _seed(db_path, [_candidate()])
    mock_settings = MagicMock()
    mock_settings.max_candidate_age_bars = 1.0

    stdout = io.StringIO()
    stderr = io.StringIO()
    run = MagicMock()
    with (
        patch("cli.Settings", return_value=mock_settings),
        patch("cli._utc_now", return_value=_FIXED_NOW),
        patch("cli.shutil.which", return_value=None),
        patch("cli.subprocess.run", run),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        rc = cli.main(["pine", "--db-path", db_path])
    assert rc == 0
    assert "//@version=6" in stdout.getvalue()
    assert "pbcopy" in stderr.getvalue()
    run.assert_not_called()

    stdout2 = io.StringIO()
    stderr2 = io.StringIO()
    run2 = MagicMock()
    which2 = MagicMock(return_value="/usr/bin/pbcopy")
    with (
        patch("cli.Settings", return_value=mock_settings),
        patch("cli._utc_now", return_value=_FIXED_NOW),
        patch("cli.shutil.which", which2),
        patch("cli.subprocess.run", run2),
        redirect_stdout(stdout2),
        redirect_stderr(stderr2),
    ):
        rc2 = cli.main(["pine", "--db-path", db_path, "--no-clipboard"])
    assert rc2 == 0
    assert "//@version=6" in stdout2.getvalue()
    assert "pbcopy" not in stderr2.getvalue()
    which2.assert_not_called()
    run2.assert_not_called()


def test_ac6_pine_module_forbidden_imports() -> None:
    """AC 6: signals/pine.py AST — no order/network/cli/AI surface; no subprocess."""
    path = Path(__file__).parent.parent / "signals" / "pine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = (
        "execution",
        "risk",
        "cli",
        "ai",
        "httpx",
        "oandapyV20",
        "urllib.request",
        "subprocess",
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for mod in imported:
        for bad in forbidden:
            assert not (mod == bad or mod.startswith(bad + ".")), (
                f"signals/pine.py imports {mod!r} (forbidden: {bad})"
            )


def test_ac7_per_candidate_staleness(tmp_path: Path) -> None:
    """AC 7: stale H1 labelled; fresh D not; STALE cell; stderr; exit 0."""
    h1 = _candidate(
        instrument="EUR_USD",
        timeframe="H1",
        generated_at="2026-04-10T12:00:00Z",
        strategy_name="donchian_h1",
        rank=1,
    )
    daily = _candidate(
        instrument="GBP_USD",
        timeframe="D",
        generated_at="2026-04-10T00:00:00Z",
        strategy_name="donchian_d",
        rank=2,
        direction="SHORT",
        entry_ref=1.2600,
        stop_distance=0.0025,
        target_distance=0.0038,
    )
    db_path = str(tmp_path / "stale.db")
    _seed(db_path, [h1, daily])
    rc, out, err = _run_pine(db_path)
    assert rc == 0
    h1_idx = out.index("donchian_h1")
    d_idx = out.index("donchian_d")
    assert "(stale)" in out[h1_idx : h1_idx + 80]
    assert "(stale)" not in out[d_idx : d_idx + 80]
    assert "STALE" in out
    assert "stale" in err.lower()


def test_ac8_analysis_join(tmp_path: Path) -> None:
    """AC 8: skip dropped + reduce_size marker; no analysis rows → full list."""
    skip_c = _candidate(
        instrument="EUR_USD",
        strategy_name="skip_me",
        rank=1,
    )
    reduce_c = _candidate(
        instrument="GBP_USD",
        strategy_name="size_me",
        rank=2,
        direction="SHORT",
        entry_ref=1.2600,
        stop_distance=0.0025,
        target_distance=0.0038,
    )
    keep_c = _candidate(
        instrument="AUD_USD",
        strategy_name="keep_me",
        rank=3,
        entry_ref=0.6600,
        stop_distance=0.0015,
        target_distance=0.0025,
    )
    db_path = str(tmp_path / "join.db")
    _seed(db_path, [skip_c, reduce_c, keep_c])
    store = Store(db_path)
    try:
        run_ts = store.latest_watchlist_run_ts()
    finally:
        store.close()

    analysis = [
        {
            "instrument": "EUR_USD",
            "timeframe": "H1",
            "strategy_name": "skip_me",
            "suggest_action": "skip",
        },
        {
            "instrument": "GBP_USD",
            "timeframe": "H1",
            "strategy_name": "size_me",
            "suggest_action": "reduce_size",
        },
        {
            "instrument": "AUD_USD",
            "timeframe": "H1",
            "strategy_name": "keep_me",
            "suggest_action": "proceed",
        },
    ]

    def _load(self: Store, watchlist_run: str | None = None) -> list[dict[str, str]]:
        assert watchlist_run == run_ts
        return analysis

    with patch.object(Store, "load_latest_analysis", _load, create=True):
        rc, out, _ = _run_pine(db_path)
    assert rc == 0
    assert "skip_me" not in out
    assert "size_me" in out
    assert "reduce size" in out
    assert "keep_me" in out

    rc2, out2, _ = _run_pine(db_path)
    assert rc2 == 0
    assert "skip_me" in out2
    assert "size_me" in out2
    assert "keep_me" in out2
    assert "reduce size" not in out2
