"""Tests for ``risk.limits`` — the book-level gate + daily-loss kill switch (P3-T-04).

Covers all four checks (kill switch, max-concurrent, book-risk stop-distance,
correlation bucket), the 00:00 UTC reset boundary (INV-03), every-reject-has-a-
reason, the side-effect-free ``kill_switch_status``, and purity (identical
injected state → identical decision).  No DB, no network, no clock — every
input is injected.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from execution.models import EntryType, Order, Position
from risk.limits import (
    DEFAULT_DAILY_LOSS_CAP,
    DEFAULT_MAX_BOOK_RISK,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_PER_CORRELATION_GROUP,
    LimitDecision,
    LimitsConfig,
    book_risk_budget,
    book_risk_sum,
    check_limits,
    kill_switch_status,
    position_risk,
)
from strategies.base import Direction

# ---------------------------------------------------------------------------
# Constants / factories
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
NEXT_MIDNIGHT = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)

EQUITY = 100_000.0
SOD_EQUITY = 100_000.0


def _order(*, instrument: str = "EUR_USD", direction: Direction = Direction.LONG) -> Order:
    units = 1000 if direction is Direction.LONG else -1000
    if direction is Direction.LONG:
        stop, target = 1.0950, 1.1100
    else:
        stop, target = 1.1050, 1.0900
    return Order(
        client_order_id="a" * 32,
        instrument=instrument,
        direction=direction,
        units=units,
        entry_type=EntryType.MARKET,
        stop_loss_price=stop,
        take_profit_price=target,
        candidate_ref=f"{instrument}:H1:macrossover_10_50",
        created_at=NOW,
    )


def _position(
    *,
    instrument: str = "GBP_USD",
    trade_id: str = "T1",
    units: int = 1000,
    entry_price: float = 1.2500,
    stop_loss_price: float = 1.2450,
    take_profit_price: float = 1.2600,
) -> Position:
    return Position(
        broker_trade_id=trade_id,
        instrument=instrument,
        units=units,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        opened_at=NOW,
        unrealized_pl=0.0,
        candidate_ref=f"{instrument}:H1:macrossover_10_50",
    )


def _returns(values: list[float]) -> pd.Series:
    """Build a daily-return Series (integer index suffices for pearson_corr)."""
    return pd.Series(values, index=list(range(len(values))), dtype="float64")


def _check(
    order: Order,
    *,
    open_positions: list[Position] | None = None,
    day_pl: float = 0.0,
    equity: float = EQUITY,
    start_of_day_equity: float = SOD_EQUITY,
    config: LimitsConfig | None = None,
    now: datetime = NOW,
    order_risk: float = 50.0,
    returns: dict[str, pd.Series] | None = None,
) -> LimitDecision:
    return check_limits(
        order,
        open_positions=open_positions or [],
        day_pl=day_pl,
        equity=equity,
        start_of_day_equity=start_of_day_equity,
        config=config or LimitsConfig(),
        now=now,
        order_risk=order_risk,
        returns=returns,
    )


# ---------------------------------------------------------------------------
# Config defaults are explicit and documented (AC).
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_approved_defaults(self) -> None:
        cfg = LimitsConfig()
        assert cfg.daily_loss_cap == 0.01 == DEFAULT_DAILY_LOSS_CAP
        assert cfg.max_concurrent == 5 == DEFAULT_MAX_CONCURRENT
        assert cfg.max_book_risk == 0.01 == DEFAULT_MAX_BOOK_RISK
        assert cfg.max_per_correlation_group == 2 == DEFAULT_MAX_PER_CORRELATION_GROUP
        assert cfg.correlation_threshold == 0.7

    def test_caps_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            LimitsConfig(daily_loss_cap=0.0)
        with pytest.raises(ValueError):
            LimitsConfig(max_concurrent=0)
        with pytest.raises(ValueError):
            LimitsConfig(max_book_risk=0.0)


# ---------------------------------------------------------------------------
# 1. Daily-loss kill switch + 00:00 UTC reset (INV-03).
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_below_cap_allowed(self) -> None:
        # Loss of 0.5% — below the 1.0% cap.
        decision = _check(_order(), day_pl=-500.0)
        assert decision.allowed is True
        assert decision.kill_switch_active is False
        assert decision.reason is None

    def test_at_cap_trips(self) -> None:
        # Exactly -(0.01 * 100_000) = -1000.0 → at the cap → tripped.
        decision = _check(_order(), day_pl=-1000.0)
        assert decision.allowed is False
        assert decision.kill_switch_active is True
        assert decision.reason is not None

    def test_over_cap_trips(self) -> None:
        decision = _check(_order(), day_pl=-1500.0)
        assert decision.allowed is False
        assert decision.kill_switch_active is True

    def test_kill_switch_rejects_even_a_clean_order(self) -> None:
        # Empty book, tiny risk, uncorrelated — only the kill switch can reject.
        decision = _check(
            _order(), open_positions=[], day_pl=-2000.0, order_risk=1.0
        )
        assert decision.allowed is False
        assert decision.kill_switch_active is True

    def test_reset_boundary_is_next_utc_midnight(self) -> None:
        status = kill_switch_status(
            day_pl=-2000.0,
            start_of_day_equity=SOD_EQUITY,
            config=LimitsConfig(),
            now=NOW,
        )
        assert status.active is True
        assert status.reset_at == NEXT_MIDNIGHT
        assert status.reset_at.tzinfo is not None
        # 00:00:00 UTC pinned (INV-03).
        assert status.reset_at.hour == 0
        assert status.reset_at.minute == 0
        assert status.reset_at.second == 0

    def test_fresh_utc_day_resets_via_zeroed_day_pl(self) -> None:
        # Reconciliation zeroes day_pl at the new UTC day; the switch is then off.
        fresh = datetime(2026, 5, 30, 0, 0, 1, tzinfo=timezone.utc)
        decision = _check(_order(), day_pl=0.0, now=fresh)
        assert decision.allowed is True
        assert decision.kill_switch_active is False

    def test_non_positive_sod_equity_is_treated_as_tripped(self) -> None:
        decision = _check(_order(), start_of_day_equity=0.0, day_pl=0.0)
        assert decision.allowed is False
        assert decision.kill_switch_active is True


# ---------------------------------------------------------------------------
# 2. Max-concurrent boundary.
# ---------------------------------------------------------------------------


class TestMaxConcurrent:
    def test_at_cap_rejected(self) -> None:
        positions = [
            _position(trade_id=f"T{i}", instrument="GBP_USD") for i in range(5)
        ]
        decision = _check(_order(), open_positions=positions)
        assert decision.allowed is False
        assert decision.kill_switch_active is False
        assert decision.reason is not None and "concurrent" in decision.reason.lower()

    def test_one_below_cap_allowed(self) -> None:
        # 4 open with negligible risk, uncorrelated returns → allowed.
        positions = [
            _position(
                trade_id=f"T{i}",
                instrument=f"X{i}_USD",
                entry_price=1.0,
                stop_loss_price=0.99999,
            )
            for i in range(4)
        ]
        decision = _check(_order(), open_positions=positions, order_risk=1.0)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# 3. Book-risk boundary (stop-distance risk, not notional).
# ---------------------------------------------------------------------------


class TestBookRisk:
    def test_position_risk_is_stop_distance_not_notional(self) -> None:
        p = _position(units=1000, entry_price=1.2500, stop_loss_price=1.2450)
        # |1000| * |1.2500 - 1.2450| = 1000 * 0.0050 = 5.0  (NOT 1250 notional).
        assert position_risk(p) == pytest.approx(5.0)

    def test_at_budget_allowed(self) -> None:
        # Budget = 0.01 * 100_000 = 1000.0.  One position risking 950 + order 50.
        p = _position(units=1, entry_price=1000.0, stop_loss_price=50.0)  # risk 950
        assert position_risk(p) == pytest.approx(950.0)
        decision = _check(_order(), open_positions=[p], order_risk=50.0)
        assert decision.allowed is True

    def test_over_budget_rejected(self) -> None:
        # 950 current + 51 order = 1001 > 1000 budget.
        p = _position(units=1, entry_price=1000.0, stop_loss_price=50.0)  # risk 950
        decision = _check(_order(), open_positions=[p], order_risk=51.0)
        assert decision.allowed is False
        assert decision.kill_switch_active is False
        assert decision.reason is not None and "book-risk" in decision.reason.lower()

    def test_book_risk_sums_across_positions(self) -> None:
        positions = [
            _position(trade_id="A", units=1, entry_price=1000.0, stop_loss_price=500.0),  # 500
            _position(trade_id="B", units=1, entry_price=1000.0, stop_loss_price=500.0),  # 500
        ]
        # 1000 current + 1 order = 1001 > 1000 budget.
        decision = _check(_order(), open_positions=positions, order_risk=1.0)
        assert decision.allowed is False
        assert decision.reason is not None and "book-risk" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Extracted helpers (P4-T-02 / DRIFT-02): book_risk_sum + book_risk_budget are
# the single source of truth check_limits calls back into, so the admin-panel
# blotter can show the exact figures the kill switch uses.
# ---------------------------------------------------------------------------


class TestBookRiskHelpers:
    def test_book_risk_sum_empty_is_zero(self) -> None:
        assert book_risk_sum([]) == 0.0

    def test_book_risk_sum_single_matches_position_risk(self) -> None:
        p = _position(units=1, entry_price=1000.0, stop_loss_price=500.0)  # risk 500
        assert book_risk_sum([p]) == pytest.approx(position_risk(p))
        assert book_risk_sum([p]) == pytest.approx(500.0)

    def test_book_risk_sum_multi_matches_sum_of_position_risk(self) -> None:
        positions = [
            _position(trade_id="A", units=1000, entry_price=1.2500, stop_loss_price=1.2450),
            _position(trade_id="B", units=1, entry_price=1000.0, stop_loss_price=500.0),
            _position(trade_id="C", units=2000, entry_price=1.1000, stop_loss_price=1.0900),
        ]
        expected = sum(position_risk(p) for p in positions)
        assert book_risk_sum(positions) == pytest.approx(expected)

    def test_book_risk_budget_is_max_book_risk_times_equity(self) -> None:
        cfg = LimitsConfig()  # default max_book_risk == 0.01
        assert book_risk_budget(100_000.0, cfg) == pytest.approx(1000.0)

    def test_book_risk_budget_honours_overridden_cap(self) -> None:
        cfg = LimitsConfig(max_book_risk=0.025)
        assert book_risk_budget(80_000.0, cfg) == pytest.approx(0.025 * 80_000.0)

    def test_check_limits_uses_the_helpers_at_the_boundary(self) -> None:
        # Reconstruct check_limits' book-risk decision from the extracted
        # helpers: current + order vs budget — confirming one source of truth.
        positions = [
            _position(trade_id="A", units=1, entry_price=1000.0, stop_loss_price=500.0),  # 500
            _position(trade_id="B", units=1, entry_price=1000.0, stop_loss_price=500.0),  # 500
        ]
        cfg = LimitsConfig()
        current = book_risk_sum(positions)
        budget = book_risk_budget(EQUITY, cfg)
        assert current == pytest.approx(1000.0)
        assert budget == pytest.approx(1000.0)
        # order_risk that exactly fills the budget is allowed; one cent over rejects.
        at_budget = _check(_order(), open_positions=positions, order_risk=0.0, config=cfg)
        assert at_budget.allowed is True
        over = _check(_order(), open_positions=positions, order_risk=0.01, config=cfg)
        assert over.allowed is False
        assert over.reason is not None and "book-risk" in over.reason.lower()


# ---------------------------------------------------------------------------
# 4. Correlation bucket: 2 correlated allowed, 3rd rejected, uncorrelated ok.
# ---------------------------------------------------------------------------


class TestCorrelationBucket:
    def _correlated_returns(self) -> dict[str, pd.Series]:
        n = 40
        base = np.arange(n, dtype=float)  # monotonic → ρ ≈ +1 between the three
        rng = np.random.default_rng(0)
        return {
            "EUR_USD": _returns(list(base + rng.normal(0, 1e-9, n))),
            "GBP_USD": _returns(list(base + rng.normal(0, 1e-9, n))),
            "AUD_USD": _returns(list(base + rng.normal(0, 1e-9, n))),
            # Anti-correlated / orthogonal instrument.
            "USD_JPY": _returns(list(-base + rng.normal(0, 1e-9, n))),
        }

    def test_two_correlated_allowed(self) -> None:
        # One correlated open position + the order = bucket of 2 == cap (allowed).
        returns = self._correlated_returns()
        p = _position(instrument="GBP_USD")
        decision = _check(
            _order(instrument="EUR_USD"),
            open_positions=[p],
            returns=returns,
            order_risk=1.0,
        )
        assert decision.allowed is True

    def test_third_correlated_rejected(self) -> None:
        # Two correlated open + a third correlated order = bucket of 3 > cap.
        returns = self._correlated_returns()
        positions = [
            _position(instrument="GBP_USD", trade_id="G"),
            _position(instrument="AUD_USD", trade_id="A"),
        ]
        decision = _check(
            _order(instrument="EUR_USD"),
            open_positions=positions,
            returns=returns,
            order_risk=1.0,
        )
        assert decision.allowed is False
        assert decision.kill_switch_active is False
        assert decision.reason is not None and "correlation" in decision.reason.lower()

    def test_uncorrelated_via_no_returns_allowed(self) -> None:
        # No returns supplied → no edges → each instrument is its own bucket.
        positions = [
            _position(instrument="GBP_USD", trade_id="G"),
            _position(instrument="AUD_USD", trade_id="A"),
        ]
        decision = _check(
            _order(instrument="EUR_USD"),
            open_positions=positions,
            returns=None,
            order_risk=1.0,
        )
        assert decision.allowed is True

    def test_anti_correlated_groups_under_abs_rho(self) -> None:
        # |ρ| (absolute) is what buckets — a strongly anti-correlated pair shares
        # exposure too.  Two anti-correlated opens + correlated order > cap.
        returns = self._correlated_returns()
        positions = [
            _position(instrument="EUR_USD", trade_id="E"),  # +base
            _position(instrument="USD_JPY", trade_id="J"),  # -base, |ρ|≈1
        ]
        decision = _check(
            _order(instrument="GBP_USD"),  # +base
            open_positions=positions,
            returns=returns,
            order_risk=1.0,
        )
        assert decision.allowed is False
        assert decision.reason is not None and "correlation" in decision.reason.lower()

    def test_genuinely_uncorrelated_instrument_allowed(self) -> None:
        # Two correlated opens + an order whose returns are noise (ρ < 0.7).
        n = 40
        base = np.arange(n, dtype=float)
        noise = np.random.default_rng(7).normal(0, 1, n)
        returns = {
            "EUR_USD": _returns(list(base)),
            "GBP_USD": _returns(list(base)),
            "NZD_CAD": _returns(list(noise)),
        }
        positions = [
            _position(instrument="EUR_USD", trade_id="E"),
            _position(instrument="GBP_USD", trade_id="G"),
        ]
        decision = _check(
            _order(instrument="NZD_CAD"),
            open_positions=positions,
            returns=returns,
            order_risk=1.0,
        )
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Every reject carries a reason; check ordering is most-global-first.
# ---------------------------------------------------------------------------


class TestRejectReasonsAndOrdering:
    def test_every_reject_has_a_reason(self) -> None:
        # Kill switch.
        d1 = _check(_order(), day_pl=-2000.0)
        # Max concurrent.
        d2 = _check(
            _order(),
            open_positions=[_position(trade_id=f"T{i}") for i in range(5)],
        )
        # Book risk.
        p = _position(units=1, entry_price=1000.0, stop_loss_price=50.0)
        d3 = _check(_order(), open_positions=[p], order_risk=100.0)
        for d in (d1, d2, d3):
            assert d.allowed is False
            assert d.reason is not None and d.reason != ""

    def test_kill_switch_takes_precedence_over_other_breaches(self) -> None:
        # Both kill switch AND max-concurrent breached: kill switch wins.
        positions = [_position(trade_id=f"T{i}") for i in range(5)]
        decision = _check(_order(), open_positions=positions, day_pl=-2000.0)
        assert decision.kill_switch_active is True
        assert decision.reason is not None and "kill switch" in decision.reason.lower()

    def test_non_positive_equity_rejected(self) -> None:
        decision = _check(_order(), equity=0.0)
        assert decision.allowed is False
        assert decision.reason is not None


# ---------------------------------------------------------------------------
# kill_switch_status is read-only / side-effect-free and pure.
# ---------------------------------------------------------------------------


class TestKillSwitchStatus:
    def test_reports_inactive_with_figures(self) -> None:
        status = kill_switch_status(
            day_pl=-500.0,
            start_of_day_equity=SOD_EQUITY,
            config=LimitsConfig(),
            now=NOW,
        )
        assert status.active is False
        assert status.day_pl == -500.0
        assert status.cap_amount == pytest.approx(1000.0)
        assert status.reset_at == NEXT_MIDNIGHT

    def test_side_effect_free_idempotent(self) -> None:
        kwargs = dict(
            day_pl=-1200.0,
            start_of_day_equity=SOD_EQUITY,
            config=LimitsConfig(),
            now=NOW,
        )
        s1 = kill_switch_status(**kwargs)  # type: ignore[arg-type]
        s2 = kill_switch_status(**kwargs)  # type: ignore[arg-type]
        assert s1 == s2
        assert s1.active is True


# ---------------------------------------------------------------------------
# Purity: identical injected state → identical decision; inputs not mutated.
# ---------------------------------------------------------------------------


class TestPurity:
    def test_deterministic(self) -> None:
        order = _order()
        positions = [_position(trade_id="P1"), _position(trade_id="P2", instrument="AUD_USD")]
        returns = {"EUR_USD": _returns(list(np.arange(40.0)))}
        d1 = _check(order, open_positions=positions, returns=returns, order_risk=10.0)
        d2 = _check(order, open_positions=positions, returns=returns, order_risk=10.0)
        assert d1 == d2

    def test_does_not_mutate_inputs(self) -> None:
        order = _order()
        positions = [_position(trade_id="P1")]
        n_before = len(positions)
        returns = {"EUR_USD": _returns(list(np.arange(40.0)))}
        keys_before = set(returns.keys())
        _check(order, open_positions=positions, returns=returns)
        assert len(positions) == n_before
        assert set(returns.keys()) == keys_before
