# Fathom Phase 3 — Cross-Spec Audit (2026-05-29)

Run per `runbook-cross-spec-audit` by a fresh, independent, read-only auditor (no
prior session context). Fixes applied afterward by the lead — each finding
annotated with its resolution. Audit + fixes landed together in one PR.

## Scope

The 9 Phase 3 specs (`order-model-and-brackets`, `position-sizing`,
`risk-limits-kill-switch`, `pretrade-check`, `order-placement`, `reconciliation`,
`deviation-monitor`, `monitor-alerts`, `execution-cli`), cross-checked against
`invariants.md`, `phase-3.md`, `code-map.md`, `INDEX.md`, and the shipped contracts
(`Candidate`/INV-13, `Signal`, `data/oanda_client.py::InstrumentMeta`,
`data/store.py`, `hermes_integration/news_risk.py`, `signals/portfolio.py`).

## Summary

18 shared concepts · 7 consistent · **6 blocking drift** · 3 non-blocking drift ·
5 ambiguities · 3 invariant-promotion candidates (all promoted). All blocking items
were bounded spec edits (missing model columns, a phantom `InstrumentMeta.pip_value`,
a non-existent "Hermes gateway" code object, an unproduced `start_of_day_equity`),
not structural rework. All fixed below; corpus is now taskgraph-ready.

## Drift findings & resolutions

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| DRIFT-01 | blocking | Realized P&L named by reconciliation/risk-limits but no model/column carries it; `order-model` punted it | **Fixed** — `order-placement` pins `positions.realized_pl` (+ full `orders`/`fills`/`positions` column lists); `order-model` `Position` now lists `realized_pl`; reconciliation writes it, risk-limits sums it into `day_pl` |
| DRIFT-02 | blocking | `start_of_day_equity` consumed by the kill switch, produced by nothing; account-summary returns *current* equity, not day-open | **Fixed** — reconciliation owns an `account_state` row (`start_of_day_equity`, `day_pl`, `as_of`) snapshotted once on the first reconcile after 00:00 UTC; restart re-reads, never re-snapshots; risk-limits reads it |
| DRIFT-03 | blocking | `client_order_id` scheme stated 3 ways (phase-3 vs order-placement vs "non-empty string"); producer undefined | **Fixed** — one formula pinned in `order-placement`/`order-model`/`execution-cli`: `sha256(instrument:strategy_name:timeframe:generated_at:execution_date)[:32]`, computed in `build_bracket`. Promoted to **INV-15** |
| DRIFT-04 | blocking | `Order.candidate_ref` (provenance) conflated with the `fathom execute` ref (lookup key); CLI ref omitted `generated_at` needed for the id | **Fixed** — `candidate_ref` = `instrument:timeframe:strategy_name` (provenance), distinct from `client_order_id`; CLI loads the full `Candidate` row (incl. `generated_at`) by that ref from the latest watchlist |
| DRIFT-05 | blocking | Realized `day_pl` source stated two ways (account-summary vs sum-of-closed-trades) | **Fixed** — account-summary is authoritative (broker-truth, INV-16); store column mirrors it; broker-day-vs-UTC-day caveat documented in reconciliation |
| DRIFT-06 | blocking | `monitor-alerts` assumes an injectable "Hermes Discord gateway" that does not exist; the monitor is a Python process, not a Hermes job | **Fixed** — replaced with a `DiscordWebhookClient` POSTing to `DISCORD_WEBHOOK_URL` (shared watchlist channel, INV-08) directly from the standalone monitor; phase-3 diagram + execution-boundary updated; no Hermes job involved (INV-01 untouched) |
| DRIFT-07 | blocking | `position-sizing`/`order-model` use `InstrumentMeta.pip_value` + max-trade-size — neither exists on the shipped model | **Fixed** — per-unit risk = `stop_distance × quote_to_account_rate` (uses `pip_location`, no `pip_value` field); max-size clamp dropped (no source; book cap covers it); `build_bracket` `precision` bound to `display_precision` |
| DRIFT-08 | non-blocking | `DeviationEvent` referenced by two specs, defined by neither; `deviation_log` columns unspecified | **Fixed** — `DeviationEvent` pydantic defined in `deviation-monitor` (producer); `deviation_log` column list pinned in `monitor-alerts` (migration owner) |
| DRIFT-09 | non-blocking | `risk-limits` "reuses `portfolio.py` correlation grouping" — no reusable grouping helper exists (logic inlined in `PortfolioLimiter.apply`) | **Fixed** — prerequisite coordinator task extracts the correlation primitive to `signals/correlation.py` (touches shipped `portfolio.py` → coordinator-serialized, flagged in code-map); risk-limits builds bucket-grouping on it; `max_per_correlation_group` ≠ portfolio's `max_per_currency` |

## Ambiguity findings & resolutions

| ID | Finding | Resolution |
|---|---|---|
| AMBIGUOUS-01 | Monitor auto-flatten vs INV-01 — which module performs the close; does it cross into the execution engine? | **Fixed (lead ruling)** — any auto-response is delegated to a deterministic `execution/` function, default-off on demo, never Hermes-reachable. "Deterministic close/modify of an *existing* position is permitted; opening is not." Stated in deviation-monitor + phase-3 boundary |
| AMBIGUOUS-02 | Equity (live) vs quote-conversion rate (cached) freshness mismatch in sizing | **Fixed** — rate source = latest cached candle mid; equity = live account-summary; the sub-rate-drift perturbation of the 0.25% cap is accepted and documented |
| AMBIGUOUS-03 | `fathom execute` may check the kill switch against a stale `day_pl` | **Fixed** — `execute` triggers a fresh reconcile before the limits check |
| AMBIGUOUS-04 | `fathom execute` ref resolvability against the watchlist | **Fixed** — resolves against the latest watchlist run (`load_watchlist(run_timestamp=None)`); stated in execution-cli |
| AMBIGUOUS-05 | `units`/`slippage` sign conventions loosely specified | **Fixed** — `units` signed to match v20 (long>0/short<0); `slippage` signed so positive = adverse; pinned by order-model AC worked examples |

## Invariant promotions (all promoted)

- **INV-14** — `Order`/`Fill`/`Position` are the frozen execution contract (execution-side analogue of INV-13).
- **INV-15** — deterministic `client_order_id`; retries never double-fill (depends on DRIFT-03 being pinned — done).
- **INV-16** — the broker is the source of truth for positions and realized P&L.

Added to `docs/invariants.md`; `order-model-and-brackets` §Touches references INV-14/15; phase-3 active-invariants list updated.

## Consistent (no action)

Gate ordering (pretrade→size→limits→submit) identical across execution-cli/phase-3;
store-table ownership unambiguous (order-placement owns orders/fills/positions +
account_state; monitor-alerts owns deviation_log); `anthropic` coordinator-edit
sequencing mirrors Phase 2's matplotlib pattern; safe-default posture (`block`)
consistent with INV-02; practice-endpoint-only (INV-07/09); reconcile cadence
(startup + 5 min); UTC-day kill-switch reset.

## Action plan — status

All 6 blocking + 3 non-blocking drifts and all 5 ambiguities **Fixed** in this PR;
3 invariants promoted. One **prerequisite coordinator task** surfaced for the
taskgraph: extract `signals/correlation.py` from the shipped `portfolio.py` (before
`risk-limits-kill-switch`).

**Verdict after fixes:** spec corpus coherent; **ready for taskgraph generation.**
