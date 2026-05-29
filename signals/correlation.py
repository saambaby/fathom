"""Shared Pearson correlation primitive for signal modules (P3-T-02).

Provides the low-level helpers that both the Phase 2 portfolio limiter
(``signals.portfolio``) and the Phase 3 risk-limits module
(``risk.limits``) need:

* ``pearson_corr`` / ``_pearson_corr`` — pairwise Pearson ρ on aligned
  return Series, returning ``None`` when data is insufficient.
* ``mid_returns`` / ``_mid_returns`` — arithmetic daily returns from
  bid/ask mid close prices.
* ``split_currencies`` / ``_split_currencies`` — OANDA instrument string
  splitter (``"EUR_USD"`` → ``["EUR", "USD"]``).

The underscore-prefixed names are the original names as shipped in
``signals/portfolio.py``; they are kept as aliases so existing callers
do not break.

INV-03: this module never creates timestamps; it only consumes the
    UTC-aware index on the return Series passed in.
"""

from __future__ import annotations

import pandas as pd

# ---------------------------------------------------------------------------
# Shared constant
# ---------------------------------------------------------------------------

#: Minimum number of overlapping non-NaN return observations required to
#: treat a computed Pearson ρ as reliable.  Below this count the caller
#: should skip the correlation check (conservative: do NOT drop on
#: insufficient data).
MIN_CORRELATION_OBS: int = 20

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def split_currencies(instrument: str) -> list[str]:
    """Split an OANDA instrument string into its leg currencies.

    ``"EUR_USD"`` → ``["EUR", "USD"]``.  Returns all non-empty parts split
    on ``"_"`` — defensive; never raises.
    """
    return [p for p in instrument.split("_") if p]


def mid_returns(df: pd.DataFrame) -> pd.Series:
    """Compute daily arithmetic returns from bid/ask mid close prices.

    Uses ``(close_bid + close_ask) / 2`` as the mid price and computes
    ``pct_change()`` (arithmetic return; sufficient for correlation
    estimation).  Returns a ``pd.Series`` indexed by ``df["time"]``.
    If the required columns are absent or the DataFrame is too short,
    returns an empty Series.
    """
    required = {"time", "close_bid", "close_ask"}
    if not required.issubset(df.columns) or len(df) < 2:
        return pd.Series(dtype="float64")
    mid = (df["close_bid"] + df["close_ask"]) / 2.0
    mid.index = pd.Index(df["time"])
    returns = mid.pct_change().dropna()
    return returns


def pearson_corr(a: pd.Series, b: pd.Series) -> float | None:
    """Compute Pearson ρ between two return series on their shared index.

    Aligns on the index (timestamps), drops NaN pairs, and returns ``None``
    if fewer than ``MIN_CORRELATION_OBS`` observations are available (the
    caller treats ``None`` as "insufficient data — do not drop").

    Returns:
        Pearson ρ in [-1, 1], or ``None`` if data is insufficient.
    """
    if a.empty or b.empty:
        return None

    # Align on shared timestamps.  ``sort=True`` suppresses the Pandas 4
    # DeprecationWarning about the default sort behaviour when concatenating
    # DatetimeIndex-backed Series; aligning by sorted index is the correct
    # semantic here (we want the shared timestamps in order).
    aligned = pd.concat([a.rename("a"), b.rename("b")], axis=1, sort=True).dropna()
    if len(aligned) < MIN_CORRELATION_OBS:
        return None

    rho: float = aligned["a"].corr(aligned["b"])
    # corr() returns NaN if std is zero; treat as None (insufficient).
    if pd.isna(rho):
        return None
    return rho


# ---------------------------------------------------------------------------
# Underscore-prefixed aliases (backward-compat for signals.portfolio)
# ---------------------------------------------------------------------------

#: Alias for :func:`split_currencies` — original name as shipped.
_split_currencies = split_currencies

#: Alias for :func:`mid_returns` — original name as shipped.
_mid_returns = mid_returns

#: Alias for :func:`pearson_corr` — original name as shipped.
_pearson_corr = pearson_corr
