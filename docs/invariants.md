# Fathom — Invariants

Cross-cutting rules that must never be violated, regardless of phase or implementation detail.
Each invariant has a name, the rule, and the reason — the reason is what lets you judge edge cases.

---

## INV-01 · Hermes Must Not Place Orders

**Rule:** Hermes Agent's autonomous layer ends at producing and delivering the watchlist. It must never directly call order-placement APIs or invoke the execution engine.

**Reason:** Hermes is an always-on agent that reads untrusted text from the internet (news feeds, calendar events). That profile must never hold direct order authority. The worst a prompt-injected headline can do is produce a bad *suggestion*; the deterministic execution layer can reject it. It must never produce a bad *trade*.

**Enforcement:** Execution engine code must not be callable as a Hermes tool. Order placement lives in `execution/orders.py` and is invoked only by the deterministic execution path, never by a Hermes job.

---

## INV-02 · All Claude Outputs Feeding Automation Must Be Structured JSON with Safe Defaults

**Rule:** Any Claude output that feeds an automated decision (signal ranking, pre-trade check, event-risk assessment) must be structured JSON, validated against a pydantic model. A malformed, low-confidence, or unparseable response must default to the safe action (skip / reduce size), never to "trade anyway."

**Reason:** Unstructured LLM output is too brittle to trust in automated paths. Validation at the boundary means a bad model response fails safely instead of silently.

**Enforcement:** Every `anthropic` SDK call in the pipeline returns a typed pydantic model. Any parse/validation error → log and default to `suggest_action: skip`.

---

## INV-03 · All Timestamps UTC, RFC 3339

**Rule:** All timestamps stored in any medium (database, Parquet, logs, API payloads) must be in UTC and formatted as RFC 3339 strings (e.g. `2026-05-28T14:32:00Z`). No local times, no Unix epoch integers in user-visible output.

**Reason:** Forex trades across time zones; mixing local times is a silent bug factory. Economic calendar events, session opens, and OANDA data are all UTC-referenced.

**Enforcement:** Treat any datetime without explicit UTC timezone as a bug. Use `datetime.now(timezone.utc)`, never `datetime.now()`.

---

## INV-04 · Every Trade Has a Bracket (Stop-Loss + Take-Profit)

**Rule:** No order is submitted to OANDA without attached stop-loss and take-profit bracket orders. No naked positions.

**Reason:** A naked position has unlimited downside in a gap or news event. The risk module's job is bounded risk; that guarantee breaks without brackets.

**Enforcement:** `execution/orders.py` must construct and submit bracket orders atomically. A trade that cannot compute a valid stop distance must be rejected, not submitted naked.

---

## INV-05 · Per-Trade Risk Capped at 0.25% of Equity

**Rule:** No single trade risks more than 0.25% of current account equity. Position size is *derived* from the stop distance and this risk budget — never a fixed lot size.

**Reason:** This cap is the primary defence against a single bad trade doing serious damage. It remains in force on demo and is the baseline for going live.

**Enforcement:** `risk/sizing.py` owns this calculation. The execution engine calls it and rejects any order that would exceed the cap.

---

## INV-06 · Backtests Must Model All Four Cost Categories

**Rule:** No backtest result is considered valid unless it models: (1) spread (bid/ask, not mid), (2) slippage on stop and target fills, (3) commission if the account type charges it, (4) overnight swap/financing for multi-day positions.

**Reason:** A cost-free backtest is fiction. The gap between gross and net returns is typically the difference between a "promising" result and a losing strategy in production.

**Enforcement:** `backtest/costs.py` is a required argument to the engine, not optional. Backtest runner must fail loudly if costs are zero and the strategy holds overnight.

---

## INV-07 · Demo First — No Live Trading Without a Track Record

**Rule:** The system must not connect to the OANDA live (non-practice) account until Phase 4 has completed with a sustained positive, stable edge demonstrated on demo *and* the execution/monitoring plumbing has proven reliable on fake money.

**Reason:** Most retail algo systems lose money in first live iterations. Going live early compounds strategy risk with operational risk before either has been validated.

**Enforcement:** `config/settings.py` has an `env: demo | live` switch. Live mode requires an explicit override. No Phase < 4-complete code should reference the live token.

---

## INV-08 · Secrets Never Committed

**Rule:** API tokens, account IDs, database credentials, and all other secrets live in `.env` (excluded from git via `.gitignore`). The `.env.example` file documents required keys by name only — never values.

**Reason:** A committed secret is permanently compromised, even if later removed from history.

**Enforcement:** `.gitignore` includes `.env`. Pre-commit check should scan staged files for `OANDA_API_KEY=` or similar patterns with values.

---

## INV-09 · Demo and Live Share One Code Path

**Rule:** Demo and live must use the exact same execution, risk, and monitoring code path, differentiated only by the `env` config switch (which selects the OANDA practice vs live endpoint and the corresponding token).

**Reason:** A separate "live mode" code path defeats the validation purpose of demo. What runs on demo is what goes live.

**Enforcement:** No `if env == 'live':` branches in logic code. Only `oanda_client.py` reads `env` to select the endpoint.

---

## INV-10 · Approved-Set Gate — No Signal Without Validation

**Rule:** The live pipeline only generates signals from (strategy, pair, timeframe) combinations that appear in the approved-set table produced by the backtester. A strategy that has not completed walk-forward validation with positive out-of-sample metrics must not generate signals in the pipeline.

**Reason:** Running unvalidated strategies on even a demo account produces noise, dilutes the signal quality of approved entries, and builds false confidence.

**Enforcement:** `signals/ranker.py` loads the approved-set table at startup and filters all candidates against it. An empty approved set means no signals (not all signals).
