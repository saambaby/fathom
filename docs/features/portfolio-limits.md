# Feature: portfolio-limits

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

Apply portfolio-level filters to the ranker's output so the watchlist isn't five correlated bets that are really one big bet. Consumes the ranked `Candidate` list from [[signal-ranker]] and enforces correlation-aware exposure: highly correlated pairs count as shared exposure, a per-currency cap limits how many candidates lean on the same currency, and a max-concurrent cap bounds the list length. Returns the final, portfolio-filtered ranked list that the CLI emits.

## User-facing behaviour

Backend module `signals/portfolio.py`. `PortfolioLimiter(store, config)` with `apply(candidates: list[Candidate]) -> list[Candidate]`:
- Walk candidates highest-score-first; admit each unless it would breach a limit, else drop it.
- **Correlation:** if a candidate's instrument is highly correlated (above a threshold) with an already-admitted instrument, count them as shared exposure — drop the lower-scored one.
- **Per-currency cap:** at most `max_per_currency` admitted candidates may share a base or quote currency.
- **Max concurrent:** at most `max_concurrent` candidates admitted total.
- Order preserved (still score-ranked); dropped candidates logged with the limit they hit.

## Acceptance criteria

- [ ] Given a ranked list, returns a subset preserving score order, with every admitted candidate respecting all caps.
- [ ] Two highly-correlated instruments (e.g. EUR_USD & GBP_USD above the correlation threshold) are not both admitted — the higher-scored one wins, the other is dropped with a logged reason.
- [ ] `max_per_currency` is enforced (e.g. ≤ N candidates sharing USD).
- [ ] `max_concurrent` bounds the output length.
- [ ] Greedy admission is deterministic (highest score first; stable tie-break).
- [ ] Empty input → empty output (no error).
- [ ] Produces only a filtered watchlist — no sizing/orders (INV-01).

## Component design

`PortfolioLimiter.apply` is a deterministic greedy pass over the score-ranked list. The correlation source is a pairwise correlation computed from recent candle returns via `Store.load_candles` (rolling window), or a static FX correlation grouping as a fallback — see Open questions. `config` carries `correlation_threshold`, `max_per_currency`, `max_concurrent`. Currency extraction splits the OANDA `INSTRUMENT` (`EUR_USD` → base `EUR`, quote `USD`).

## Non-goals

- No position sizing or risk budget (Phase 3 / INV-05 — that's `risk/`).
- No order placement (INV-01).
- No re-scoring — consumes the ranker's scores as-is; only admits/drops.

## Touches

- [INV-01] — output is a filtered watchlist of candidates only; never orders.
- [INV-03] — correlation computed from UTC-stamped candles.

## Depends on

- [[signal-ranker]] — consumes its `Candidate` output (and the pinned `Candidate` wire shape).
- `Store.load_candles` (shipped) — for correlation from returns.

## Approach

New `signals/portfolio.py` beside `ranker.py` (distinct file → parallel-safe with ranker once the `Candidate` shape is fixed, but logically sequenced after it). Greedy admission keeps it simple and deterministic. Correlation from recent daily returns over a rolling window is the honest approach; a static major-pairs correlation grouping is an acceptable Phase-2 fallback if the rolling computation proves noisy.

## Open questions

- Correlation source: rolling return-correlation (data-driven, can be noisy on short windows) vs a static FX correlation grouping (simple, stable, but coarse). Lean: rolling daily-return correlation with a sane min-window; document the threshold (e.g. |ρ| > 0.7).
- Default caps: `max_concurrent` (e.g. 5), `max_per_currency` (e.g. 2) — confirm in Plan.

## Out of scope

- Risk sizing / exposure-in-currency-terms beyond candidate counting (Phase 3).
- The CLI surface ([[cli-commands]]).
