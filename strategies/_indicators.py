"""Shared technical indicators for Fathom strategies.

All strategies must import from this module (INV-11) — never define their own
copies of these computations.

library_defaults:
  pandas.ewm(com=..., adjust=False) — Wilder's recursive smoothing.
  adjust=False is required to match charting-tool implementations.
"""

from __future__ import annotations

import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average True Range using Wilder's smoothing.

    True Range = max(
        high_bid - low_bid,
        |high_bid - prev_close_bid|,
        |low_bid  - prev_close_bid|,
    )

    ATR is the EWM of True Range with ``com = period - 1`` and
    ``adjust=False`` (alpha = 1/period — Wilder's definition).

    Parameters
    ----------
    df:
        Candle DataFrame with columns ``high_bid``, ``low_bid``,
        ``close_bid``.  Must contain at least one row.
    period:
        Look-back for the EWM average.  Default 14 (standard Wilder ATR).

    Returns
    -------
    pd.Series
        ATR values aligned to ``df.index``.  Row 0 is initialised from
        ``high_bid[0] - low_bid[0]`` (no prev_close available); subsequent
        values are produced by the EWM.

    Notes
    -----
    Formula is extracted verbatim from the PoC ``MACrossover._compute_atr``
    (``trend.py``).  No behaviour change — the two implementations produce
    identical floating-point results.
    """
    high = df["high_bid"].astype(float)
    low = df["low_bid"].astype(float)
    close = df["close_bid"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder's ATR: ewm with com = period - 1 (alpha = 1/period), adjust=False
    return tr.ewm(com=period - 1, adjust=False).mean()
