"""Tests for the live-trading gate (P5-T-02) — the real-money safety gate.

Two layers:

1. **Pure unit tests** of ``execution.live_gate`` — the full 16-row truth table
   over the four booleans, the B-1 default-refuse rows (None / non-PreflightReport
   / ``.go`` non-True preflight), the demo no-op, and ``effective_risk_fraction``
   live/demo selection + the INV-05 settings-time validation bounds.

2. **CLI wiring tests** of ``cli.cmd_execute`` — the demo path is byte-identical
   (no preflight, no typed confirm, ``size_position`` gets exactly 0.0025); live
   ``--yes`` STILL requires the typed account-id confirm (N-3); a wrong/empty
   confirm refuses with no order; a ``run_preflight`` exception in the live path
   refuses (no order), never GO; and a live ``size_position`` receives
   ``live_risk_fraction`` (B-2).

3. **INV-09 enforcement** — a source scan asserting no ``env``-aware branch /
   ``settings.env`` read exists in sizing / orders / reconcile / monitor.

NO live endpoint / token anywhere: ``Settings`` is a stub, the client is mocked,
``run_preflight`` is patched.  The suite never requires the live token (INV-07/08).
"""

from __future__ import annotations

import argparse
import io
import re
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from config.settings import Settings

import pytest

import cli
from data.store import Store
from data.oanda_client import InstrumentMeta
from signals.ranker import Candidate
from execution.live_gate import (
    LiveTradingBlocked,
    assert_live_allowed,
    effective_risk_fraction,
)
from execution.preflight import CheckResult, PreflightReport
from execution.reconcile import ReconcileReport
from risk.sizing import DEFAULT_RISK_FRACTION, SizingResult


# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------


def _settings(
    *,
    env: str = "live",
    live_trading_enabled: bool = True,
    live_risk_fraction: float = 0.001,
    oanda_account_id: str = "001-001-1234567-001",
) -> "Settings":
    """A duck-typed Settings stub — no live token, no I/O (INV-07/08).

    The gate only reads ``env`` / ``live_trading_enabled`` / ``live_risk_fraction``
    / ``oanda_account_id``, so a ``SimpleNamespace`` with those attributes is a
    faithful stand-in.  Cast to ``Settings`` so the call sites type-check without
    requiring a real (token-bearing) ``Settings`` instance.
    """
    return cast(
        "Settings",
        SimpleNamespace(
            env=env,
            live_trading_enabled=live_trading_enabled,
            live_risk_fraction=live_risk_fraction,
            oanda_account_id=oanda_account_id,
        ),
    )


def _preflight(go: bool) -> PreflightReport:
    return PreflightReport(
        go=go,
        checks=[CheckResult(name="x", ok=go, detail="stub")],
        checked_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. assert_live_allowed — full truth table (4 booleans = 16 rows)
# ---------------------------------------------------------------------------


class TestAssertLiveAllowedTruthTable:
    @pytest.mark.parametrize("env_live", [True, False])
    @pytest.mark.parametrize("flag", [True, False])
    @pytest.mark.parametrize("preflight_go", [True, False])
    @pytest.mark.parametrize("confirmed", [True, False])
    def test_truth_table(
        self,
        env_live: bool,
        flag: bool,
        preflight_go: bool,
        confirmed: bool,
    ) -> None:
        settings = _settings(
            env="live" if env_live else "demo",
            live_trading_enabled=flag,
        )
        report = _preflight(preflight_go)

        # On demo (env != "live") the gate is ALWAYS a no-op, regardless of the
        # other three booleans.  On live it allows ONLY when all four are True.
        should_allow = (not env_live) or (flag and preflight_go and confirmed)

        if should_allow:
            # No exception == allowed (the function returns None on success).
            assert_live_allowed(
                settings=settings,
                preflight_report=report,
                confirmed=confirmed,
            )
        else:
            with pytest.raises(LiveTradingBlocked):
                assert_live_allowed(
                    settings=settings,
                    preflight_report=report,
                    confirmed=confirmed,
                )

    def test_all_four_true_allows(self) -> None:
        # No exception == allowed.
        assert_live_allowed(
            settings=_settings(env="live", live_trading_enabled=True),
            preflight_report=_preflight(True),
            confirmed=True,
        )

    def test_reason_names_first_failing_gate_flag(self) -> None:
        with pytest.raises(LiveTradingBlocked, match="live_trading_enabled"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=False),
                preflight_report=_preflight(True),
                confirmed=True,
            )

    def test_reason_names_preflight_when_flag_ok(self) -> None:
        with pytest.raises(LiveTradingBlocked, match="preflight"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=True),
                preflight_report=_preflight(False),
                confirmed=True,
            )

    def test_reason_names_confirmation_when_rest_ok(self) -> None:
        with pytest.raises(LiveTradingBlocked, match="confirmation"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=True),
                preflight_report=_preflight(True),
                confirmed=False,
            )


# ---------------------------------------------------------------------------
# 1b. B-1 default-refuse on bad preflight_report
# ---------------------------------------------------------------------------


class TestDefaultRefuseBadPreflight:
    def test_none_preflight_refuses(self) -> None:
        with pytest.raises(LiveTradingBlocked, match="preflight"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=True),
                preflight_report=None,
                confirmed=True,
            )

    @pytest.mark.parametrize(
        "bad", [object(), {"go": True}, "GO", 1, True, SimpleNamespace(go=True)]
    )
    def test_non_preflightreport_refuses(self, bad: object) -> None:
        # Even a duck-typed object with .go == True is refused: it is not a
        # PreflightReport instance (default-refuse, B-1).
        with pytest.raises(LiveTradingBlocked, match="preflight"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=True),
                preflight_report=bad,
                confirmed=True,
            )

    @pytest.mark.parametrize("go_value", [False, None, 1, "True", 0])
    def test_go_not_exactly_true_refuses(self, go_value: object) -> None:
        # A genuine PreflightReport whose .go is not exactly True must refuse.
        # PreflightReport.go is typed bool, so construct then mutate via a stub
        # subclass to exercise non-True truthy/falsy values.
        report = _preflight(True)
        object.__setattr__(report, "go", go_value)
        with pytest.raises(LiveTradingBlocked, match="preflight"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=True),
                preflight_report=report,
                confirmed=True,
            )

    def test_demo_noop_even_with_bad_preflight(self) -> None:
        # On demo the gate is a no-op — even None preflight + unconfirmed.
        assert_live_allowed(
            settings=_settings(env="demo", live_trading_enabled=False),
            preflight_report=None,
            confirmed=False,
        )

    @pytest.mark.parametrize("conf", [False, None, 1, "yes", 0])
    def test_confirmed_not_exactly_true_refuses(self, conf: object) -> None:
        with pytest.raises(LiveTradingBlocked, match="confirmation"):
            assert_live_allowed(
                settings=_settings(env="live", live_trading_enabled=True),
                preflight_report=_preflight(True),
                confirmed=conf,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# 2. effective_risk_fraction + INV-05 settings validation
# ---------------------------------------------------------------------------


class TestEffectiveRiskFraction:
    def test_live_returns_live_fraction(self) -> None:
        s = _settings(env="live", live_risk_fraction=0.001)
        assert effective_risk_fraction(s) == 0.001

    def test_demo_returns_default_cap(self) -> None:
        s = _settings(env="demo", live_risk_fraction=0.001)
        assert effective_risk_fraction(s) == DEFAULT_RISK_FRACTION
        assert DEFAULT_RISK_FRACTION == 0.0025

    def test_live_fraction_never_above_cap_for_any_valid_value(self) -> None:
        # effective_risk_fraction just selects; the cap is enforced by Settings.
        s = _settings(env="live", live_risk_fraction=0.0025)
        assert effective_risk_fraction(s) <= DEFAULT_RISK_FRACTION


class TestSettingsValidationINV05:
    """The Field(gt=0.0, le=0.0025) bound rejects out-of-range live fractions."""

    def _env(self, **extra: str) -> dict[str, str]:
        base = {
            "OANDA_API_TOKEN": "stub-not-a-real-token",
            "OANDA_ACCOUNT_ID": "001-001-1234567-001",
        }
        base.update(extra)
        return base

    def test_accepts_default(self) -> None:
        from config.settings import Settings

        with patch.dict("os.environ", self._env(), clear=True):
            s = Settings(_env_file=None)
        assert s.live_risk_fraction == 0.001

    def test_rejects_above_cap(self) -> None:
        from pydantic import ValidationError
        from config.settings import Settings

        with patch.dict(
            "os.environ", self._env(LIVE_RISK_FRACTION="0.01"), clear=True
        ):
            with pytest.raises(ValidationError):
                Settings(_env_file=None)

    def test_rejects_zero_or_negative(self) -> None:
        from pydantic import ValidationError
        from config.settings import Settings

        for bad in ("0.0", "-0.001"):
            with patch.dict(
                "os.environ", self._env(LIVE_RISK_FRACTION=bad), clear=True
            ):
                with pytest.raises(ValidationError):
                    Settings(_env_file=None)

    def test_accepts_exact_cap(self) -> None:
        from config.settings import Settings

        with patch.dict(
            "os.environ", self._env(LIVE_RISK_FRACTION="0.0025"), clear=True
        ):
            s = Settings(_env_file=None)
        assert s.live_risk_fraction == 0.0025


# ---------------------------------------------------------------------------
# 3. cmd_execute wiring — shared helpers (mirror test_execution_cli.py)
# ---------------------------------------------------------------------------


def _utc(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _make_candidate() -> Candidate:
    return Candidate(
        instrument="EUR_USD",
        timeframe="H1",
        strategy_name="macrossover_10_50",
        direction="LONG",
        entry_ref=1.1050,
        stop_distance=0.0020,
        target_distance=0.0030,
        oos_sharpe_mean=1.5,
        quality_score=0.75,
        rank=1,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at="2026-04-10T12:00:00Z",
    )


def _make_instrument_meta() -> InstrumentMeta:
    return InstrumentMeta(
        name="EUR_USD",
        pip_location=-4,
        min_trade_size=1.0,
        margin_rate=0.02,
        display_precision=5,
        long_rate=0.0001,
        short_rate=-0.0002,
        financing_days_of_week=[2],
    )


def _seed(db_path: str) -> None:
    store = Store(db_path)
    store.write_watchlist([_make_candidate()], run_timestamp=_utc(2026, 4, 10, 10))
    store.write_account_state(
        start_of_day_equity=100_000.0, day_pl=0.0, as_of=_utc(2026, 4, 10, 10)
    )
    store.upsert_instruments([_make_instrument_meta()])
    store.close()


def _recon_report() -> ReconcileReport:
    r = ReconcileReport()
    r.start_of_day_equity = 100_000.0
    r.day_pl = 0.0
    return r


def _namespace(db_path: str, *, yes: bool = True, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        command="execute",
        candidate_ref="EUR_USD:H1:macrossover_10_50",
        db_path=db_path,
        dry_run=dry_run,
        yes=yes,
    )


# ---------------------------------------------------------------------------
# 3a. Demo path byte-identical: no preflight, no typed confirm, 0.0025
# ---------------------------------------------------------------------------


class TestDemoPathUnchanged:
    def test_demo_size_position_gets_exactly_default_and_no_preflight(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "demo.db")
        _seed(db_path)

        captured: dict[str, object] = {}

        def spy_size(candidate, equity, *, instrument_meta, rate=1.0, risk_fraction=DEFAULT_RISK_FRACTION):  # type: ignore[no-untyped-def]
            captured["risk_fraction"] = risk_fraction
            return SizingResult(units=0, risk_amount=0.0, reason="spy stop")

        from hermes_integration.pretrade_check import PretradeVerdict

        demo_settings = _settings(env="demo")

        with (
            patch("cli.Settings", return_value=demo_settings),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_recon_report()),
            patch("cli.pretrade_check", return_value=PretradeVerdict(decision="proceed", reason="ok")),
            patch("cli.size_position", side_effect=spy_size),
            patch("cli.run_preflight") as mock_pf,
            patch("cli.assert_live_allowed") as mock_gate,
            patch("builtins.input") as mock_input,
        ):
            args = _namespace(db_path, dry_run=True)
            with redirect_stderr(io.StringIO()):
                code = cli.cmd_execute(args)

        # Demo: gate machinery never touched; size_position gets exactly 0.0025.
        assert captured["risk_fraction"] == DEFAULT_RISK_FRACTION == 0.0025
        mock_pf.assert_not_called()
        mock_gate.assert_not_called()
        mock_input.assert_not_called()  # no typed confirm on demo
        assert code != 0  # spy rejection — but proves we reached sizing on demo


# ---------------------------------------------------------------------------
# 3b. Live path: typed confirm required, not --yes bypassable (N-3); B-2
# ---------------------------------------------------------------------------


class TestLivePathGate:
    def _live_patches(
        self, *, account_id: str = "001-001-1234567-001"
    ) -> tuple["Settings", object]:
        from hermes_integration.pretrade_check import PretradeVerdict

        live_settings = _settings(
            env="live",
            live_trading_enabled=True,
            live_risk_fraction=0.001,
            oanda_account_id=account_id,
        )
        return live_settings, PretradeVerdict(decision="proceed", reason="ok")

    def test_live_yes_still_requires_typed_confirm_and_threads_fraction(
        self, tmp_path: Path
    ) -> None:
        """N-3: live --yes STILL prompts; correct id → proceeds; B-2: 0.001."""
        db_path = str(tmp_path / "live_ok.db")
        _seed(db_path)
        live_settings, proceed = self._live_patches()

        captured: dict[str, object] = {}

        def spy_size(candidate, equity, *, instrument_meta, rate=1.0, risk_fraction=DEFAULT_RISK_FRACTION):  # type: ignore[no-untyped-def]
            captured["risk_fraction"] = risk_fraction
            return SizingResult(units=0, risk_amount=0.0, reason="spy stop")

        with (
            patch("cli.Settings", return_value=live_settings),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_recon_report()),
            patch("cli.run_preflight", return_value=_preflight(True)) as mock_pf,
            patch("cli.pretrade_check", return_value=proceed),
            patch("cli.size_position", side_effect=spy_size),
            patch("builtins.input", return_value="001-001-1234567-001") as mock_input,
        ):
            # yes=True must NOT bypass the typed confirm.
            args = _namespace(db_path, yes=True, dry_run=True)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.cmd_execute(args)

        mock_pf.assert_called_once()  # live ran preflight
        mock_input.assert_called_once()  # typed confirm prompted despite --yes
        # B-2: live size_position received live_risk_fraction (0.001), not 0.0025.
        assert captured["risk_fraction"] == 0.001
        assert code != 0  # stopped at the spy sizing rejection (no order)

    @pytest.mark.parametrize("typed", ["", "wrong-account", "001-001-1234567-002"])
    def test_live_wrong_or_empty_confirm_refuses_no_order(
        self, tmp_path: Path, typed: str
    ) -> None:
        db_path = str(tmp_path / "live_wrong.db")
        _seed(db_path)
        live_settings, proceed = self._live_patches()

        submit = MagicMock(name="submit_order")
        size = MagicMock(name="size_position")

        with (
            patch("cli.Settings", return_value=live_settings),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_recon_report()),
            patch("cli.run_preflight", return_value=_preflight(True)),
            patch("cli.pretrade_check", return_value=proceed),
            patch("cli.size_position", size),
            patch("cli.submit_order", submit),
            patch("builtins.input", return_value=typed),
        ):
            args = _namespace(db_path, yes=True, dry_run=False)
            buf = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf):
                code = cli.cmd_execute(args)

        assert code != 0
        assert "LIVE REFUSED" in buf.getvalue()
        submit.assert_not_called()  # no order
        size.assert_not_called()  # refused BEFORE sizing

    def test_live_flag_false_refuses(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "live_flag.db")
        _seed(db_path)
        live_settings = _settings(
            env="live", live_trading_enabled=False, oanda_account_id="001-001-1234567-001"
        )
        from hermes_integration.pretrade_check import PretradeVerdict

        submit = MagicMock(name="submit_order")

        with (
            patch("cli.Settings", return_value=live_settings),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_recon_report()),
            patch("cli.run_preflight", return_value=_preflight(True)),
            patch("cli.pretrade_check", return_value=PretradeVerdict(decision="proceed", reason="ok")),
            patch("cli.submit_order", submit),
            patch("builtins.input", return_value="001-001-1234567-001"),
        ):
            args = _namespace(db_path, yes=True, dry_run=False)
            buf = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf):
                code = cli.cmd_execute(args)

        assert code != 0
        assert "live_trading_enabled" in buf.getvalue()
        submit.assert_not_called()

    def test_live_preflight_exception_refuses_no_order_never_go(
        self, tmp_path: Path
    ) -> None:
        """B-1: a run_preflight EXCEPTION in the live path → refuse, no order."""
        db_path = str(tmp_path / "live_exc.db")
        _seed(db_path)
        live_settings, proceed = self._live_patches()

        submit = MagicMock(name="submit_order")
        size = MagicMock(name="size_position")
        gate = MagicMock(name="assert_live_allowed")

        with (
            patch("cli.Settings", return_value=live_settings),
            patch("cli.OandaClient"),
            patch("cli.reconcile", return_value=_recon_report()),
            patch("cli.run_preflight", side_effect=RuntimeError("boom")),
            patch("cli.pretrade_check", return_value=proceed),
            patch("cli.size_position", size),
            patch("cli.submit_order", submit),
            patch("cli.assert_live_allowed", gate),
            patch("builtins.input") as mock_input,
        ):
            args = _namespace(db_path, yes=True, dry_run=False)
            buf = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf):
                code = cli.cmd_execute(args)

        assert code != 0
        assert "LIVE REFUSED" in buf.getvalue()
        submit.assert_not_called()  # never GO — no order
        size.assert_not_called()
        gate.assert_not_called()  # refused before even reaching the gate
        mock_input.assert_not_called()  # no confirm after a preflight failure


# ---------------------------------------------------------------------------
# 4. INV-09 enforcement — no env-aware branch in mechanics modules
# ---------------------------------------------------------------------------


class TestINV09NoEnvBranchInMechanics:
    MECHANICS = [
        "risk/sizing.py",
        "execution/orders.py",
        "execution/reconcile.py",
        "monitoring/watcher.py",
    ]

    # Patterns that would indicate an env-aware branch in mechanics code.
    # After _code_only normalisation: spaces around '.' and '==' are removed,
    # and string literals are gone (so 'live'/'demo' compare-RHS vanish — an
    # `env == "live"` collapses to `env==` in code-only form).
    FORBIDDEN = [
        re.compile(r"settings\.env"),
        re.compile(r"\benv=="),
        re.compile(r"==env\b"),
    ]

    def _repo_root(self) -> Path:
        return Path(cli.__file__).resolve().parent

    @staticmethod
    def _code_only(src: str) -> str:
        """Return source with comments and string literals (incl. docstrings)
        removed, so the scan only sees executable code — a docstring that *says*
        'never reads env' must not trip the enforcement."""
        import io as _io
        import tokenize as _tok

        out: list[str] = []
        toks = _tok.generate_tokens(_io.StringIO(src).readline)
        for tok in toks:
            if tok.type in (_tok.COMMENT, _tok.STRING):
                continue
            out.append(tok.string)
        joined = " ".join(out)
        # Normalise spacing the tokenizer introduces so attribute access and
        # comparisons match regardless of original formatting.
        joined = re.sub(r"\s*\.\s*", ".", joined)
        joined = re.sub(r"\s*==\s*", "==", joined)
        return joined

    def test_no_env_branch_in_mechanics(self) -> None:
        root = self._repo_root()
        for rel in self.MECHANICS:
            path = root / rel
            assert path.exists(), f"expected mechanics module missing: {rel}"
            code = self._code_only(path.read_text(encoding="utf-8"))
            # The mechanics modules must not reference settings.env / branch on
            # env at all in executable code (INV-09 operator-boundary clause).
            for pat in self.FORBIDDEN:
                assert not pat.search(code), (
                    f"INV-09 BREACH: env-aware branch in {rel}: matched {pat.pattern!r}"
                )

    def test_live_gate_is_the_sanctioned_env_reader(self) -> None:
        # Sanity: the gate module IS allowed to read settings.env (it is the
        # operator-boundary exception).  Confirm it does, so the enforcement
        # test above is meaningfully distinguishing the two.
        from pathlib import Path as _P

        gate_src = (_P(cli.__file__).resolve().parent / "execution/live_gate.py").read_text()
        assert "settings.env" in gate_src
