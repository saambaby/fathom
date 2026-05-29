"""Portfolio-level filter for the ranked candidate watchlist (P2-T-02).

Applies three portfolio-level caps to the ranker's output so the watchlist
does not end up as a cluster of highly-correlated bets:

1. **Correlation gate:** if a candidate's instrument is highly correlated
   (|ρ| > ``correlation_threshold``) with an already-admitted instrument,
   the lower-scored candidate is dropped.  Correlation is computed from
   rolling daily returns via ``store.load_candles`` over a configurable
   lookback window.

2. **Per-currency cap:** at most ``max_per_currency`` admitted candidates
   may share a base or quote currency (e.g. ≤ 2 USD-leg candidates).

3. **Max-concurrent cap:** the total output list length is bounded by
   ``max_concurrent``.

Admission is greedy, highest-score-first (same sort key as the ranker:
``oos_sharpe_mean`` desc → ``quality_score`` desc → ``(instrument,
strategy_name)`` asc as a stable tie-break).  The input list is already
score-ranked, but we re-sort defensively to remain correct even if
candidates arrive out of order.

INV-01: this module produces only a filtered watchlist; it does not size,
    price, or place any order.  There is no import of ``execution`` or
    ``risk`` here.
INV-03: correlation is computed from UTC-stamped candles (``load_candles``
    returns ``datetime64[ns, UTC]``).  ``lookback_days`` defines the
    backwards reach from "now" — the caller is responsible for the store
    being populated; this module only reads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import pandas as pd
from pydantic import BaseModel, Field

from signals.ranker import Candidate

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables / defaults
# ---------------------------------------------------------------------------

#: Default absolute Pearson correlation threshold above which two instruments
#: are considered highly correlated (|ρ| > threshold → share exposure).
DEFAULT_CORRELATION_THRESHOLD: float = 0.7

#: Default maximum number of admitted candidates sharing any single leg
#: currency (base or quote).
DEFAULT_MAX_PER_CURRENCY: int = 2

#: Default maximum total admitted candidates (watchlist length cap).
DEFAULT_MAX_CONCURRENT: int = 5

#: How many calendar days of daily-return history to load when computing
#: rolling pairwise correlations.  90 days gives a stable estimate while
#: remaining within a few seconds of load time on a warm SQLite store.
DEFAULT_LOOKBACK_DAYS: int = 90

#: Minimum number of overlapping non-NaN return observations required to
#: treat a computed correlation as reliable.  Below this count the
#: correlation check is skipped (conservative: the pair is NOT dropped on
#: insufficient data).
MIN_CORRELATION_OBS: int = 20

# ---------------------------------------------------------------------------
# Store Protocol (structural — keeps PortfolioLimiter mockable in tests)
# ---------------------------------------------------------------------------


class _StoreLike(Protocol):
    """Structural interface for the candle store consumed by PortfolioLimiter.

    Only ``load_candles`` is required.  ``data.store.Store`` satisfies this
    protocol without modification.
    """

    def load_candles(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        ...


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class PortfolioLimiterConfig(BaseModel):
    """Tunable knobs for ``PortfolioLimiter``.

    All fields have sensible defaults from the Phase-2 spec; callers may
    override any subset.

    Attributes:
        correlation_threshold: |ρ| above which two instruments count as
            highly correlated (default 0.7).
        max_per_currency: Maximum admitted candidates sharing a base or
            quote currency (default 2).
        max_concurrent: Maximum total admitted candidates (default 5).
        lookback_days: Calendar days of daily-return history to load for
            correlation computation (default 90).
    """

    correlation_threshold: float = Field(
        default=DEFAULT_CORRELATION_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="Absolute Pearson |ρ| threshold (0–1).",
    )
    max_per_currency: int = Field(
        default=DEFAULT_MAX_PER_CURRENCY,
        ge=1,
        description="Max admitted candidates sharing any single leg currency.",
    )
    max_concurrent: int = Field(
        default=DEFAULT_MAX_CONCURRENT,
        ge=1,
        description="Max total admitted candidates.",
    )
    lookback_days: int = Field(
        default=DEFAULT_LOOKBACK_DAYS,
        ge=1,
        description="Calendar days of daily-return history for correlation.",
    )


# ---------------------------------------------------------------------------
# PortfolioLimiter
# ---------------------------------------------------------------------------


def _split_currencies(instrument: str) -> list[str]:
    """Split an OANDA instrument string into its leg currencies.

    ``"EUR_USD"`` → ``["EUR", "USD"]``.  Returns all non-empty parts split
    on ``"_"`` — defensive; never raises.
    """
    return [p for p in instrument.split("_") if p]


def _mid_returns(df: pd.DataFrame) -> pd.Series:
    """Compute daily log-returns from bid/ask mid close prices.

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


class PortfolioLimiter:
    """Apply portfolio-level caps to a ranked ``Candidate`` list.

    Admission is greedy: candidates are considered in decreasing score
    order (``oos_sharpe_mean`` desc → ``quality_score`` desc → stable
    tie-break).  A candidate is admitted unless it would breach one of:

    * **Correlation limit** — instrument already above threshold with an
      admitted instrument.
    * **Per-currency limit** — another admitted candidate already exhausted
      the ``max_per_currency`` quota for the base or quote currency.
    * **Max-concurrent limit** — ``max_concurrent`` candidates already
      admitted.

    Dropped candidates are logged at INFO level with the specific limit hit.
    The output list preserves score order.

    INV-01 enforced: no sizing/orders — this module ONLY filters.

    Args:
        store: Anything exposing ``load_candles``.  The correlation
            computation fetches ``"D"`` (daily) granularity candles.
        config: Tunable caps.  Defaults to the Phase-2 spec values.
    """

    def __init__(
        self,
        store: _StoreLike,
        config: PortfolioLimiterConfig | None = None,
    ) -> None:
        self._store = store
        self._config: PortfolioLimiterConfig = config or PortfolioLimiterConfig()

    # -- public API ----------------------------------------------------------

    def apply(self, candidates: list[Candidate]) -> list[Candidate]:
        """Filter ``candidates`` to a portfolio-safe subset.

        Args:
            candidates: Ranked candidates from ``Ranker.rank()``.  Need not
                be pre-sorted — this method re-sorts defensively.

        Returns:
            A subset of ``candidates`` (same ``Candidate`` objects, not
            copies) that respects all three caps, in score order.  Empty
            list in → empty list out (no error).
        """
        if not candidates:
            return []

        # Re-sort: highest score first; stable tie-break (instrument,
        # strategy_name) ascending — identical sort key to the ranker so the
        # admission order is deterministic.
        ordered = sorted(
            candidates,
            key=lambda c: (
                -c.oos_sharpe_mean,
                -c.quality_score,
                c.instrument,
                c.strategy_name,
            ),
        )

        cfg = self._config
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=cfg.lookback_days)

        # Cache for daily-return series keyed by instrument.
        return_cache: dict[str, pd.Series] = {}

        # State accumulated as we admit candidates.
        admitted: list[Candidate] = []
        admitted_instruments: list[str] = []      # instruments admitted so far
        currency_counts: dict[str, int] = {}      # currency → admitted count

        for candidate in ordered:
            instr = candidate.instrument
            score_repr = (
                f"oos_sharpe={candidate.oos_sharpe_mean:.4f}/"
                f"quality={candidate.quality_score:.4f}"
            )

            # -- Limit 1: max_concurrent -----------------------------------
            if len(admitted) >= cfg.max_concurrent:
                _log.info(
                    "DROP %s (%s): max_concurrent=%d reached.",
                    instr,
                    score_repr,
                    cfg.max_concurrent,
                )
                continue

            # -- Limit 2: per-currency cap ---------------------------------
            currencies = _split_currencies(instr)
            currency_breach: str | None = None
            for ccy in currencies:
                if currency_counts.get(ccy, 0) >= cfg.max_per_currency:
                    currency_breach = ccy
                    break
            if currency_breach is not None:
                _log.info(
                    "DROP %s (%s): max_per_currency=%d breached on currency %s.",
                    instr,
                    score_repr,
                    cfg.max_per_currency,
                    currency_breach,
                )
                continue

            # -- Limit 3: correlation --------------------------------------
            corr_drop = False
            if admitted_instruments:
                # Lazy-load returns for the current candidate.
                if instr not in return_cache:
                    return_cache[instr] = self._load_returns(
                        instr, start, now
                    )
                r_new = return_cache[instr]

                for admitted_instr in admitted_instruments:
                    if admitted_instr not in return_cache:
                        return_cache[admitted_instr] = self._load_returns(
                            admitted_instr, start, now
                        )
                    r_adm = return_cache[admitted_instr]

                    rho = _pearson_corr(r_new, r_adm)
                    if rho is not None and abs(rho) > cfg.correlation_threshold:
                        _log.info(
                            "DROP %s (%s): |ρ|=%.3f > threshold=%.3f with "
                            "admitted instrument %s.",
                            instr,
                            score_repr,
                            abs(rho),
                            cfg.correlation_threshold,
                            admitted_instr,
                        )
                        corr_drop = True
                        break

            if corr_drop:
                continue

            # -- Admit -----------------------------------------------------
            admitted.append(candidate)
            admitted_instruments.append(instr)
            for ccy in currencies:
                currency_counts[ccy] = currency_counts.get(ccy, 0) + 1

        return admitted

    # -- helpers -------------------------------------------------------------

    def _load_returns(
        self, instrument: str, start: datetime, end: datetime
    ) -> pd.Series:
        """Load daily candles and return an arithmetic return Series.

        Uses daily (``"D"``) granularity.  If ``load_candles`` raises or
        returns an empty/insufficient DataFrame, an empty Series is returned
        (the correlation check is then skipped — conservative: not dropped).
        """
        try:
            df = self._store.load_candles(instrument, "D", start, end)
        except Exception:
            _log.warning(
                "Could not load candles for %s; skipping correlation check.",
                instrument,
                exc_info=True,
            )
            return pd.Series(dtype="float64")
        return _mid_returns(df)


# ---------------------------------------------------------------------------
# Internal correlation helper
# ---------------------------------------------------------------------------


def _pearson_corr(a: pd.Series, b: pd.Series) -> float | None:
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
