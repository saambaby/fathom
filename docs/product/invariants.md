# Fathom — Invariants

Cross-cutting rules that must never be violated, regardless of phase or implementation detail.
Each invariant has a name, the rule, and the reason — the reason is what lets you judge edge cases.

---

## INV-01 · Hermes Must Not Place Orders

**Rule:** Hermes Agent's autonomous layer ends at producing and delivering the watchlist. It must never directly call order-placement APIs or invoke the execution engine.

**Reason:** Hermes is an always-on agent that reads untrusted text from the internet (news feeds, calendar events). That profile must never hold direct order authority. The worst a prompt-injected headline can do is produce a bad *suggestion*; the deterministic execution layer can reject it. It must never produce a bad *trade*.

**Enforcement:** Execution engine code must not be callable as a Hermes tool. Order placement lives in `execution/orders.py` and is invoked only by the deterministic execution path, never by a Hermes job.

**Enforcement (always-on UI / monitoring surfaces — added Phase 4):** No always-on or operator-facing read surface (`panel/`, the deviation monitor, any future dashboard) may reach order-placement or risk sizing/placement code — **directly or transitively**. Concretely: `panel/` and `monitoring/` must not import `execution.orders`, `execution.models.build_bracket`, `risk.sizing`, or `risk.limits` placement paths, and must not import `cli` (which carries those at module level). A read-only "refresh/scan" affordance must reach the ranker via an order-free entrypoint (`signals/scan.py::run_scan`), not via `cli.cmd_scan`. Enforced by a **transitive-import boundary test** over the surface's module graph — a UI button that can place a trade is exactly the hazard this invariant exists to prevent.

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

**Enforcement (operator-boundary go-live gate — added Phase 5):** The single-code-path rule governs the **execution, risk, and monitoring mechanics** — `risk/sizing.py`, `execution/orders.py`, `execution/reconcile.py`, and the deviation monitor must contain **no** `env`-aware branches and must not read `settings.env`. The go-live **safety gate** is the one sanctioned exception: `execution/live_gate.py` (`assert_live_allowed`, `effective_risk_fraction`) and the `fathom execute`/`fathom preflight` wiring in `cli.py` may read `settings.env`, `settings.live_trading_enabled`, and `settings.live_risk_fraction` **solely to select the operator-boundary gate behaviour and the risk-fraction *input***. It must not alter the mechanics: the same `size_position`/`build_bracket`/`submit_order` code runs demo and live; only the injected `risk_fraction` value and the pre-submit gate differ. `oanda_client.py` remains the only reader of `env` for **endpoint** selection. A test asserts no `env`-aware branch exists in `risk/sizing.py`, `execution/orders.py`, `execution/reconcile.py`, or the monitor.

**Enforcement (demo-only measurement writes — added Phase 9):** Until INV-07 clears, `cli.py` (`fathom execute`) and `signals/analyze.py` (`fathom analyze`) may skip append-only **measurement** writes (`eval.veto_ledger` recording hooks) when `settings.env == "live"`. That skip is operator-boundary telemetry, not a fork of execution/risk/monitoring mechanics: the same `pretrade_check` / `news_risk_check` / sizing / submit code still runs. `eval/` (`veto_ledger.py`, `counterfactual.py`) and `data/store.py` must not read `settings.env`; the skip lives only at those two orchestration call sites. Live verdict recording is out of scope until INV-07.

---

## INV-10 · Approved-Set Gate — No Signal Without Validation

**Rule:** The live pipeline only generates signals from (strategy, pair, timeframe) combinations that appear in the approved-set table produced by the backtester. A strategy that has not completed walk-forward validation with positive out-of-sample metrics must not generate signals in the pipeline.

**Reason:** Running unvalidated strategies on even a demo account produces noise, dilutes the signal quality of approved entries, and builds false confidence.

**Enforcement:** `signals/ranker.py` loads the approved-set table at startup and filters all candidates against it. An empty approved set means no signals (not all signals).

---

## INV-11 · Every Strategy Signal Carries an ATR(14)-Derived Stop and an RR-Multiple Target

**Rule:** Every `Signal` a strategy produces must set `stop_distance` to the 14-bar ATR at the signal bar (Wilder's smoothing — `ewm(com=period-1, adjust=False)`, the shipped `trend.py` formula, exposed once via `strategies/_indicators.py::atr()`), and `target_distance` to `stop_distance × rr_ratio` where `rr_ratio` is a documented fixed parameter (default 1.5). Both must be strictly positive (already enforced by the `Signal` validators). Alternative derivations (range-width stop, band-midline target, etc.) must be proposed as an amendment to this invariant — never implemented unilaterally in one strategy.

**Reason:** Comparable stop distances are the prerequisite for comparable Sharpe ratios and position sizing across strategies. The Phase 2 ranker and the Phase 3 sizing layer treat all strategies symmetrically; a strategy with a structurally different stop/target derivation carries different effective leverage and cannot be ranked or sized against the others fairly. INV-04 requires brackets to exist; INV-11 fixes how they are sized so the approved-set is apples-to-apples.

**Enforcement:** `Signal` validators enforce positivity. All strategies import the single `strategies/_indicators.py::atr()` — no per-file ATR copies. Code review enforces the RR-multiple target. A shared test fixture verifies each shipped `Strategy.generate_signals` produces ATR-consistent stops.

---

## INV-12 · Approved-Set Table Writes Are Single-Writer, Parent-Serialized

**Rule:** The `approved_set` table must be written by exactly one process/thread at a time. In the multi-process backtest runner, worker processes return `ApprovedSetEntry` objects to the parent; the parent performs all inserts in a single transaction. No worker writes directly to the approved-set table.

**Reason:** SQLite without explicit WAL + retry is not safe for concurrent writers. A partially-failed concurrent write produces an incomplete approved-set that INV-10 cannot distinguish from a legitimately small one — silently undermining the gate (some combinations approved but missing, while the runner exits 0).

**Enforcement:** `fathom backtest` collects all `ProcessPoolExecutor` futures into a list before any DB write, then writes the full batch in one `BEGIN…COMMIT`. Reviewed at the runner task.

---

## INV-13 · The `Candidate` Model Is the Frozen Hermes-Facing Wire Contract

**Rule:** `signals/ranker.py`'s `Candidate` pydantic model is the stable output contract of the Phase 2 watchlist pipeline. Its field **names** (snake_case), **types**, and **flat (non-nested) shape** are frozen once the ranker ships. A `fathom watchlist` JSON response is always a JSON array of `Candidate` objects serialised by this model; the Hermes job, charts, narration, and portfolio layer all build against this exact shape. The pinned fields are: `instrument, timeframe, strategy_name, direction, entry_ref, stop_distance, target_distance, oos_sharpe_mean, quality_score, rank, spread_ok, session_ok, news_flag, generated_at` (UTC RFC-3339). Changes to field names/types/shape are **breaking changes to the Hermes integration** and must be treated as an amendment to this invariant.

**Reason:** `Candidate` is consumed by `portfolio.py`, `charts.py`, `cli.py`, `narration.py`, and the Hermes daily job. Once shipped, a silent field rename ripples across all of them and breaks the Discord watchlist. Freezing the contract (as INV-11 freezes the `Signal` stop/target derivation) makes the dependency explicit and reviewable. Note: `timeframe` is the same dimension the approved-set/DB calls `granularity`; the INV-10 gate join is `signal.timeframe == approved_set.granularity`.

**Enforcement:** The field table lives in `docs/features/signal-ranker.md`; `cli-commands`, `chart-generation`, and `watchlist-narration` reference it rather than re-listing fields. A serialisation round-trip test pins the JSON shape. `Candidate` sets `model_config = {"frozen": True}` (pydantic v2), so in-process field assignment raises `ValidationError` at runtime, not just at review time; a test (`tests/test_ranker.py::test_candidate_is_frozen`) pins this. Reviewer checks any `Candidate` field change against this invariant.

---

## INV-14 · The `Order`/`Fill`/`Position` Models Are the Frozen Execution Contract

**Rule:** `execution/models.py`'s `Order`, `Fill`, and `Position` pydantic models are the stable in-process execution contract. Their field names (snake_case), types, and flat shape are frozen once `order-model-and-brackets` ships. `position-sizing`, `order-placement`, `reconciliation`, the deviation monitor, and the alerter all build against this exact shape. A change to field names/types/shape is a breaking change across the execution path and must be treated as an amendment to this invariant.

**Reason:** like `Candidate` (INV-13), these models are consumed by many modules; a silent rename ripples across sizing, placement, reconciliation, and monitoring. Freezing them makes the dependency explicit and reviewable. This is the execution-side analogue of INV-13.

**Enforcement:** a serialisation round-trip test pins each model's JSON shape; the field tables live in `docs/features/order-model-and-brackets.md` (and the persisted column lists in `order-placement.md`), and consumers reference them rather than re-listing fields. `Order`, `Fill`, and `Position` each set `model_config = {"frozen": True}` (pydantic v2), so in-process field assignment raises `ValidationError` at runtime; tests (`tests/test_order_model.py::test_order_is_frozen`, `test_fill_is_frozen`, `test_position_is_frozen`) pin this. Reviewer checks any field change against this invariant.

---

## INV-15 · Every Order Carries a Deterministic Client-Order-ID; Retries Never Double-Fill

**Rule:** every `Order` submitted to OANDA carries a `client_order_id` deterministically derived from `(instrument, strategy_name, timeframe, generated_at, execution_date)`. Submission is idempotent: re-submitting the same `client_order_id` (a network retry, or an operator re-run of `fathom execute`) must return the existing fill, never create a second broker order.

**Reason:** a network retry or an operator re-run must not open a duplicate position. A double-fill costs real money and corrupts the book the kill switch and monitor trust. Idempotency is a first-class safety feature, not an afterthought.

**Enforcement:** `build_bracket()` computes the id; `execution/orders.py` checks the store and attaches the v20 client-extension id before every submit (belt-and-suspenders). A duplicate-submit test asserts exactly one filled position.

---

## INV-16 · The Broker Is the Source of Truth for Positions and Realized P&L

**Rule:** on any disagreement between Fathom's local `positions`/`account_state` and the OANDA account (open trades, account summary), the broker wins: local state is corrected to match. A broker-only position is adopted; a locally-open position the broker has closed is marked closed with its realized P&L; the realized day P&L and equity the kill switch reads are sourced from the broker account summary.

**Reason:** a crashed process or a missed fill must be recoverable, not silently wrong — the kill switch (INV-05 daily cap) and the deviation monitor both trust this state. Operational risk is real risk; reconciliation is a first-class feature.

**Enforcement:** `execution/reconcile.py` applies broker-wins on startup and every N minutes; drift is logged at WARNING, never silently dropped. The kill switch reads `day_pl`/`start_of_day_equity` from the reconciled `account_state` row.

## INV-17 – INV-19 · Reserved — phase-10 AI research loop

Reserved ids, pre-referenced by [`docs/phases/phase-10/phase.md`](../phases/phase-10/phase.md) and [`docs/implementation-plan.md`](../implementation-plan.md): INV-17 (trial ledger — every engine run logged by construction), INV-18 (deflation-gated `approved_set` promotion), INV-19 (frozen evaluation config, agent-unreachable). Recorded here when phase-10.1/10.2 land their specs.

## INV-20 · One LLM Adapter, One Offline Predicate — LLM Call Sites Fail Safe by Default

**Rule:** all in-process LLM traffic goes through the single OpenAI-compatible adapter (`ai/llm_client.py::OpenAICompatClient` from phase-07; today `hermes_integration/pretrade_check.py`), governed solely by `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`. The offline predicate is uniform: a call site with no injected client and no `LLM_API_KEY` set returns its documented safe default with **zero network I/O** — veto-path sites take their INV-02 safe default (news-risk → `skip`, pretrade → abort), advisory sites take their deterministic fallback text. Where a measurement row is written (`analysis_log`, `veto_ledger`), the offline case records `model_id="offline"`; otherwise `model_id` is the `LLM_MODEL` in effect.

**Reason:** promoted during the phase-07–10 spec sprint (2026-09-01): five features (pretrade-check, ai-package-migration, analyze-command, market-brief, veto-ledger) each restated this predicate; one drifting restatement would silently break the ledger's `model_id` join semantics or turn an offline run into a crash or a fabricated verdict.

**Enforcement:** per-call-site offline tests assert the safe default with a socket guard or httpx mock (zero network); key-hygiene tests (INV-08) extend to every new call path; new LLM call sites must import the shared adapter, never construct their own HTTP client.

## INV-21 · One Freshness Definition for Watchlist Candidates

**Rule:** a watchlist candidate is **stale** when its `generated_at` is older than `max_candidate_age_bars` × its **own timeframe's** bar length — one settings field, one bar-length map (`TIMEFRAME_BAR_LENGTH`; today defined in `cli.py`, relocated by phase-07's analyze-command to the leaf module `signals/timeframes.py` so non-CLI surfaces can import it without touching `cli`), shared by every consumer. Consumers differ only in reaction, never in definition: `fathom execute` Step 1.5 **refuses** stale candidates; `fathom pine` **warns** (per-candidate "(stale)" labels + STALE status cell); `fathom analyze` derives its `entry_window_utc` from the same bar-length map. No consumer may define its own TTL or bar lengths.

**Reason:** promoted during the phase-07–10 spec sprint (2026-09-01): three features (execution-cli, pine-generation, analyze-command) depend on "stale" meaning the same thing; a second definition would let a candidate render as fresh on the chart the operator trades from while `fathom execute` refuses it — or worse, the reverse.

**Enforcement:** the setting and the map have a single definition site each; staleness checks import them (no literal TTLs); pine's staleness fixture (stale-H1 + fresh-D) and execute's Step-1.5 tests pin the shared arithmetic.
