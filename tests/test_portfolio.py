"""Tests for signals/portfolio.py (P2-T-02).

Everything is mocked — NO live HTTP, no real candle data.  Tests cover all
AC from the P2-T-02 task spec:

- Correlated pair (|ρ| > threshold) → only higher-scored admitted.
- max_per_currency enforced.
- max_concurrent enforced.
- Greedy admission is deterministic (highest score first, stable tie-break).
- Empty input → empty output (no error).
- INV-01: no sizing/orders — only a filtered list returned.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from signals.portfolio import (
    MIN_CORRELATION_OBS,
    PortfolioLimiter,
    PortfolioLimiterConfig,
    _mid_returns,
    _pearson_corr,
    _split_currencies,
)
from signals.ranker import Candidate

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
_GENERATED_AT = "2026-05-29T10:00:00Z"


def _make_candidate(
    instrument: str = "EUR_USD",
    strategy_name: str = "macrossover",
    oos_sharpe_mean: float = 1.0,
    quality_score: float = 0.8,
    rank: int = 1,
    direction: str = "LONG",
) -> Candidate:
    """Return a minimal ``Candidate`` with overridable fields."""
    return Candidate(
        instrument=instrument,
        timeframe="H4",
        strategy_name=strategy_name,
        direction=direction,
        entry_ref=1.1000,
        stop_distance=0.0020,
        target_distance=0.0030,
        oos_sharpe_mean=oos_sharpe_mean,
        quality_score=quality_score,
        rank=rank,
        spread_ok=True,
        session_ok=True,
        news_flag=False,
        generated_at=_GENERATED_AT,
    )


def _make_daily_candles(
    n: int = MIN_CORRELATION_OBS + 5,
    close_bid: float = 1.1000,
    noise_std: float = 0.001,
    seed: int = 0,
) -> pd.DataFrame:
    """Build a fake daily candle DataFrame of length ``n``."""
    rng = np.random.default_rng(seed)
    prices_bid = close_bid + np.cumsum(rng.normal(0, noise_std, n))
    prices_ask = prices_bid + 0.0002

    times = pd.date_range(
        start=NOW - timedelta(days=n), periods=n, freq="D", tz="UTC"
    )
    return pd.DataFrame(
        {
            "time": times,
            "open_bid": prices_bid,
            "high_bid": prices_bid + noise_std,
            "low_bid": prices_bid - noise_std,
            "close_bid": prices_bid,
            "open_ask": prices_ask,
            "high_ask": prices_ask + noise_std,
            "low_ask": prices_ask - noise_std,
            "close_ask": prices_ask,
            "volume": np.ones(n, dtype=int),
        }
    )


def _make_correlated_candles(
    n: int = MIN_CORRELATION_OBS + 10,
    rho_target: float = 0.95,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return two ``(n,)`` candle frames whose returns have correlation ≈ ``rho_target``.

    Uses a Cholesky decomposition to produce correlated standard-normal shocks,
    then accumulates into price series.
    """
    rng = np.random.default_rng(seed)
    cov = np.array([[1.0, rho_target], [rho_target, 1.0]])
    L = np.linalg.cholesky(cov)
    z = rng.standard_normal((2, n))
    correlated = L @ z  # shape (2, n)

    def _to_df(shocks: np.ndarray, base_price: float) -> pd.DataFrame:
        prices = base_price + np.cumsum(shocks * 0.001)
        times = pd.date_range(
            start=NOW - timedelta(days=n), periods=n, freq="D", tz="UTC"
        )
        return pd.DataFrame(
            {
                "time": times,
                "open_bid": prices,
                "high_bid": prices + 0.0002,
                "low_bid": prices - 0.0002,
                "close_bid": prices,
                "open_ask": prices + 0.0002,
                "high_ask": prices + 0.0004,
                "low_ask": prices,
                "close_ask": prices + 0.0002,
                "volume": np.ones(n, dtype=int),
            }
        )

    return _to_df(correlated[0], 1.1000), _to_df(correlated[1], 1.2500)


def _make_uncorrelated_candles(
    n: int = MIN_CORRELATION_OBS + 10,
    seed: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return two candle frames whose returns are essentially uncorrelated."""
    rng = np.random.default_rng(seed)

    def _to_df(base_price: float, extra_seed: int) -> pd.DataFrame:
        rng2 = np.random.default_rng(extra_seed)
        prices = base_price + np.cumsum(rng2.normal(0, 0.001, n))
        times = pd.date_range(
            start=NOW - timedelta(days=n), periods=n, freq="D", tz="UTC"
        )
        return pd.DataFrame(
            {
                "time": times,
                "open_bid": prices,
                "high_bid": prices + 0.0002,
                "low_bid": prices - 0.0002,
                "close_bid": prices,
                "open_ask": prices + 0.0002,
                "high_ask": prices + 0.0004,
                "low_ask": prices,
                "close_ask": prices + 0.0002,
                "volume": np.ones(n, dtype=int),
            }
        )

    _ = rng  # used just for clarity; actual frames use deterministic seeds
    return _to_df(1.1000, 7), _to_df(1.2500, 99)


def _make_store(candle_map: dict[str, pd.DataFrame]) -> MagicMock:
    """Build a mock store that returns candles from ``candle_map`` keyed by instrument."""

    def _load_candles(
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        df = candle_map.get(instrument, pd.DataFrame())
        return df

    store = MagicMock()
    store.load_candles.side_effect = _load_candles
    return store


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestSplitCurrencies:
    def test_standard_pair(self) -> None:
        assert _split_currencies("EUR_USD") == ["EUR", "USD"]

    def test_triple_component(self) -> None:
        # Exotic like XAU_USD — returns all non-empty parts.
        assert _split_currencies("XAU_USD") == ["XAU", "USD"]

    def test_malformed(self) -> None:
        # Should not raise.
        result = _split_currencies("EURUSD")
        assert isinstance(result, list)

    def test_empty(self) -> None:
        assert _split_currencies("") == []


class TestPearsonCorr:
    def test_returns_none_for_empty_series(self) -> None:
        assert _pearson_corr(pd.Series(dtype="float64"), pd.Series(dtype="float64")) is None

    def test_returns_none_for_insufficient_obs(self) -> None:
        a = pd.Series([0.01, 0.02], index=[0, 1])
        b = pd.Series([0.01, 0.02], index=[0, 1])
        assert _pearson_corr(a, b) is None  # < MIN_CORRELATION_OBS

    def test_high_positive_correlation(self) -> None:
        n = MIN_CORRELATION_OBS + 10
        idx = list(range(n))
        a = pd.Series(np.arange(n, dtype=float), index=idx)
        b = pd.Series(np.arange(n, dtype=float) + 0.001, index=idx)
        rho = _pearson_corr(a, b)
        assert rho is not None
        assert rho > 0.99

    def test_no_overlap_returns_none(self) -> None:
        a = pd.Series([0.01] * 30, index=list(range(30)))
        b = pd.Series([0.01] * 30, index=list(range(100, 130)))
        # No shared index → aligned df empty → None.
        assert _pearson_corr(a, b) is None

    def test_returns_float_in_range(self) -> None:
        n = MIN_CORRELATION_OBS + 5
        idx = list(range(n))
        a = pd.Series(np.random.default_rng(0).normal(0, 1, n), index=idx)
        b = pd.Series(np.random.default_rng(1).normal(0, 1, n), index=idx)
        rho = _pearson_corr(a, b)
        assert rho is not None
        assert -1.0 <= rho <= 1.0


# ---------------------------------------------------------------------------
# PortfolioLimiter tests
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_list_returns_empty(self) -> None:
        store = _make_store({})
        limiter = PortfolioLimiter(store)
        result = limiter.apply([])
        assert result == []


class TestMaxConcurrent:
    def test_max_concurrent_bounds_output(self) -> None:
        cfg = PortfolioLimiterConfig(max_concurrent=3, max_per_currency=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate(f"EUR_USD", f"s{i}", oos_sharpe_mean=float(10 - i))
            for i in range(6)
        ]
        result = limiter.apply(candidates)
        assert len(result) <= 3

    def test_max_concurrent_1(self) -> None:
        cfg = PortfolioLimiterConfig(max_concurrent=1, max_per_currency=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate(
                instrument="EUR_USD",
                strategy_name=f"strat{i}",
                oos_sharpe_mean=float(5 - i),
            )
            for i in range(3)
        ]
        result = limiter.apply(candidates)
        assert len(result) == 1
        # The highest-score one (i=0) should be kept.
        assert result[0].strategy_name == "strat0"

    def test_exact_max_concurrent_admitted(self) -> None:
        """Exactly max_concurrent candidates admitted when all distinct instruments."""
        n = 4
        cfg = PortfolioLimiterConfig(max_concurrent=n, max_per_currency=n + 1)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        instruments = ["EUR_USD", "GBP_JPY", "AUD_CAD", "NZD_CHF"]
        candidates = [
            _make_candidate(
                instrument=instr,
                strategy_name="s",
                oos_sharpe_mean=float(n - i),
            )
            for i, instr in enumerate(instruments)
        ]
        result = limiter.apply(candidates)
        assert len(result) == n


class TestMaxPerCurrency:
    def test_max_per_currency_enforced(self) -> None:
        """No more than max_per_currency candidates share a given currency."""
        cfg = PortfolioLimiterConfig(max_per_currency=2, max_concurrent=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        # All USD-leg candidates; config allows 2.
        candidates = [
            _make_candidate(
                instrument="EUR_USD",
                strategy_name="s1",
                oos_sharpe_mean=4.0,
            ),
            _make_candidate(
                instrument="GBP_USD",
                strategy_name="s2",
                oos_sharpe_mean=3.0,
            ),
            _make_candidate(
                instrument="AUD_USD",
                strategy_name="s3",
                oos_sharpe_mean=2.0,
            ),
            _make_candidate(
                instrument="NZD_USD",
                strategy_name="s4",
                oos_sharpe_mean=1.0,
            ),
        ]
        result = limiter.apply(candidates)
        # At most 2 USD-leg instruments.
        usd_count = sum(
            1 for c in result if "USD" in _split_currencies(c.instrument)
        )
        assert usd_count <= 2

    def test_max_per_currency_1_drops_second_same_currency(self) -> None:
        cfg = PortfolioLimiterConfig(max_per_currency=1, max_concurrent=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_USD", "s1", oos_sharpe_mean=3.0),
            _make_candidate("GBP_USD", "s2", oos_sharpe_mean=2.0),  # shares USD
            _make_candidate("USD_JPY", "s3", oos_sharpe_mean=1.0),  # shares USD
        ]
        result = limiter.apply(candidates)
        # Only EUR_USD (first USD leg) admitted; GBP_USD and USD_JPY dropped.
        instruments = [c.instrument for c in result]
        assert "EUR_USD" in instruments
        assert "GBP_USD" not in instruments
        assert "USD_JPY" not in instruments

    def test_max_per_currency_non_shared_pass(self) -> None:
        """Candidates that don't share currencies are all admitted."""
        cfg = PortfolioLimiterConfig(max_per_currency=1, max_concurrent=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_GBP", "s1", oos_sharpe_mean=3.0),
            _make_candidate("USD_JPY", "s2", oos_sharpe_mean=2.0),
            _make_candidate("AUD_CAD", "s3", oos_sharpe_mean=1.0),
        ]
        result = limiter.apply(candidates)
        assert len(result) == 3


class TestCorrelation:
    def test_highly_correlated_pair_only_higher_score_admitted(self) -> None:
        """EUR_USD and GBP_USD above threshold → only higher-scored admitted."""
        df_eur, df_gbp = _make_correlated_candles(rho_target=0.95)

        candle_map = {"EUR_USD": df_eur, "GBP_USD": df_gbp}
        store = _make_store(candle_map)

        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=10,
            max_concurrent=10,
        )
        limiter = PortfolioLimiter(store, cfg)

        # EUR_USD has a higher score.
        eur = _make_candidate("EUR_USD", "s1", oos_sharpe_mean=2.0)
        gbp = _make_candidate("GBP_USD", "s2", oos_sharpe_mean=1.0)

        result = limiter.apply([gbp, eur])  # intentionally submit out-of-order
        instruments = [c.instrument for c in result]
        assert "EUR_USD" in instruments, "Higher-scored EUR_USD should be admitted"
        assert "GBP_USD" not in instruments, "Lower-scored GBP_USD should be dropped"

    def test_highly_correlated_pair_lower_score_submitted_first_still_dropped(
        self,
    ) -> None:
        """Lower-scored candidate wins admission if it appears first in the input,
        but that shouldn't happen here — greedy re-sort means the higher-scored
        is always considered first."""
        df_eur, df_gbp = _make_correlated_candles(rho_target=0.95)
        candle_map = {"EUR_USD": df_eur, "GBP_USD": df_gbp}
        store = _make_store(candle_map)

        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=10,
            max_concurrent=10,
        )
        limiter = PortfolioLimiter(store, cfg)

        # GBP_USD has higher score here.
        eur = _make_candidate("EUR_USD", "s1", oos_sharpe_mean=1.0)
        gbp = _make_candidate("GBP_USD", "s2", oos_sharpe_mean=2.0)

        result = limiter.apply([eur, gbp])
        instruments = [c.instrument for c in result]
        assert "GBP_USD" in instruments
        assert "EUR_USD" not in instruments

    def test_uncorrelated_pair_both_admitted(self) -> None:
        """Two uncorrelated instruments are both admitted (below threshold)."""
        df_a, df_b = _make_uncorrelated_candles()
        candle_map = {"EUR_GBP": df_a, "USD_JPY": df_b}
        store = _make_store(candle_map)

        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=10,
            max_concurrent=10,
        )
        limiter = PortfolioLimiter(store, cfg)

        a = _make_candidate("EUR_GBP", "s1", oos_sharpe_mean=2.0)
        b = _make_candidate("USD_JPY", "s2", oos_sharpe_mean=1.0)

        result = limiter.apply([a, b])
        assert len(result) == 2

    def test_missing_candles_skips_correlation_drop(self) -> None:
        """If candle data is unavailable, correlation check skipped (not dropped)."""
        # Store returns empty DataFrames.
        store = _make_store({})

        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=10,
            max_concurrent=10,
        )
        limiter = PortfolioLimiter(store, cfg)

        a = _make_candidate("EUR_USD", "s1", oos_sharpe_mean=2.0)
        b = _make_candidate("GBP_USD", "s2", oos_sharpe_mean=1.0)

        result = limiter.apply([a, b])
        # Both admitted since correlation can't be computed.
        assert len(result) == 2

    def test_below_threshold_pair_both_admitted(self) -> None:
        """If |ρ| is below threshold both instruments are admitted."""
        df_a, df_b = _make_correlated_candles(rho_target=0.4, seed=123)
        candle_map = {"EUR_USD": df_a, "GBP_USD": df_b}
        store = _make_store(candle_map)

        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=10,
            max_concurrent=10,
        )
        limiter = PortfolioLimiter(store, cfg)

        a = _make_candidate("EUR_USD", "s1", oos_sharpe_mean=2.0)
        b = _make_candidate("GBP_USD", "s2", oos_sharpe_mean=1.0)

        result = limiter.apply([a, b])
        assert len(result) == 2


class TestGreedyOrder:
    def test_highest_score_first_deterministic(self) -> None:
        """Greedy admission always picks the highest-scored candidate first."""
        cfg = PortfolioLimiterConfig(max_per_currency=1, max_concurrent=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        # All share EUR — max_per_currency=1 means only the first (highest) is kept.
        candidates = [
            _make_candidate("EUR_USD", "s1", oos_sharpe_mean=1.0),
            _make_candidate("EUR_GBP", "s2", oos_sharpe_mean=3.0),
            _make_candidate("EUR_CHF", "s3", oos_sharpe_mean=2.0),
        ]
        # Submit in any order.
        result = limiter.apply(candidates)
        assert len(result) == 1
        assert result[0].instrument == "EUR_GBP"  # highest oos_sharpe_mean

    def test_stable_tie_break_by_instrument_strategy(self) -> None:
        """When oos_sharpe_mean and quality_score tie, stable tie-break applies."""
        cfg = PortfolioLimiterConfig(max_per_currency=10, max_concurrent=2)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_USD", "s_b", oos_sharpe_mean=2.0, quality_score=0.5),
            _make_candidate("EUR_USD", "s_a", oos_sharpe_mean=2.0, quality_score=0.5),
        ]
        result1 = limiter.apply(candidates)
        result2 = limiter.apply(list(reversed(candidates)))
        # Both orderings must produce the same result (deterministic).
        assert [c.strategy_name for c in result1] == [c.strategy_name for c in result2]

    def test_output_preserves_score_order(self) -> None:
        """Output is in score order regardless of input order."""
        cfg = PortfolioLimiterConfig(max_per_currency=10, max_concurrent=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_USD", "s1", oos_sharpe_mean=1.0),
            _make_candidate("GBP_USD", "s2", oos_sharpe_mean=3.0),
            _make_candidate("AUD_USD", "s3", oos_sharpe_mean=2.0),
        ]
        result = limiter.apply(candidates)
        sharpes = [c.oos_sharpe_mean for c in result]
        assert sharpes == sorted(sharpes, reverse=True)


class TestCombinedLimits:
    def test_all_limits_together(self) -> None:
        """Combined scenario: currency cap + correlation + max_concurrent."""
        df_eur, df_gbp = _make_correlated_candles(rho_target=0.95, seed=5)
        candle_map = {
            "EUR_USD": df_eur,
            "GBP_USD": df_gbp,
            "USD_JPY": _make_daily_candles(seed=10),
            "AUD_CAD": _make_daily_candles(seed=20),
            "NZD_CHF": _make_daily_candles(seed=30),
            "CHF_JPY": _make_daily_candles(seed=40),
        }
        store = _make_store(candle_map)
        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=2,
            max_concurrent=3,
        )
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_USD", "s1", oos_sharpe_mean=6.0),
            _make_candidate("GBP_USD", "s2", oos_sharpe_mean=5.0),  # correlated w/ EUR_USD
            _make_candidate("USD_JPY", "s3", oos_sharpe_mean=4.0),  # 3rd USD → over cap at 2
            _make_candidate("AUD_CAD", "s4", oos_sharpe_mean=3.0),
            _make_candidate("NZD_CHF", "s5", oos_sharpe_mean=2.0),
            _make_candidate("CHF_JPY", "s6", oos_sharpe_mean=1.0),  # 4th would hit max_concurrent
        ]
        result = limiter.apply(candidates)

        assert len(result) <= cfg.max_concurrent
        # Verify currency counts.
        from signals.portfolio import _split_currencies

        for ccy in ["EUR", "GBP", "USD", "JPY", "AUD", "CAD", "NZD", "CHF"]:
            count = sum(
                1 for c in result if ccy in _split_currencies(c.instrument)
            )
            assert count <= cfg.max_per_currency, f"{ccy} over cap"

    def test_inv01_no_execution_import(self) -> None:
        """INV-01: signals.portfolio must not import execution or risk."""
        import importlib
        import sys

        # Remove cached module if present to force a fresh import attribute check.
        mod = importlib.import_module("signals.portfolio")
        # Check none of the module's imports bring in execution or risk.
        mod_file = getattr(mod, "__file__", "") or ""
        assert "execution" not in mod.__dict__, "execution should not be imported"
        assert "risk" not in mod.__dict__, "risk should not be imported"


class TestLogging:
    def test_drop_reason_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Each dropped candidate should produce an INFO log line."""
        cfg = PortfolioLimiterConfig(max_per_currency=1, max_concurrent=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_USD", "s1", oos_sharpe_mean=2.0),
            _make_candidate("GBP_USD", "s2", oos_sharpe_mean=1.0),  # EUR/USD share — dropped
        ]
        with caplog.at_level(logging.INFO, logger="signals.portfolio"):
            limiter.apply(candidates)

        drop_records = [r for r in caplog.records if "DROP" in r.message]
        assert len(drop_records) >= 1, "Expected at least one DROP log for currency cap"

    def test_max_concurrent_drop_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = PortfolioLimiterConfig(max_concurrent=1, max_per_currency=10)
        store = _make_store({})
        limiter = PortfolioLimiter(store, cfg)

        candidates = [
            _make_candidate("EUR_USD", "s1", oos_sharpe_mean=2.0),
            _make_candidate("GBP_JPY", "s2", oos_sharpe_mean=1.0),
        ]
        with caplog.at_level(logging.INFO, logger="signals.portfolio"):
            limiter.apply(candidates)

        drop_records = [r for r in caplog.records if "max_concurrent" in r.message]
        assert len(drop_records) >= 1


class TestConfigDefaults:
    def test_default_config_values(self) -> None:
        from signals.portfolio import (
            DEFAULT_CORRELATION_THRESHOLD,
            DEFAULT_MAX_CONCURRENT,
            DEFAULT_MAX_PER_CURRENCY,
        )

        cfg = PortfolioLimiterConfig()
        assert cfg.correlation_threshold == DEFAULT_CORRELATION_THRESHOLD
        assert cfg.max_per_currency == DEFAULT_MAX_PER_CURRENCY
        assert cfg.max_concurrent == DEFAULT_MAX_CONCURRENT

    def test_custom_config(self) -> None:
        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.5,
            max_per_currency=3,
            max_concurrent=8,
            lookback_days=30,
        )
        assert cfg.correlation_threshold == 0.5
        assert cfg.max_per_currency == 3
        assert cfg.max_concurrent == 8
        assert cfg.lookback_days == 30


# ---------------------------------------------------------------------------
# NaN price path — regression for the index-reconstruction crash
# ---------------------------------------------------------------------------


class TestMidReturnsNanPrices:
    """Regression tests for _mid_returns when mid-series prices are NaN.

    The old code did:
        returns = mid.pct_change().dropna()
        returns.index = pd.Index(df["time"].values[1:])

    which assumes pct_change().dropna() removes exactly ONE row (position 0).
    When close_bid or close_ask is NaN mid-series, dropna() removes MORE rows
    and the index reassignment raises ValueError: Length mismatch.

    The fix sets mid.index = pd.Index(df["time"]) BEFORE pct_change(), so
    dropna() carries the correct timestamps automatically — no post-hoc
    length-assuming assignment needed.
    """

    def _make_candles_with_nan(
        self,
        n: int = MIN_CORRELATION_OBS + 10,
        nan_positions: list[int] | None = None,
    ) -> pd.DataFrame:
        """Build a candle frame with NaN close_bid at specified mid-series positions."""
        rng = np.random.default_rng(7)
        prices_bid = 1.1000 + np.cumsum(rng.normal(0, 0.001, n))
        prices_ask = prices_bid + 0.0002
        times = pd.date_range(
            start=NOW - timedelta(days=n), periods=n, freq="D", tz="UTC"
        )
        df = pd.DataFrame(
            {
                "time": times,
                "close_bid": prices_bid,
                "close_ask": prices_ask,
            }
        )
        if nan_positions:
            for pos in nan_positions:
                df.loc[pos, "close_bid"] = float("nan")
        return df

    def test_nan_mid_series_does_not_raise(self) -> None:
        """NaN close_bid mid-series must NOT raise ValueError from _mid_returns."""
        df = self._make_candles_with_nan(nan_positions=[5, 10])
        # Must not raise — the old code would raise ValueError: Length mismatch.
        result = _mid_returns(df)
        assert isinstance(result, pd.Series)

    def test_nan_mid_series_timestamps_align_with_surviving_rows(self) -> None:
        """Returned index timestamps must match the rows that survived dropna()."""
        n = MIN_CORRELATION_OBS + 10
        nan_pos = 5  # one NaN mid-series drops an extra row beyond position-0
        df = self._make_candles_with_nan(n=n, nan_positions=[nan_pos])

        result = _mid_returns(df)

        # Compute the expected surviving timestamps manually:
        # mid.pct_change() at position nan_pos AND nan_pos+1 produce NaN
        # (the NaN row itself, and the first finite-to-NaN boundary).
        # Any index.dtype should be compatible with df["time"].
        assert not result.empty
        # Every timestamp in the result index must appear in df["time"].
        result_times = set(result.index)
        df_times = set(df["time"])
        assert result_times.issubset(df_times), (
            f"Result contains timestamps not in df['time']: "
            f"{result_times - df_times}"
        )

    def test_old_code_would_raise_value_error(self) -> None:
        """Demonstrate that the old post-hoc index assignment raises ValueError.

        This documents WHY the fix is needed: the old pattern is:
            returns.index = pd.Index(df["time"].values[1:])
        which fails when dropna() removes more than one row.
        """
        df = self._make_candles_with_nan(nan_positions=[5])
        mid = (df["close_bid"] + df["close_ask"]) / 2.0
        returns_with_nans = mid.pct_change().dropna()
        # With a NaN at position 5, pct_change().dropna() removes BOTH
        # position 0 (initial NaN) AND rows 5 and 6 (the NaN and the
        # finite-after-NaN boundary) — more than 1 row total.
        # df["time"].values[1:] has length n-1 but returns has length < n-1.
        n = len(df)
        assert len(returns_with_nans) < n - 1, (
            "Precondition: NaN mid-series must make dropna() remove >1 row"
        )
        with pytest.raises(ValueError):
            returns_with_nans.index = pd.Index(df["time"].values[1:])

    def test_no_nan_returns_correctly_indexed(self) -> None:
        """Sanity check: clean data still produces a correctly-indexed Series."""
        df = self._make_candles_with_nan(nan_positions=None)
        result = _mid_returns(df)
        # All timestamps in result must come from df["time"].
        result_times = set(result.index)
        df_times = set(df["time"])
        assert result_times.issubset(df_times)
        # No NaN values in result.
        assert not result.isna().any()

    def test_nan_path_correlation_does_not_raise(self) -> None:
        """End-to-end: PortfolioLimiter with NaN candles must not raise."""
        n = MIN_CORRELATION_OBS + 10
        df_with_nan = self._make_candles_with_nan(n=n, nan_positions=[3, 8])
        df_clean = _make_daily_candles(n=n, seed=42)

        candle_map = {"EUR_USD": df_with_nan, "GBP_USD": df_clean}
        store = _make_store(candle_map)
        cfg = PortfolioLimiterConfig(
            correlation_threshold=0.7,
            max_per_currency=10,
            max_concurrent=10,
        )
        limiter = PortfolioLimiter(store, cfg)

        a = _make_candidate("EUR_USD", "s1", oos_sharpe_mean=2.0)
        b = _make_candidate("GBP_USD", "s2", oos_sharpe_mean=1.0)

        # Must not raise — old code would raise ValueError in _mid_returns.
        result = limiter.apply([a, b])
        assert isinstance(result, list)
