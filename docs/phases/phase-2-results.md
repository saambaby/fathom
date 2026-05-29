# Fathom Phase 2 — Results

**Date:** 2026-05-29
**Verdict:** ✅ **Code-complete and verified end-to-end against live OANDA.**
The entire Hermes-facing pipeline (`backtest → scan → watchlist → chart` + the
INV-02 news-risk boundary + narration fallback + Discord-message assembly) was
exercised locally with real data and produces a coherent ranked watchlist.
⏳ **One residual:** P2-T-08's *live* multi-day Discord delivery is a human-operator
gate (needs a configured Hermes instance + Discord webhook + Anthropic key + ≥5
consecutive weekday runs) — it cannot be executed or automated from the repo.

---

## What shipped (all merged to `main`)

| Task | Feature | Status |
|---|---|---|
| P2-T-01 | `signals/ranker.py` — `Candidate` (INV-13) + INV-10 gate join | ✅ merged |
| P2-T-02 | `signals/portfolio.py` — `PortfolioLimiter` (per-currency / correlation caps) | ✅ merged |
| P2-T-03 | `signals/charts.py` — matplotlib candidate chart PNG | ✅ merged |
| P2-T-04 | `hermes_integration/news_risk.py` — `parse_news_risk` (INV-02 boundary) | ✅ merged |
| P2-T-05 | `hermes_integration/narration.py` — `should_use_fallback` / `fallback_narration` | ✅ merged |
| P2-T-06 | `hermes_integration/jobs/daily.md` + prompts — Hermes job artefact + operator runbook | ✅ merged |
| P2-T-07 | `cli.py` — `fathom scan` / `watchlist` / `chart` (the join) | ✅ merged |
| P2-T-08 | live Discord acceptance (≥5 weekday runs) | ⏳ **operator gate** (below) |

Health: `mypy .` strict = 0 errors (56 files); `pytest` = 667 passed (PR #68).

---

## End-to-end verification (2026-05-29, live OANDA practice)

The full chain the daily Hermes job orchestrates was run locally. This is the
dry-run acceptance from `daily.md → Verifying the setup`, plus the two
Claude-mediated steps exercised through their Fathom-side parsers (no Anthropic
key required for the deterministic guardrails).

### Step 0 — Seed the approved-set (operator prerequisite #3)

```
fathom backtest --instruments EUR_USD,GBP_USD,USD_JPY --timeframes H1,H4,D --strategies all
```
→ **10/72 combos approved** (reproduces the Phase 1A result exactly), persisted to
`data/fathom.db`. Run time ~74 s incl. live candle fetch.

### Step 1 — `fathom scan` (live)

→ Ranker produced **6 candidates; 2 survived PortfolioLimiter** (an EUR_USD combo
dropped on `max_per_currency=2` for USD). Emitted valid INV-13 `Candidate[]` JSON:

| rank | instrument | tf | strategy | dir | oos_sharpe | news_flag |
|---|---|---|---|---|---|---|
| 1 | EUR_USD | D | BollingerReversion(20,2.0) | LONG | 2.000 | false |
| 2 | USD_JPY | D | BollingerReversion(20,2.0) | LONG | 1.027 | false |

### Step 2 — `fathom watchlist` (persisted re-read)

→ Byte-identical to the `scan` stdout (`scan.out == wl.out`). **INV-13 round-trip
through the `watchlist` SQLite table confirmed on real data.**

### Step 3 — `fathom chart EUR_USD --timeframe D`

→ `charts/EUR_USD_D_2026-05-18_21-00-00.png` (1189×590 PNG): candlesticks with
entry / stop / target levels, the signal marker, and a title carrying
strategy · direction · OOS Sharpe · rank.

### Step 4 — INV-02 news-risk boundary (`parse_news_risk`, no key)

Every malformed input fails safe to **`skip`**; every valid input maps correctly:

| input | → suggest_action |
|---|---|
| garbage (not JSON) | `skip` |
| empty string | `skip` |
| out-of-enum action | `skip` |
| missing field | `skip` |
| `{… "suggest_action":"proceed"}` | `proceed` |
| `{… "suggest_action":"reduce_size"}` | `reduce_size` |
| `{… "suggest_action":"skip"}` | `skip` |

(`_SAFE_DEFAULT_ACTION = "skip"`, `event_risk="high"` — a false skip costs an
opportunity; a false proceed costs money.)

### Step 5 — narration fallback (`should_use_fallback`, usability not INV-02)

empty / whitespace / >280 chars → `True` (use deterministic fallback); a good
one-liner → `False` (use Claude's line).

### Step 6 — Discord-message assembly (2 real candidates, stub verdicts)

With stub verdicts (`EUR_USD: proceed`, `USD_JPY: reduce_size`) standing in for
the Hermes-side Claude calls, the assembled post is:

```
Fathom daily scan — watchlist (2026-05-29 UTC)

1. EUR_USD D | BollingerReversion(20,2.0) | LONG
   BollingerReversion(20,2.0) Long on EUR/USD D, OOS Sharpe 2.00.
   [Attachment: charts/EUR_USD_D_2026-05-18_21-00-00.png]

2. USD_JPY D | BollingerReversion(20,2.0) | LONG
   BollingerReversion(20,2.0) Long on USD/JPY D, OOS Sharpe 1.03.
   [NEWS FLAG: reduce_size]
```

### INV-08 — secret-leak check

`scan` / `watchlist` / `chart` / `backtest` stdout+stderr contain neither the
OANDA token nor the account id. ✓ clean across all six output streams.

### INV-01 — order boundary

The job grants Hermes exactly `scan`, `watchlist`, `chart`. No order / execute /
risk tool exists or is referenced. The pipeline ends at message assembly.

---

## Residual: P2-T-08 live Discord acceptance (operator gate, D-P2-5)

Everything that can be verified without external services is verified above. The
*live* acceptance is intentionally a human-admin gate and **cannot run from the
repo** — at the time of writing the three required credentials are absent from
this environment:

| Required | Present here? |
|---|---|
| `OANDA_API_TOKEN` + `OANDA_ACCOUNT_ID` (seed/scan) | ✅ yes |
| `ANTHROPIC_API_KEY` (Hermes Claude calls) | ❌ absent |
| `DISCORD_WEBHOOK_URL` (delivery) | ❌ absent |
| Running Hermes instance with cron | ❌ not configured |

**To close T-08, the operator must:**

1. Stand up a Hermes instance; register `hermes_integration/jobs/daily.md` and the
   `fathom` CLI tool (grant **`scan`, `watchlist`, `chart` only** — INV-01).
2. Set `ANTHROPIC_API_KEY` and `DISCORD_WEBHOOK_URL` in Hermes' `.env`; wire the
   two prompt templates (`prompts/news_risk.md`, `prompts/narration.md`) and the
   response parsers (`parse_news_risk`, `should_use_fallback`).
3. Schedule `0 22 * * 1-5` (or preferred session-close hour).
4. Confirm over **≥5 consecutive weekday runs**: a coherent ranked watchlist lands
   in Discord (charts + Claude rationale + news flags); empty days post "no
   candidates today"; a `skip` verdict vetoes a candidate; no secret in output.
5. Append the live run log to this file.

The full step-by-step is in `hermes_integration/jobs/daily.md → Operator runbook`.

---

## Bottom line

Phase 2 is **code-complete and end-to-end verified on live data**. The only thing
between here and a fully-closed Phase 2 is the operator standing up Hermes +
Discord + an Anthropic key and watching five weekday posts — a deployment/ops
step, not an engineering one. No further Fathom code is required for Phase 2.
Phase 3 (sizing + execution, crossing INV-01) begins only after this gate is
recorded.
