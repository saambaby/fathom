"""Focused unit tests for the extracted signals/correlation.py primitive (P3-T-02).

Tests the correlation helpers directly on signals.correlation (not via the
portfolio shim) so that the Phase 3 risk-limits module has a green test
baseline for the shared primitive before it uses it.

These tests are deliberately simple and deterministic — no mocks, no store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from signals.correlation import (
    MIN_CORRELATION_OBS,
    _mid_returns,
    _pearson_corr,
    _split_currencies,
    mid_returns,
    pearson_corr,
    split_currencies,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# split_currencies / _split_currencies
# ---------------------------------------------------------------------------


class TestSplitCurrencies:
    def test_standard_pair(self) -> None:
        assert split_currencies("EUR_USD") == ["EUR", "USD"]

    def test_xau_usd(self) -> None:
        assert split_currencies("XAU_USD") == ["XAU", "USD"]

    def test_malformed_no_underscore(self) -> None:
        result = split_currencies("EURUSD")
        assert isinstance(result, list)

    def test_empty_string(self) -> None:
        assert split_currencies("") == []

    def test_underscore_alias_is_same_object(self) -> None:
        # The underscore alias must be the exact same function.
        assert _split_currencies is split_currencies


# ---------------------------------------------------------------------------
# mid_returns / _mid_returns
# ---------------------------------------------------------------------------


def _make_candle_df(n: int = MIN_CORRELATION_OBS + 5, seed: int = 0) -> pd.DataFrame:
    """Build a minimal daily candle DataFrame."""
    rng = np.random.default_rng(seed)
    bid = 1.1000 + np.cumsum(rng.normal(0, 0.001, n))
    ask = bid + 0.0002
    times = pd.date_range(
        start=NOW - timedelta(days=n), periods=n, freq="D", tz="UTC"
    )
    return pd.DataFrame(
        {"time": times, "close_bid": bid, "close_ask": ask}
    )


class TestMidReturns:
    def test_returns_series_correct_length(self) -> None:
        df = _make_candle_df()
        result = mid_returns(df)
        # pct_change() drops one row; all NaN rows for clean data = 1 row dropped.
        assert len(result) == len(df) - 1

    def test_index_is_subset_of_df_times(self) -> None:
        df = _make_candle_df()
        result = mid_returns(df)
        assert set(result.index).issubset(set(df["time"]))

    def test_empty_df_returns_empty_series(self) -> None:
        result = mid_returns(pd.DataFrame())
        assert result.empty

    def test_too_short_returns_empty_series(self) -> None:
        df = _make_candle_df(n=1)
        result = mid_returns(df)
        assert result.empty

    def test_missing_column_returns_empty_series(self) -> None:
        df = _make_candle_df()
        result = mid_returns(df.drop(columns=["close_ask"]))
        assert result.empty

    def test_nan_mid_series_does_not_raise(self) -> None:
        """NaN close_bid mid-series must not raise (regression guard)."""
        df = _make_candle_df(n=MIN_CORRELATION_OBS + 10)
        df.loc[5, "close_bid"] = float("nan")
        result = mid_returns(df)
        assert isinstance(result, pd.Series)
        assert set(result.index).issubset(set(df["time"]))

    def test_underscore_alias_is_same_object(self) -> None:
        assert _mid_returns is mid_returns


# ---------------------------------------------------------------------------
# pearson_corr / _pearson_corr
# ---------------------------------------------------------------------------


class TestPearsonCorr:
    def test_returns_none_for_empty_series(self) -> None:
        assert pearson_corr(pd.Series(dtype="float64"), pd.Series(dtype="float64")) is None

    def test_returns_none_for_insufficient_obs(self) -> None:
        a = pd.Series([0.01, 0.02], index=[0, 1])
        b = pd.Series([0.01, 0.02], index=[0, 1])
        assert pearson_corr(a, b) is None  # < MIN_CORRELATION_OBS

    def test_high_positive_correlation(self) -> None:
        n = MIN_CORRELATION_OBS + 10
        idx = list(range(n))
        a = pd.Series(np.arange(n, dtype=float), index=idx)
        b = pd.Series(np.arange(n, dtype=float) + 0.001, index=idx)
        rho = pearson_corr(a, b)
        assert rho is not None
        assert rho > 0.99

    def test_high_negative_correlation(self) -> None:
        n = MIN_CORRELATION_OBS + 10
        idx = list(range(n))
        a = pd.Series(np.arange(n, dtype=float), index=idx)
        b = pd.Series(-np.arange(n, dtype=float), index=idx)
        rho = pearson_corr(a, b)
        assert rho is not None
        assert rho < -0.99

    def test_no_overlap_returns_none(self) -> None:
        a = pd.Series([0.01] * 30, index=list(range(30)))
        b = pd.Series([0.01] * 30, index=list(range(100, 130)))
        assert pearson_corr(a, b) is None

    def test_returns_float_in_range(self) -> None:
        n = MIN_CORRELATION_OBS + 5
        idx = list(range(n))
        a = pd.Series(np.random.default_rng(0).normal(0, 1, n), index=idx)
        b = pd.Series(np.random.default_rng(1).normal(0, 1, n), index=idx)
        rho = pearson_corr(a, b)
        assert rho is not None
        assert -1.0 <= rho <= 1.0

    def test_constant_series_returns_none(self) -> None:
        """Zero std → corr() returns NaN → we return None."""
        n = MIN_CORRELATION_OBS + 5
        idx = list(range(n))
        a = pd.Series([1.0] * n, index=idx)
        b = pd.Series(np.arange(n, dtype=float), index=idx)
        assert pearson_corr(a, b) is None

    def test_underscore_alias_is_same_object(self) -> None:
        assert _pearson_corr is pearson_corr


# ---------------------------------------------------------------------------
# MIN_CORRELATION_OBS sanity
# ---------------------------------------------------------------------------


def test_min_correlation_obs_value() -> None:
    assert MIN_CORRELATION_OBS == 20
