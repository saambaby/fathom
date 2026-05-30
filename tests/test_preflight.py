"""Tests for P5-T-03 preflight-check: run_preflight + fathom preflight CLI.

Design (NO live HTTP)
---------------------
All tests drive ``execution.preflight.run_preflight`` directly, or
``cli.cmd_preflight``/``cli.main`` for CLI tests.  The OANDA client is always
stubbed (no live HTTP, INV-07/INV-08).  The store is an in-memory SQLite
instance seeded per-test.

Coverage
--------
1.  go=True only when ALL checks pass AND attested=True (AC-1).
2.  attested=False → NO-GO with INV-07 reason (AC-2).
3.  Kill-switch NO-GO when account_state is None (missing) (AC-3a).
4.  Kill-switch NO-GO when account_state.as_of is stale (>10 min) (AC-3b).
5.  Kill-switch NO-GO when kill switch is tripped (AC-3c).
6.  Kill-switch GO when present + fresh + not tripped (AC-3d).
7.  ENV=live without token → NO-GO, clearly reported (AC-4a).
8.  ENV=live without account ID → NO-GO, clearly reported (AC-4b).
9.  ENV=live with live_trading_enabled=False → NO-GO (AC-4c).
10. ENV=demo always consistent (AC-4d).
11. Account reachable: stub client success → PASS; exception → FAIL.
12. No client provided → account_reachable FAIL (not PASS).
13. INV-04 static bracket contract: check is PASS in normal builds.
14. CLI exits 0 on GO; exits 1 on NO-GO (AC-5).
15. Token never printed in report details (INV-08).
16. All timestamps UTC (INV-03).
17. Read-only: no order/write method reachable from run_preflight.
18. fathom preflight --help works.
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional
from unittest.mock import MagicMock, patch

import pytest

import cli
from data.store import Store
from execution.preflight import PreflightReport, run_preflight


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN = "fake-token-not-real"
FAKE_ACCOUNT_ID = "101-001-999-1"


def _utc(**kwargs: int) -> datetime:
    """Build a UTC-aware datetime from keyword args, e.g. year=2026, month=5."""
    return datetime(
        kwargs.get("year", 2026),
        kwargs.get("month", 1),
        kwargs.get("day", 1),
        kwargs.get("hour", 12),
        kwargs.get("minute", 0),
        kwargs.get("second", 0),
        tzinfo=timezone.utc,
    )


def _make_settings(
    env: str = "demo",
    token: str = FAKE_TOKEN,
    account_id: str = FAKE_ACCOUNT_ID,
    live_trading_enabled: bool = False,
) -> MagicMock:
    """Build a mock Settings object."""
    s = MagicMock()
    s.env = env
    s.oanda_account_id = account_id
    s.live_trading_enabled = live_trading_enabled
    # SecretStr-like: .get_secret_value() returns the token string.
    secret = MagicMock()
    secret.get_secret_value.return_value = token
    s.oanda_api_token = secret
    return s


def _make_store_with_state(
    db: Store,
    *,
    day_pl: float = 0.0,
    start_of_day_equity: float = 10_000.0,
    as_of: Optional[datetime] = None,
) -> None:
    """Seed the store with a fresh account_state row."""
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    db.write_account_state(
        start_of_day_equity=start_of_day_equity,
        day_pl=day_pl,
        as_of=as_of,
    )


def _make_stub_client(reachable: bool = True) -> MagicMock:
    """Build a stub OANDA client that succeeds or fails account_summary."""
    client = MagicMock()
    if reachable:
        client.account_summary.return_value = {"account": {"balance": "10000.00"}}
    else:
        client.account_summary.side_effect = RuntimeError("OANDA unreachable")
    return client


@pytest.fixture()
def mem_store(tmp_path: object) -> Generator[Store, None, None]:
    """Return a fresh in-memory (tmp_path) SQLite store."""
    import tempfile
    import os
    db_file = tempfile.mktemp(suffix=".db")
    store = Store(db_file)
    yield store
    store.close()
    try:
        os.unlink(db_file)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 1. go=True only when ALL checks pass AND attested=True
# ---------------------------------------------------------------------------


def test_go_requires_all_checks_and_attestation(mem_store: Store) -> None:
    """go=True only when all mechanical checks pass AND attested=True."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is True
    assert all(c.ok for c in report.checks)
    # INV-03: checked_at is UTC-aware.
    assert report.checked_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 2. attested=False → NO-GO (AC-2)
# ---------------------------------------------------------------------------


def test_no_go_when_not_attested(mem_store: Store) -> None:
    """attested=False yields NO-GO with an INV-07-referencing reason."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=False,
    )
    assert report.go is False
    attest_check = next(c for c in report.checks if c.name == "track_record_attested")
    assert attest_check.ok is False
    assert "INV-07" in attest_check.detail


# ---------------------------------------------------------------------------
# 3. Kill-switch checks (AC-3a/b/c/d)
# ---------------------------------------------------------------------------


def test_kill_switch_no_go_missing(mem_store: Store) -> None:
    """account_state is None → kill_switch_armed NO-GO 'missing'."""
    # Do NOT seed account_state.
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is False
    ks_check = next(c for c in report.checks if c.name == "kill_switch_armed")
    assert ks_check.ok is False
    assert "missing" in ks_check.detail.lower() or "reconcil" in ks_check.detail.lower()


def test_kill_switch_no_go_stale(mem_store: Store) -> None:
    """account_state.as_of > 10 minutes ago → kill_switch_armed NO-GO 'stale'."""
    stale_as_of = datetime.now(timezone.utc) - timedelta(minutes=15)
    _make_store_with_state(mem_store, as_of=stale_as_of)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is False
    ks_check = next(c for c in report.checks if c.name == "kill_switch_armed")
    assert ks_check.ok is False
    assert "stale" in ks_check.detail.lower()


def test_kill_switch_no_go_tripped(mem_store: Store) -> None:
    """Kill switch tripped (day_pl <= -cap) → NO-GO 'tripped'."""
    # day_pl = -200 on equity 10000, cap = 1% = 100 → tripped.
    _make_store_with_state(
        mem_store,
        day_pl=-200.0,
        start_of_day_equity=10_000.0,
    )
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is False
    ks_check = next(c for c in report.checks if c.name == "kill_switch_armed")
    assert ks_check.ok is False
    assert "tripped" in ks_check.detail.lower()


def test_kill_switch_go_present_fresh_not_tripped(mem_store: Store) -> None:
    """Present + fresh + not tripped → kill_switch_armed GO."""
    _make_store_with_state(mem_store, day_pl=0.0, start_of_day_equity=10_000.0)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    ks_check = next(c for c in report.checks if c.name == "kill_switch_armed")
    assert ks_check.ok is True


# ---------------------------------------------------------------------------
# 4. Env/flag/token consistency (AC-4)
# ---------------------------------------------------------------------------


def test_env_live_no_token_no_go(mem_store: Store) -> None:
    """ENV=live, empty token → NO-GO."""
    _make_store_with_state(mem_store)
    settings = _make_settings(env="live", token="", live_trading_enabled=True)
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is False
    ec = next(c for c in report.checks if c.name == "env_flag_token_consistency")
    assert ec.ok is False
    assert "token" in ec.detail.lower()
    # Token value must not appear in detail (INV-08).
    assert FAKE_TOKEN not in ec.detail


def test_env_live_no_account_id_no_go(mem_store: Store) -> None:
    """ENV=live, missing account ID → NO-GO."""
    _make_store_with_state(mem_store)
    settings = _make_settings(env="live", account_id="", live_trading_enabled=True)
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is False
    ec = next(c for c in report.checks if c.name == "env_flag_token_consistency")
    assert ec.ok is False
    assert "account" in ec.detail.lower()


def test_env_live_trading_disabled_no_go(mem_store: Store) -> None:
    """ENV=live + live_trading_enabled=False → NO-GO."""
    _make_store_with_state(mem_store)
    settings = _make_settings(env="live", live_trading_enabled=False)
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    assert report.go is False
    ec = next(c for c in report.checks if c.name == "env_flag_token_consistency")
    assert ec.ok is False
    assert "live_trading_enabled" in ec.detail


def test_env_demo_always_consistent(mem_store: Store) -> None:
    """ENV=demo → env_flag_token_consistency always OK."""
    _make_store_with_state(mem_store)
    settings = _make_settings(env="demo", live_trading_enabled=False)
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    ec = next(c for c in report.checks if c.name == "env_flag_token_consistency")
    assert ec.ok is True
    assert "demo" in ec.detail.lower()


def test_env_live_all_ok(mem_store: Store) -> None:
    """ENV=live + token + account_id + live_trading_enabled=True → GO on this check."""
    _make_store_with_state(mem_store)
    settings = _make_settings(env="live", live_trading_enabled=True)
    client = _make_stub_client()

    report = run_preflight(
        settings=settings,
        store=mem_store,
        client=client,
        attested=True,
    )
    ec = next(c for c in report.checks if c.name == "env_flag_token_consistency")
    assert ec.ok is True


# ---------------------------------------------------------------------------
# 5. Account reachable (stub client)
# ---------------------------------------------------------------------------


def test_account_reachable_ok(mem_store: Store) -> None:
    """Stub client succeeds → account_reachable PASS."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client(reachable=True)

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=True)
    ar = next(c for c in report.checks if c.name == "account_reachable")
    assert ar.ok is True


def test_account_reachable_fail(mem_store: Store) -> None:
    """Stub client raises → account_reachable FAIL, go=False."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client(reachable=False)

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=True)
    assert report.go is False
    ar = next(c for c in report.checks if c.name == "account_reachable")
    assert ar.ok is False
    assert "fail" in ar.detail.lower() or "unreachable" in ar.detail.lower()


def test_no_client_account_reachable_fail(mem_store: Store) -> None:
    """No client provided → account_reachable FAIL (not silently PASS)."""
    _make_store_with_state(mem_store)
    settings = _make_settings()

    report = run_preflight(settings=settings, store=mem_store, client=None, attested=True)
    assert report.go is False
    ar = next(c for c in report.checks if c.name == "account_reachable")
    assert ar.ok is False


# ---------------------------------------------------------------------------
# 6. INV-04 static bracket contract check
# ---------------------------------------------------------------------------


def test_bracket_contract_check_passes(mem_store: Store) -> None:
    """The bracket/INV-04 static contract check should PASS in a correct build."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=True)
    bc = next(c for c in report.checks if c.name == "bracket_contract_inv04")
    assert bc.ok is True
    assert "inv-04" in bc.detail.lower()


# ---------------------------------------------------------------------------
# 7. Token never printed (INV-08)
# ---------------------------------------------------------------------------


def test_token_never_in_report_details(mem_store: Store) -> None:
    """No PreflightReport check detail contains the actual token value."""
    _make_store_with_state(mem_store)
    secret_token = "my-super-secret-token-12345"
    settings = _make_settings(token=secret_token)
    client = _make_stub_client()

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=True)
    for check in report.checks:
        assert secret_token not in check.detail, (
            f"Token leaked in check '{check.name}': {check.detail!r}"
        )


# ---------------------------------------------------------------------------
# 8. UTC timestamps (INV-03)
# ---------------------------------------------------------------------------


def test_checked_at_is_utc(mem_store: Store) -> None:
    """PreflightReport.checked_at is UTC-aware (INV-03)."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=False)
    assert report.checked_at.tzinfo is not None
    # Either the tzinfo is UTC or the offset is zero.
    utc_offset = report.checked_at.utcoffset()
    assert utc_offset is not None and utc_offset.total_seconds() == 0.0


# ---------------------------------------------------------------------------
# 9. Read-only: no order/write methods reachable from run_preflight
# ---------------------------------------------------------------------------


def test_read_only_no_write_methods_called(mem_store: Store) -> None:
    """run_preflight never calls write_account_state or submit_order on the store/client."""
    _make_store_with_state(mem_store)
    settings = _make_settings()

    # Wrap the store in a proxy that tracks writes.
    write_calls: list[str] = []
    original_write = mem_store.write_account_state

    def _tracking_write(
        *,
        start_of_day_equity: float,
        day_pl: float,
        as_of: datetime,
    ) -> None:
        write_calls.append("write_account_state")
        return original_write(
            start_of_day_equity=start_of_day_equity,
            day_pl=day_pl,
            as_of=as_of,
        )

    mem_store.write_account_state = _tracking_write  # type: ignore[method-assign]

    client = MagicMock()
    client.account_summary.return_value = {}
    # submit_order, place_order etc. should never be called.

    run_preflight(settings=settings, store=mem_store, client=client, attested=True)

    assert write_calls == [], (
        f"run_preflight unexpectedly wrote to the store: {write_calls}"
    )
    # Order-placement methods must not be called.
    for method_name in ("submit_order", "place_order", "create_order"):
        attr = getattr(client, method_name, None)
        if attr is not None:
            assert not attr.called


# ---------------------------------------------------------------------------
# 10. CLI: exit codes + --help
# ---------------------------------------------------------------------------


def test_preflight_help() -> None:
    """fathom preflight --help exits 0 and mentions key flags."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["preflight", "--help"])
    assert exc_info.value.code == 0


def test_preflight_cli_no_go_exit_nonzero(tmp_path: object) -> None:
    """fathom preflight (demo, no attest, empty store) → exit 1 (NO-GO)."""
    import tempfile, os
    db_file = tempfile.mktemp(suffix=".db")
    try:
        with (
            patch("cli.Settings") as mock_settings_cls,
            patch("cli.OandaClient") as mock_client_cls,
        ):
            mock_settings_cls.return_value = _make_settings()
            stub_client = _make_stub_client(reachable=True)
            mock_client_cls.return_value = stub_client

            buf = io.StringIO()
            with redirect_stderr(buf):
                result = cli.main(["preflight", "--db-path", db_file])

        assert result == 1, "Expected NO-GO (exit 1) when store is empty and no attestation"
        stderr_output = buf.getvalue()
        assert "NO-GO" in stderr_output
    finally:
        try:
            os.unlink(db_file)
        except OSError:
            pass


def test_preflight_cli_go_exit_zero(tmp_path: object) -> None:
    """fathom preflight --attest-track-record (demo, seeded store) → exit 0 (GO)."""
    import tempfile, os
    db_file = tempfile.mktemp(suffix=".db")
    try:
        # Seed the store first.
        store = Store(db_file)
        _make_store_with_state(store)
        store.close()

        with (
            patch("cli.Settings") as mock_settings_cls,
            patch("cli.OandaClient") as mock_client_cls,
        ):
            mock_settings_cls.return_value = _make_settings()
            stub_client = _make_stub_client(reachable=True)
            mock_client_cls.return_value = stub_client

            buf = io.StringIO()
            with redirect_stdout(buf):
                result = cli.main(
                    ["preflight", "--db-path", db_file, "--attest-track-record"]
                )

        assert result == 0, "Expected GO (exit 0) when all checks pass + attested"
        assert "GO" in buf.getvalue()
    finally:
        try:
            os.unlink(db_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 11. All five check names are present in the report
# ---------------------------------------------------------------------------


def test_report_has_all_five_checks(mem_store: Store) -> None:
    """PreflightReport must contain exactly five named checks."""
    _make_store_with_state(mem_store)
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=True)

    check_names = {c.name for c in report.checks}
    expected = {
        "account_reachable",
        "kill_switch_armed",
        "bracket_contract_inv04",
        "env_flag_token_consistency",
        "track_record_attested",
    }
    assert expected == check_names


# ---------------------------------------------------------------------------
# 12. go=False if any single check fails even if attested
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day_pl,sod_equity,as_of_offset_min",
    [
        # Tripped: day_pl <= -(1% * sod_equity)
        (-200.0, 10_000.0, 0),
        # Stale: as_of 20 min ago
        (0.0, 10_000.0, -20),
    ],
)
def test_single_failing_check_causes_no_go(
    mem_store: Store,
    day_pl: float,
    sod_equity: float,
    as_of_offset_min: int,
) -> None:
    """Any single failing check → go=False even when attested."""
    as_of = datetime.now(timezone.utc) + timedelta(minutes=as_of_offset_min)
    _make_store_with_state(
        mem_store,
        day_pl=day_pl,
        start_of_day_equity=sod_equity,
        as_of=as_of,
    )
    settings = _make_settings()
    client = _make_stub_client()

    report = run_preflight(settings=settings, store=mem_store, client=client, attested=True)
    assert report.go is False
