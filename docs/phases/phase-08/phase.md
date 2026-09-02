# phase-08 — Trader companion commands: review, journal, ask, deviation explainer

**Status:** not_started
**Commitment level:** Phase N — additive operator tooling on the phase-07 platform.
**Time horizon:** open — after phase-07
**Depends on:** [`phase-07`](../phase-07/phase.md) (`ai/` package + in-process LLM call pattern + prompt-template convention)
**Unlocks:** nothing hard — [`phase-09`](../phase-09/phase.md) is independent but shares the AI surface

## Purpose

Round out the "AI-supported trader" loop with four read-only companion commands. Each one
takes data Fathom already persists (positions, last-reconciled `account_state`,
deviation log, executed trades, watchlist history), hands it to the LLM with a
purpose-built prompt, and prints advisory text. Nothing here gains order
authority: every command is INV-01-safe read-only analysis; a total LLM outage
degrades each command to "analysis unavailable", never to a wrong action.

Riskiest assumption tested: **LLM commentary over the store's own data is useful enough
that the operator keeps running these commands** — the cheap test of the whole
"AI-supported trader" thesis before phase-09/10 invest in heavier machinery.

## In scope

1. **`fathom review`** — open positions + last-reconciled `account_state` (`as_of`
   freshness, not a persisted reconcile-report — none exists) + deviation log → LLM
   flags anomalies (position near a calendar event, unusual stop distance) with a
   "worth investigating?" flag each. Broker-vs-db `drift_flags` are in-memory-only
   inside `reconcile()` and are **not** a review input this phase.
2. **`fathom journal`** — auto-upsert a journal row on every `fathom execute`
   that reaches `build_bracket` (has a `client_order_id`): dry-run, limits
   reject after minting the id, operator confirm-abort, broker reject, submit
   failure, or submitted fill. Gate aborts *before* `build_bracket` (stale,
   pretrade block, sizing zero, …) write **no** journal row — those verdicts
   already live on [[veto-ledger]] where applicable. `fathom journal
   show|summarize` renders rows and LLM-summarizes patterns. Idempotent UPSERT
   on `client_order_id`.
3. **`fathom ask "<question>"`** — freeform Q&A grounded strictly in store data
   (watchlist, approved set, backtest metrics, positions); refuses questions needing data
   it does not hold rather than speculating.
4. **Deviation-log explainer** — folded into `fathom review` (or `fathom review
   --deviations`): plain-English diagnosis per deviation entry.
5. **Shared plumbing** — one `ai/companion.py` call-shape (context-pack builder + prompt
   template + bounded response) so all four commands are the same pattern, testable
   offline with injected stub clients.

## Out of scope

- Any write path to broker, risk, or execution modules — these commands import store
  readers only; the AST forbidden-import boundary test pattern extends to them.
- Counterfactual veto tracking — [`phase-09`](../phase-09/phase.md).
- Research loop / strategy proposal — [`phase-10`](../phase-10/phase.md).
- Journal entries feeding sizing, ranking, or any automated decision — journal is a
  record + narrative summary only.
- Scheduled/digest modes for these commands — on-demand only, same operator decision as phase-07.
- Panel views for journal/review — CLI only this phase.
- New data capture (no new market data, no broker endpoints beyond what reconcile already reads).

## Done when

- [ ] All four commands run green against the demo store with a live `LLM_*` key and print
      grounded, non-empty analysis; with `LLM_API_KEY` unset each prints its deterministic
      fallback ("analysis unavailable" + raw data tables) and exits 0.
- [ ] `fathom execute` (both dry-run and demo submit **that pass sizing /
      `build_bracket`**) upserts a journal row; a repeated execute of the same
      candidate (same `client_order_id`) does not duplicate it.
- [ ] `fathom ask` answers ≥3 operator questions correctly from store data and visibly
      declines one out-of-scope question without fabricating.
- [ ] AST boundary test proves none of the companion modules import `execution.orders`,
      `execution.models.build_bracket`, `execution.reconcile`, `risk.sizing`,
      `risk.limits`, or `cli`. Reading frozen INV-14 models (`Position` / `Fill` /
      `Order`) returned by `Store` is allowed — that is not order authority.
- [ ] CI green; CLAUDE.md commands + feature INDEX updated.

## Architecture (this phase)

Superset of the phase-07 diagram — adds the companion layer (dashed = new this phase):

```mermaid
graph TD
    subgraph ext["External Systems"]
        OANDA["OANDA v20 API"]
        LLM_API["LLM provider\nOpenAI-compatible"]
        CALENDAR["Economic Calendar"]
        TV["TradingView (Pine paste)"]
    end

    subgraph fathom["Fathom — standalone CLI"]
        CLI["cli.py\nanalyze | pine | review | journal | ask | execute …"]

        subgraph signal_layer["Signal Pipeline"]
            RANKER["ranker.py"]
            PORTFOLIO["portfolio.py"]
        end

        subgraph ai_layer["AI Analysis (ai/)"]
            NEWSRISK["news_risk.py"]
            BRIEF["brief.py"]
            NARRATE["narration.py"]
            PRETRADE["pretrade_check.py"]
            COMPANION["companion.py\nreview · journal · ask · deviation explainer"]
        end

        PINE["pine.py"]
        STORE["store.py\n+ journal table"]
        RECONCILE["reconcile.py\n(not called by review)"]
    end

    CLI --> RANKER --> PORTFOLIO --> NEWSRISK --> BRIEF --> NARRATE
    CLI --> PINE
    STORE --> PINE
    PINE -. paste .-> TV
    CLI --> COMPANION
    STORE --> COMPANION
    NEWSRISK & BRIEF & NARRATE & PRETRADE & COMPANION --> LLM_API
    CLI --> OANDA
    CALENDAR --> NEWSRISK
    RANKER --> STORE
    style COMPANION stroke-dasharray: 5 5
```

## Anticipated specs

| Feature | Hint |
|---|---|
| companion-core | Context-pack builder + shared call shape + offline fallbacks + AST boundary test |
| review-command | Positions + last `account_state` + deviation log + local calendar; anomaly-flag response model; `--deviations` explainer |
| journal | Journal table schema, execute-hook append (idempotent), show/summarize subcommands |
| ask-command | Store-data-only Q&A; visible `REFUSED:`; fixed pack (not an LLM router) |

## Scoping assumptions

- Verified this scoping session: last-reconciled `account_state`, open
  `positions`, and a deviation log are readable from the store (`fathom
  positions`, `fathom reconcile` writes those tables; panel Deviation Log
  view per [`CLAUDE.md`](../../../CLAUDE.md)). There is **no** persisted
  `ReconcileReport` row — `drift_flags` are in-memory only. `fathom review`
  must not call `reconcile()`.
- scoping assumption — verify at spec time: the deviation log's stored fields carry enough
  context (expected vs actual, timestamps, instrument) for a per-entry explanation without
  new capture.
- Verified at spec time: `fathom execute` has **multiple** returns after
  `build_bracket` (limits reject, dry-run, confirm abort ×2, fill,
  OrderRejected, generic submit). [[journal]] attaches a recorder at each;
  there is no single post-gate exit.
