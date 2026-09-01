# Fathom Master Implementation Plan

> **For any coding agent (Claude Code, Cursor, Copilot, or a human):** execute this plan task-by-task — one task = one test-first change cycle = one PR, reviewed by someone (or some agent) who did not write it. Steps use checkbox (`- [ ]`) syntax for tracking. Workstreams 2.1 and 3–4 require a written feature spec (per the Layer-4 process in the `halfcycle` plugin — `/halfcycle:write-spec`, templates beside [`docs/features/INDEX.md`](features/INDEX.md)) before their tasks are executable — do not improvise those. *Claude Code users only:* the superpowers subagent-driven-development / executing-plans skills and the `halfcycle:*` commands automate this loop, but nothing in this plan depends on them.

**Date:** 2026-08-31
**Goal:** Take Fathom from "code-complete but idle, with known correctness bugs and an uncapturable edge" to a provider-agnostic, capture-capable trading system with an AI-agent research loop that is statistically honest by construction.

**Architecture:** Two planes joined at one table. The **trading plane** (exists: scan → rank → veto → size → limit → execute → reconcile) stays deterministic and keeps every invariant; this plan fixes its bugs and closes its capture gap. The **research plane** (new: agent → constrained proposals → backtest-as-a-tool API → trial ledger → deflation gate) is where AI lives; it can only reach the trading plane through `approved_set`, behind the walk-forward gate plus a new multiple-testing statistics gate plus a human decision.

**Tech Stack:** Python 3.11, pydantic v2 (+pydantic-settings), httpx, pandas, SQLite, pytest/mypy(strict)/responses/hypothesis, OANDA v20 REST, MCP (research API), any OpenAI-compatible LLM endpoint.

**Sources:** this plan is self-contained — every task carries its own rationale, file targets, and acceptance criteria, and the repo docs ([`docs/product/invariants.md`](product/invariants.md), [`docs/phases/phases-manifest.json`](phases/phases-manifest.json), [`docs/operator-acceptance.md`](operator-acceptance.md)) are the authoritative ground truth. Three supplementary write-ups from the 2026-08-31 audit/research session (*Fathom Audit*, *Fathom Edge Roadmap*, *Fathom AI Research Loop*) exist as private Claude artifacts with fuller evidence and citations; they are background reading, and no task below requires access to them.

## Global Constraints

- **INV-01:** no LLM/agent code path may import or reach order placement; live execution stays operator-only.
- **INV-02:** every LLM output crossing into automation is typed JSON with a fail-closed safe default (veto → `block`).
- **INV-05:** per-trade risk ≤ 0.25% (demo) / `live_risk_fraction` ≤ 0.25%, validated at Settings construction.
- **INV-07:** no live trading without a demo track record; this plan gives it quantitative teeth (MinTRL) but never weakens it.
- **Merge gate:** whole-repo `mypy .` clean + full `pytest` green before every merge; merge via `gh pr merge` (squash) — standing hygiene.
- **TDD:** every behavior change lands test-first (RED verified before implementation).
- **No-mocks evidence rule:** nothing counts as evidence a strategy works unless it came from real recorded market data through the real engine with full costs, or from real demo fills — see the Strategy Verification Protocol section. Mocks/stubs are permitted only for external-HTTP plumbing tests, never anywhere in a run cited as strategy evidence.
- **Commits:** plain conventional-commit messages; no AI-attribution trailers, footers, or co-author sign-offs of any kind.
- **Docs discipline:** any new CLI command, dependency, doc file, or invariant updates CLAUDE.md / `docs/product/invariants.md` / `docs/features/INDEX.md` per the CLAUDE.md trigger table.

---

## Strategy Verification Protocol (real data, real engine — strictly no mocks)

This section defines what counts as **evidence that a strategy works**. Every workstream that touches strategies (Tasks 3.1, 3.6, 3.7, and all of Workstream 4) verifies against this protocol. It exists because the cheapest way to fool yourself is to "verify" a strategy against fabricated data, a stubbed engine, or an LLM's opinion of a backtest.

### The no-mocks rule

**Admissible as evidence of edge (in escalating order of strength):**
1. Real historical OANDA candles (the cached `candles` table / Parquet archive, fetched from the live v20 API) run through the **real** event-driven engine with the **full** cost model — spread + slippage + commission + swap (INV-06), never a zero-cost or reduced-cost run.
2. The same, gated by walk-forward OOS windows and the trial-corrected statistics (DSR/PBO/MinTRL, Workstream 4 Phase B).
3. A live **demo forward run** — real broker fills on the practice account over calendar time (the INV-07 track record). This is the terminal verification; nothing substitutes for it.

**Never admissible as evidence of edge:**
- Synthetic, fabricated, or hand-constructed price series presented as validation. (Permutation/bootstrap tests on shuffled *real* returns are allowed — but only as **null-hypothesis destroyers**: they can kill a strategy, never approve one, and every report must label them as such.)
- A mocked or stubbed engine, mocked fills, mocked costs, or metrics computed outside `backtest/metrics.py`.
- Any LLM-produced number: an LLM may propose strategies and narrate results; it may never simulate, estimate, or "sanity-check" a backtest result into existence (this is also load-bearing against parametric look-ahead bias — see Workstream 4).
- In-sample performance, single-window results, or any run whose trial wasn't logged (once INV-17 lands).

**Scope note for the unit-test suite:** the existing tests stub *external HTTP* (the `responses` library for the OANDA REST client, patched `httpx` for Discord) — that is correct and stays: those tests verify wire-format plumbing, not strategies, and hitting a live broker in CI would be flaky and slow. The no-mocks rule governs **strategy verification**: engine behavior, strategy logic, cost math, and metrics must be exercised against real recorded market data (see the fixture task below), and no mocked component may sit anywhere in a run that is later cited as evidence a strategy works.

### Task V.1: Real-data regression fixture (prerequisite for everything below)

**Files:**
- Create: `tests/fixtures/candles_real/` (Parquet: EUR_USD + USD_JPY, H4 + D, a contiguous ≥3-year slice of real OANDA candles), `tests/fixtures/candles_real/PROVENANCE.md`
- Create: `tests/test_engine_real_data.py`

- [ ] **Step 1:** Export the slice from the live candle cache (`fathom backtest --dry-run` populates it; or fetch once via `data/candles.py`) to Parquet. Record in `PROVENANCE.md`: instrument, granularity, exact UTC time range, fetch date, row counts, and the SHA-256 of each file. The fixture is real market data, committed to git, never regenerated silently — any refresh is a reviewed PR that updates the hashes.
- [ ] **Step 2 (failing test first):** `test_fixture_integrity` — files exist, hashes match `PROVENANCE.md`, timestamps are strictly increasing UTC with no duplicates, and gap count ≤ the documented weekend/holiday gaps.
- [ ] **Step 3 (failing test first):** `test_engine_end_to_end_on_real_data` — run the **real** `BacktestEngine` + `compute_metrics` on the fixture with the real default cost model for one pinned strategy config (`BollingerReversion(20,2.0)` on EUR_USD D), and assert the exact metric values (Sharpe, trade count, expectancy to 6 decimal places). This pins engine + costs + metrics against real data; any behavior change in the pipeline breaks it loudly.
- [ ] **Step 4:** `test_walkforward_on_real_data` — run `WalkForwardValidator` on the fixture, assert window boundaries, per-window trade counts, and the approval verdict. No stubs anywhere in these three tests.
- [ ] Commit `test: real-data regression fixture + end-to-end engine pin (no mocks)`.

### The verification ladder — every strategy climbs all of it

A strategy (existing, new sleeve, or agent-proposed) is **approved for demo** only after passing every rung; it is **eligible for live consideration** only after rung 7. Record every rung's output in the trial ledger (post-INV-17) and the results doc.

| # | Rung | Concrete gate |
|---|------|---------------|
| 1 | **Data integrity pre-check** | Candle range covers `--history-years` for every instrument/timeframe in the run; gap scan passes; all UTC (INV-03). A run on incomplete data is void, not "approximate". |
| 2 | **Full-cost walk-forward on real data** | `fathom backtest --instruments ALL --history-years 5` (5y minimum so D gets ≥5 six-month OOS windows, H4 ≥7, H1 ≥16 — the 3y default's single D window is *not* acceptable verification). Per-window gate: every OOS window Sharpe > 0 (correctly annualized per Task 0.3) AND ≥ 5 trades; **plus** total OOS trades ≥ 20 per combo, or the result is labeled "statistically meaningless" (the metrics module already warns — the gate must enforce it, not just warn). |
| 3 | **Trial-corrected statistics** (once Phase B lands; until then, record trial counts manually in the results doc) | DSR ≥ 0.95 at the honest trial count N; PBO ≤ 0.20 via CSCV over the full trial matrix; MinTRL computed and recorded ("this Sharpe needs X more demo days at 95% confidence"). |
| 4 | **Parameter plateau** | Perturb every strategy parameter ±10% and ±20%; OOS Sharpe must not fall by more than half at any perturbation. An isolated spike is a rejection. |
| 5 | **Null-hypothesis destruction** | Trade-sequence bootstrap (1,000 resamples of the real trade P&L): the 5th-percentile terminal equity must remain above the max-drawdown budget. Bar-level permutation test on shuffled real returns: the real OOS Sharpe must exceed the 95th percentile of ≥ 200 permuted runs. Both use real data as the base; both can only reject. |
| 6 | **Regime slices** | Split the OOS period into thirds by calendar and by realized-volatility terciles; the strategy must be profitable (or at worst flat) in at least 2 of 3 of each split — an edge that lives entirely in one regime is documented as conditional, not approved unconditionally. |
| 7 | **Demo forward run** | The strategy trades the live practice account (via the watcher/demo autopilot once built, manually until then) for at least its MinTRL horizon or 30 calendar days, whichever is longer, with every fill reconciled broker-truth (INV-16). Forward Sharpe within the bootstrap confidence band of the backtest — a forward run wildly outside the band voids the approval regardless of how good the backtest looked. |

**Standing rules:** re-verification (rungs 1–3 minimum) is required after any change to the engine, cost model, metrics, or the strategy's own code — the pinned real-data regression test (Task V.1) is the tripwire. The approved set is rebuilt only by a full protocol run, never hand-edited (INV-12). And per the honesty principle in Workstream 4: passing this ladder means "not yet shown to be luck," never "guaranteed profitable."

---

## Current state (context for a zero-context engineer)

Phases PoC–5 are code-merged; the repo has been idle since 2026-05-30 with four operator gates open (#59, #86, #109, #123). A full audit (2026-08-31) found: one critical real-money bug (INV-15 idempotency broken for operator re-runs), an INV-07 attestation that is hardcoded at the point of execution, a go-live runbook that deadlocks, a deviation monitor wired to a no-op alerter, operator docs with wrong env-var names, a cross-timeframe Sharpe mis-annualization that biases the ranker, a sizing fallback that can mis-size non-USD pairs ~150×, no candidate-freshness check, machine-specific hardcoded test paths, unpinned deps, and no CI. Separately, the approved edge is thin (10/72 combos, 3 pairs only) and structurally uncapturable at a once-daily manual cadence.

**Work already in flight:** Workstream 1 (LLM provider swap) has verified-RED tests committed to the working tree and a half-edited module — see its status note. Nothing else is started.

---

## Workstream 0 — Correctness & audit fixes (P0, do first)

Everything downstream (research loop included) inherits these metrics and gates; fixing them first is not optional. Each task is one PR.

### Task 0.1: INV-15 idempotency — client-order-id must use the UTC *date*

**Files:**
- Modify: `execution/models.py:414-427` (`_client_order_id`)
- Test: `tests/test_order_model.py`

**Interfaces:** `_client_order_id(candidate, execution_date: datetime) -> str` (signature unchanged; only the interpolation changes to `execution_date.date().isoformat()`, matching [`docs/features/order-placement.md:80-86`](features/order-placement.md) verbatim).

- [ ] **Step 1: Write the failing tests**

```python
def test_same_day_rerun_yields_identical_client_order_id() -> None:
    """INV-15: an operator re-run the same UTC day must dedup (same id)."""
    c = _make_candidate()
    d1 = datetime(2026, 8, 31, 9, 15, 3, 123456, tzinfo=timezone.utc)
    d2 = datetime(2026, 8, 31, 17, 42, 59, 999999, tzinfo=timezone.utc)
    assert _client_order_id(c, d1) == _client_order_id(c, d2)

def test_next_day_rerun_yields_distinct_client_order_id() -> None:
    c = _make_candidate()
    d1 = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    d2 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert _client_order_id(c, d1) != _client_order_id(c, d2)
```

- [ ] **Step 2: Run, verify FAIL** — `pytest tests/test_order_model.py -k client_order_id -v` → the same-day test fails (ids differ today).
- [ ] **Step 3: Implement** — in `_client_order_id`, interpolate `execution_date.date().isoformat()` instead of `execution_date`; update the docstring to quote the spec's "UTC date of the run".
- [ ] **Step 4: Reconcile the existing pin** — `tests/test_order_model.py::test_client_order_id_changes_with_execution_date` uses `timedelta(days=1)`; it must still pass unchanged.
- [ ] **Step 5: Full gate** — `pytest` + `mypy .` clean. Commit `fix(execution): INV-15 client-order-id uses UTC date so same-day re-runs dedup`.

### Task 0.2: Reject, don't warn, on missing quote→account conversion rate

**Files:**
- Modify: `cli.py:1586-1616` (sizing conversion fallback)
- Test: `tests/test_execution_cli.py`

- [ ] Write failing test: with no cached close for the conversion pair, `cmd_execute` returns non-zero and no order is built (mock the store to return no rate; assert `build_bracket` not called).
- [ ] Implement: replace the `rate = 1.0` fallback with an abort (log + stderr `SIZING REFUSED: no conversion rate for <pair>`, return 1).
- [ ] Full gate; commit `fix(cli): refuse to size when quote->account conversion rate unavailable`.

### Task 0.3: Per-timeframe Sharpe annualization

**Files:**
- Modify: `backtest/metrics.py` (`compute_metrics`, `_sharpe`, `_sortino`), `backtest/walkforward.py` + `cli.py:595` call sites
- Test: `tests/test_metrics_and_walkforward.py`

**Interfaces:** `compute_metrics(result, risk_free_rate=0.0, periods_per_year: float = 252.0) -> Metrics`. Callers pass per-timeframe values: D=252, H4=252×6=1512, H1=252×24=6048 (FX trades ~24h weekdays). Annualization factor becomes `sqrt(periods_per_year)`.

- [ ] Failing test: identical per-bar return series evaluated as H4 yields Sharpe = D-Sharpe × √6.
- [ ] Implement; thread `periods_per_year` from `WINDOW_CONFIG`/timeframe at both call sites; keep default 252 for backward compatibility of direct callers.
- [ ] **Consequence check (test):** ranker ordering over a mixed D/H4 approved set changes accordingly — pin one regression case in `tests/test_ranker.py`.
- [ ] Full gate; commit. **Then re-run `fathom backtest` before trusting any ranking again** (Workstream 3, Task 3.1 does this).

### Task 0.4: Candidate freshness TTL in the execute gate

**Files:**
- Modify: `cli.py` (`cmd_execute`, after candidate load ~line 1384), `config/settings.py`
- Test: `tests/test_execution_cli.py`

**Interfaces:** `Settings.max_candidate_age_bars: float = 1.0` (candidate expires after this many bars of its own timeframe). Timeframe bar lengths: H1=1h, H4=4h, D=24h.

- [ ] Failing tests: an H4 candidate with `generated_at` 5h old is refused (exit non-zero, no gate steps run); a 3h-old one proceeds; `--dry-run` reports the same refusal.
- [ ] Implement deterministic age check immediately after candidate load; refusal message names the age and the limit.
- [ ] Full gate; commit `feat(cli): reject stale candidates (freshness TTL, closes the missing staleness gate)`.

### Task 0.5: Wire the real Discord alerter into the monitor

**Files:**
- Modify: `scripts/run_monitor.py:142-143`
- Test: `tests/test_monitor_alerts.py`

- [ ] Failing test: `run_monitor` module wiring uses `build_alerter_from_settings(store)` (`monitoring/alerts.py:288`) when `DISCORD_WEBHOOK_URL` is set, `NoOpAlerter` otherwise (test via a small extracted `_build_alerter(store)` helper).
- [ ] Implement; delete the stale "T-09 will replace this" comment.
- [ ] Full gate; commit `fix(monitoring): run_monitor uses the real alerter (T-09 shipped, entrypoint never wired)`.

### Task 0.6: Live-gate honesty — attestation and runbook order

**Files:**
- Modify: `cli.py:1496-1502`, `execution/preflight.py:347-357`, `docs/go-live-runbook.md`, `docs/operator-acceptance.md`
- Test: `tests/test_live_gate.py`, `tests/test_preflight.py`

Two coordinated changes (one PR — a reviewer must see them together):

- [ ] Failing test A: `run_preflight` persists a GO record (new `preflight_attestations` row: timestamp, account id, attested flag) and `cmd_execute`'s live path reads the **latest persisted attested-GO within 24h** instead of passing `attested=True`; absent/stale → live refused.
- [ ] Failing test B: preflight's `env_flag_token_consistency` with `ENV=live` and flag off becomes a **named warning check that does not veto GO when every other check passes and `--pre-cutover` is passed** (`fathom preflight --pre-cutover`), so runbook Step 2 is satisfiable; without `--pre-cutover` current behavior stands.
- [ ] Implement both; update the runbook Steps 1–3 and `operator-acceptance.md` Gate 4 to the now-consistent sequence.
- [ ] Full gate; commit `fix(live-gate): execute reads a persisted preflight attestation; runbook deadlock resolved`.

### Task 0.7: Operator docs that actually work

**Files:**
- Modify: `hermes_integration/jobs/daily.md:239-248`, `docs/operator-acceptance.md:31-37,75-76`, `docs/go-live-runbook.md:142`, `CLAUDE.md:69`, `cli.py:826,1343`

- [ ] Fix env-var names (`OANDA_API_TOKEN`, `ENV`); document that the LLM key must be **exported** (or set via the new Settings field from Workstream 1); quote every candidate-ref containing parens; use real ref format `EUR_USD:D:BollingerReversion(20,2.0)` everywhere (CLI help text included); `python scripts/run_monitor.py`.
- [ ] Test: extend `tests/test_execution_cli.py` doc-lint test to grep the docs for the two dead env-var names and the invented ref formats (fails if they reappear).
- [ ] Commit `docs: operator commands and env vars match the code`.

### Task 0.8: De-machine the test suite + pin the environment + CI

**Files:**
- Modify: `tests/test_execution_cli.py:805-820`, `tests/test_panel_data.py:875-911`, `pyproject.toml`
- Create: `.github/workflows/ci.yml`, `requirements-lock.txt` (or `uv.lock`)

- [ ] Replace both `cwd="/home/sam-baby/development/fathom"` with `pathlib.Path(__file__).parent.parent` and `[sys.executable, "-m", "cli"]`-style invocation (no `.venv/bin/fathom` assumption); verify both tests pass on this machine.
- [ ] Fix the two `unused-ignore` mypy errors in `data/store.py:1640,1695`.
- [ ] Add a lockfile; CI workflow runs `pytest` + `mypy .` on 3.11 on every PR.
- [ ] Deflake `tests/test_stream.py::TestPriceStreamReconnect::test_reconnect_on_heartbeat_timeout` (passes alone, fails under load — widen its heartbeat-timeout margin).
- [ ] Commit `chore: portable tests, locked deps, CI merge gate`.

### Task 0.9: Freeze the frozen contracts

**Files:**
- Modify: `signals/ranker.py:111` (`Candidate`), `execution/models.py:109,172,222` (`Order`/`Fill`/`Position`)
- Test: `tests/test_ranker.py`, `tests/test_order_model.py`

- [ ] Failing tests: assigning to any field of a constructed `Candidate`/`Order`/`Fill`/`Position` raises `ValidationError`.
- [ ] Add `model_config = ConfigDict(frozen=True)` to each (merge with existing config dicts); fix any in-repo mutation sites the suite surfaces (expected: none — the audit found mutation is only a latent risk).
- [ ] Full gate; commit `feat(contracts): INV-13/14 models are runtime-frozen, not frozen-by-convention`.

**Workstream 0 done when:** fresh-clone `pytest` and `mypy .` are green in CI, and every audit finding marked Critical/High is closed or explicitly deferred with an issue. (Low/hygiene audit items — features INDEX status sync, stale remote branch pruning, code-map refresh — are folded into the end-of-workstream docs/context sync pass, not separate tasks.)

---

## Workstream 1 — LLM provider swap (OpenAI-compatible adapter) · **IN PROGRESS**

**Status (2026-08-31):** RED phase complete and verified — but **only as uncommitted changes in the git worktree at `.claude/worktrees/project-audit-e3e078` (branch `claude/project-audit-e3e078`)**. If you are working from that tree: failing tests exist in `tests/test_pretrade_check.py` (`TestOpenAICompatClient`, updated `TestPretradeCheckOfflinePath`, public-names assertion), `tests/test_config.py::TestLlmClientFields`, and `tests/test_execution_cli.py::TestLlmClientFromSettings`; `hermes_integration/pretrade_check.py` has the new module header/constants (`MODEL = "gpt-5-nano"`, `DEFAULT_BASE_URL`, httpx import) but **still contains the anthropic `_LiveClient`** — the module is mid-edit. Resume from Task 1.1; do not rewrite the tests.

**If your checkout does NOT contain those changes** (fresh clone, different tool), run Task 1.0 first to recreate the RED state, then continue.

### Task 1.0 (only if the RED tests are absent): recreate the failing tests

Write these tests, run them, and verify they FAIL with import/attribute errors before writing any implementation:

- `tests/test_pretrade_check.py` — new class `TestOpenAICompatClient` (mock `hermes_integration.pretrade_check.httpx.Client` with a stub whose `post` captures url/headers/json and returns an object with `raise_for_status()` and `json()`):
  - `test_posts_openai_chat_completions_wire_format` — `OpenAICompatClient(api_key="test-key-123", base_url="https://example.test/v1", model="test-model").complete("hello prompt")` posts to `https://example.test/v1/chat/completions` with header `Authorization: Bearer test-key-123` and body `{"model": "test-model", "messages": [{"role": "user", "content": "hello prompt"}]}`, returning `choices[0].message.content`.
  - `test_missing_choices_raises` / `test_non_string_content_raises` — empty `choices` or non-string `content` → `ValueError`.
  - `test_http_error_propagates_to_caller` — `raise_for_status()` raising propagates (caller's except → block).
  - `test_api_key_not_in_default_repr` — the key never appears in `repr()`/`str()` of the client (INV-08).
  - `test_defaults_from_env` / `test_from_env_without_key_returns_none` — `OpenAICompatClient.from_env()` reads `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`, defaults to `DEFAULT_BASE_URL` and `MODEL`, returns `None` when `LLM_API_KEY` is unset.
- `tests/test_pretrade_check.py::TestPretradeCheckOfflinePath` — rewrite the three existing offline tests to key on `LLM_API_KEY` (monkeypatch `delenv`) instead of `ANTHROPIC_API_KEY`, and add `test_anthropic_key_alone_no_longer_activates_live_path` (set `ANTHROPIC_API_KEY`, unset `LLM_API_KEY` → safe-default block).
- `tests/test_pretrade_check.py` public-names test — add `assert "OpenAICompatClient" in public_names`.
- `tests/test_config.py` — new class `TestLlmClientFields` (follow the existing `TestLiveTradingGateFields` fixture pattern): `llm_api_key` defaults to `None` and is a `SecretStr` absent from `repr(settings)`; `llm_base_url` defaults to `"https://api.openai.com/v1"`; `llm_model` equals `hermes_integration.pretrade_check.MODEL`.
- `tests/test_execution_cli.py` — new class `TestLlmClientFromSettings`: `cli._llm_client_from_settings(settings)` returns `None` when `llm_api_key` is `None`, and a client carrying the settings' `base_url`/`model` when a `SecretStr` key is present.

Also apply the module-header half of the edit: in `hermes_integration/pretrade_check.py`, set `MODEL = "gpt-5-nano"`, add `DEFAULT_BASE_URL = "https://api.openai.com/v1"` and `_HTTP_TIMEOUT_S = 30.0`, add `import httpx`, and update the docstring's key references from `ANTHROPIC_API_KEY` to `LLM_API_KEY`.

**Design (settled):** the SDK touchpoint is exactly one class behind the `_ClientAdapter` protocol (`complete(prompt) -> str`). Replace it with a provider-agnostic client speaking the OpenAI chat-completions wire format via httpx (already a dependency — the `anthropic` package is then removable). Config precedence: injected client (from Settings, `.env`-aware) → `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` env vars → offline fail-closed `block`. Works with OpenAI, Groq, NVIDIA NIM, OpenRouter, Gemini-compat, local Ollama. Fail-closed semantics unchanged (INV-02). Note from research: free tiers are fine for experiments, but the veto gate fails closed on provider outage — a paid nano-tier model (<$1/month at this volume) or local Ollama is the reliability-correct default.

### Task 1.1: `OpenAICompatClient` (GREEN for the existing RED tests)

**Files:**
- Modify: `hermes_integration/pretrade_check.py` (replace `_LiveClient` block, lines ~215-252)

**Interfaces (produced, already consumed by the RED tests):**

```python
class OpenAICompatClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 model: str = MODEL) -> None: ...   # stores key privately; repr excludes it
    base_url: str
    model: str
    def complete(self, prompt: str) -> str: ...      # POST {base_url}/chat/completions,
                                                     # Authorization: Bearer <key>,
                                                     # body {"model": model, "messages":[{"role":"user","content": prompt}]},
                                                     # raise_for_status; returns choices[0].message.content;
                                                     # non-string/missing content -> ValueError
    @classmethod
    def from_env(cls) -> "OpenAICompatClient | None": ...  # None when LLM_API_KEY unset
```

- [ ] Implement the class exactly to that contract (use `httpx.Client(timeout=_HTTP_TIMEOUT_S)` as a context manager so the mock pattern in the tests holds; `__repr__` returns `OpenAICompatClient(base_url=..., model=...)`).
- [ ] Delete `_LiveClient` and the `anthropic` imports; update `_ClientAdapter` docstring.
- [ ] Update `pretrade_check` step 1/2: gate on `LLM_API_KEY`; live client via `OpenAICompatClient.from_env()`.
- [ ] Run `pytest tests/test_pretrade_check.py -v` → all green, including the four previously-passing offline tests and `test_anthropic_key_alone_no_longer_activates_live_path`.
- [ ] Commit `feat(pretrade): provider-agnostic OpenAI-compatible LLM adapter (replaces anthropic SDK)`.

### Task 1.2: Settings fields + `.env` awareness

**Files:**
- Modify: `config/settings.py`, `.env.example`

- [ ] Add to `Settings`: `llm_api_key: Optional[SecretStr] = None`, `llm_base_url: str = "https://api.openai.com/v1"`, `llm_model: str = "gpt-5-nano"` (import nothing from `hermes_integration` — keep the value literal; the equality test pins them together).
- [ ] Add `LLM_API_KEY=`, `LLM_BASE_URL=`, `LLM_MODEL=` to `.env.example` with one-line comments (the drift-guard test `test_env_example_keys_match_settings_fields` enforces this).
- [ ] Run `pytest tests/test_config.py -v` → `TestLlmClientFields` green.
- [ ] Commit `feat(config): llm_* settings fields (.env-aware LLM configuration)`.

### Task 1.3: CLI injection

**Files:**
- Modify: `cli.py` (new `_llm_client_from_settings`, and `cmd_execute` line ~1546)

```python
def _llm_client_from_settings(settings: "Settings") -> "OpenAICompatClient | None":
    if settings.llm_api_key is None:
        return None
    return OpenAICompatClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
```

- [ ] Wire: `verdict = pretrade_check(candidate, client=_llm_client_from_settings(settings))` (client `None` → existing env/offline path).
- [ ] Run `pytest tests/test_execution_cli.py::TestLlmClientFromSettings -v` → green.
- [ ] Commit `feat(cli): pretrade veto LLM client built from Settings`.

### Task 1.4: Sweep the residue

- [ ] Remove `anthropic>=0.39` from `pyproject.toml`; update CLAUDE.md Stack + Documentation rows; update `docs/features/pretrade-check.md` and `docs/operator-acceptance.md` (`ANTHROPIC_API_KEY` → the `LLM_*` scheme); `grep -rn ANTHROPIC_API_KEY` must return only historical phase docs.
- [ ] Full gate (`pytest` + `mypy .`); commit `chore: complete the anthropic->OpenAI-compatible migration`.

**Note:** `news_risk.py` and `narration.py` are invoked Hermes-side (prose job definitions, not the SDK) — swapping their model is a `hermes_integration/jobs/daily.md` config edit, folded into Task 0.7's doc pass.

---

## Workstream 2 — TradingView posture (decisions, small tasks)

**Decisions (researched, settled):** TradingView has no retail API; its broker link to OANDA is manual-UI-only; all "TradingView MCP" servers are ToS-violating scrapers — **nothing TradingView-derived enters the automated pipeline.** TradingView remains the human dashboard. Charts already use TradingView's official open-source renderer (`streamlit-lightweight-charts`) — no change. Execution and data stay on OANDA v20.

### Task 2.1: Fathom read-only MCP server (the useful inversion)

**Files:**
- Create: `mcp_server/server.py`, `tests/test_mcp_server.py`
- Spec first: `docs/features/fathom-mcp.md` (Layer-4 feature spec; follow the format of existing files in `docs/features/`)

- [ ] Spec: expose exactly the INV-01-safe read-only surface as MCP tools — `get_watchlist` (Candidate[] JSON via `signals.scan` persisted watchlist), `get_positions`, `get_reconcile_report`, `render_chart` (path to PNG), `run_scan` (order-free refresh). No execute, no preflight, no settings mutation. AST-boundary test mirroring `tests/test_admin_panel.py`'s forbidden-import probe.
- [ ] Implement per spec (Python `mcp` SDK, stdio transport), TDD.
- [ ] Register in CLAUDE.md commands + features INDEX.

### Task 2.2 (optional, deferred): TradingView alert-webhook intake

Only if wanted later: a `POST /tv-alert` endpoint on the watcher server (Workstream 3) that logs Pine-alert payloads into `deviation_log`-style storage as *notifications* — never as signals (they can't pass the walk-forward gate). Requires TradingView paid plan + 2FA. Not scheduled.

---

## Workstream 3 — Capture & breadth (the Edge Roadmap, code side)

Each task needs a Layer-4 feature spec before implementation; the list below fixes scope and order, and each bullet carries the essential design decision so the spec author needs nothing outside this repo.

- [ ] **Task 3.1 — Full-universe, longer-history backtest run** (after Tasks 0.3 and V.1): `fathom backtest --instruments ALL --history-years 5`, executed as rungs 1–2 of the Strategy Verification Protocol (data-integrity pre-check first; ≥20-OOS-trade rule enforced); persist; record a results doc à la `phase-1a-results.md` including the trial count of the run. Operator-run, ~10 minutes; refreshes `approved_set` on honest metrics. *(D timeframe then gets ≥5 OOS windows instead of 1.)*
- [ ] **Task 3.2 — Watcher server stage 1: bar-close scanning.** Long-running service triggering `signals.scan.run_scan` at each H1/H4/D bar close (not once daily). Spec: `watcher-server.md`.
- [ ] **Task 3.3 — Watcher stage 2: freshness + drift re-anchor.** TTL from Task 0.4 enforced service-side; reject/re-anchor when price has moved > X×ATR from `entry_ref`.
- [ ] **Task 3.4 — Watcher stage 3: demo autopilot.** On `ENV=demo` only, auto-run the full existing execute gate on fresh top-ranked candidates — this is how the INV-07 track record accrues at the trade frequency the backtest assumed. Live remains operator-only (INV-01 unchanged).
- [ ] **Task 3.5 — Calendar wiring.** `fathom calendar refresh` command + scheduled refresh in the watcher, making the existing news gate non-inert; live spread check replacing the `spread_ok` stub.
- [ ] **Task 3.6 — Exit management.** Real `ExecutionResponder` (trailing stop via OANDA's server-side order, breakeven at +1R, time stop at N bars) — backtest engine support first, since exits change the validated edge: every exit variant re-runs the full Verification Protocol ladder against real data (an exit rule is a strategy change, not a tweak), and the real-data regression pin (Task V.1) is extended with one exit-managed config.
- [ ] **Task 3.7 — New sleeves through the standard gate:** vol-scaled multi-lookback time-series momentum (candles only), then cross-sectional carry+momentum (needs v20 financing rates — the carry sleeve's backtest must use *recorded real* financing rates archived from the API, never assumed constants), then the session-bias overlay. Every sleeve climbs the full Verification Protocol ladder (rungs 1–7); none reaches `approved_set` on walk-forward alone once Phase B lands.

---

## Workstream 4 — AI quant research loop (Phases A–E)

Phase A's bug-fix half **is Workstream 0** — don't duplicate it. Each lettered phase runs as its own carved phase through the build method (specs → approved task graph → implementation). The design rationale in brief: LLM agents iterating on a backtester amplify multiple-testing bias (independent 2026 research found autonomous discovery loops reliably produce "profitable" strategies of which none survive Deflated-Sharpe correction), and LLMs carry memorized market history ("parametric look-ahead bias" — measured backtest inflation up to ~60–78% on pre-training-cutoff data, and ticker/date anonymization demonstrably fails to fix it). Hence the three load-bearing mechanisms below: an append-only trial ledger (honest trial count N), a deflation-gated promotion path, and a constrained proposal grammar with post-cutoff-only clean evidence. Key formula references for Phase B: Bailey &amp; López de Prado, "The Deflated Sharpe Ratio" (J. Portfolio Mgmt 2014) and "The Probability of Backtest Overfitting" (J. Computational Finance 2015); the Python package `pypbo` implements both and serves as the validation reference.

- [ ] **Phase A (remainder) — trial ledger + `run_trial()` API.** Append-only `trial_log` table (spec hash, params, instrument, timeframe, window dates, full OOS metrics incl. skew/kurtosis/T, engine+data+config hashes, proposer identity + model cutoff, pre-registered hypothesis, timestamp), populated inside the single backtest entry point — completeness by construction (new **INV-17**). Promote `cli._run_combo` into `backtest/trials.py::run_trial()` returning full window metrics + a hash-frozen run manifest.
- [ ] **Phase B — statistics gate.** `backtest/deflation.py`: Probabilistic Sharpe, **Deflated Sharpe Ratio** (expected-max-SR null over honest N with skew/kurtosis correction; validate against `pypbo` *and* against the real-data fixture — compute DSR by hand for the pinned fixture run and assert the implementation matches), **PBO/CSCV**, **MinTRL** (quantifies INV-07). This phase implements rungs 3–6 of the Strategy Verification Protocol as code (`fathom verify <combo>` or equivalent), so the ladder becomes a command instead of a checklist. Promotion to `approved_set` requires the full ladder (new **INV-18**). Pre-registered hypotheses face N=1 deflation. Evaluation config (splits, costs, universe) is frozen and unreadable/unwritable by any agent surface (new **INV-19**).
- [ ] **Phase C — Fathom Research MCP server.** Tools: `propose_trial` (validated **StrategySpec DSL v1** — compositions over the six existing families + causal filters; free-form code inexpressible), `run_backtest`, `get_result`, `list_trials`, `get_stats` (live DSR at session N). Hard session budgets (max trials/sweep/concurrency — refuse, don't queue).
- [ ] **Phase D — research agent + contamination controls.** Loop: pre-register → propose → backtest → diagnose → refine under budget, on the Workstream-1 adapter (any model). Clean-evidence rule: only OOS windows after the proposing model's training cutoff count toward promotion. Date-blind prompting; periodic four-arm date-sensitivity diagnostic.
- [ ] **Phase E — tiered autonomy + evaluating the AI itself.** Autonomy ladder (Observe → Advise → Act-with-approval → bounded-autonomous demo) with statistical gates at each promotion; shadow-mode/champion-challenger evaluation of the pretrade veto (counterfactual logging; the LLM gate must show trial-corrected incremental value before gaining authority); replay tests on prompt/model changes; output-variance monitoring.

**Honesty constraint carried through every phase:** "foolproof" is unattainable — search over finite history guarantees false positives. The bar is: statistically honest (nothing reported above its trial-corrected confidence), fail-safe by construction (risk envelope is architectural), auditable and reversible.

---

## Consolidated sequencing

| # | Work | Depends on | Size | Why this order |
|---|------|-----------|------|----------------|
| 1 | Workstream 0 (Tasks 0.1–0.9) + Task V.1 fixture | — | M | Real-money bugs + honest metrics + the real-data regression pin; everything inherits these |
| 2 | Workstream 1 (Tasks 1.1–1.4) | — (parallel with 0) | S | RED tests already done; unblocks any-model usage everywhere |
| 3 | Task 3.1 full-universe run | 0.3 | XS | Free breadth on honest metrics |
| 4 | Phase A remainder (ledger + `run_trial`) | 0.3 | S–M | Research plane foundation |
| 5 | Phase B statistics gate + INV-17/18/19 | 4 | M | Makes even human research honest |
| 6 | Watcher stages 1–3 (Tasks 3.2–3.4) | 0.4 | M | Capture fix + INV-07 track record accrual |
| 7 | Phase C MCP tool surface + DSL | 4, 5 | M | Agents can research safely |
| 8 | Phase D research agent | 2, 7 | M | The AI loop itself |
| 9 | Tasks 3.5–3.7 (calendar, exits, sleeves) | 6 | M–L | New edge through the full gate |
| 10 | Phase E autonomy + shadow eval · Task 2.1 MCP read-only server | 6, 8 | M | Authority is earned, not assumed |

Operator gates T-08/T-11/T-06 (#59/#86/#109) become runnable once rows 1–2 land (they are blocked by Workstream 0 doc/code fixes today); T-05 live cutover stays INV-07+MinTRL-gated at the very end.

## Method integration

Run each workstream through the project's documented build method (the half-cycle layers, which any tool can follow manually): author feature specs → compile a dependency-ordered task graph and get human approval → implement tasks in isolated branches/worktrees with a fresh reviewer per PR and squash merges → an adversarial acceptance walk on the deployed result → end-of-phase docs/context sync. Claude Code users can drive each step with the corresponding `halfcycle:*` command; other tools follow the same steps by hand. Workstream 0 is small enough to skip spec-writing and run directly off this plan's task list.

## Out of scope (evidence-based, deliberate)

TradingView data/MCP/scrapers in any automated path; LLM-decided entries, exits, sizing, or numeric forecasts; multi-agent debate as a signal source; free-form agent-authored Python in DSL v1; HFT anything; raising risk caps before an INV-07+MinTRL-satisfying track record; treating pre-cutoff OOS windows as clean evidence for agent-proposed strategies.
