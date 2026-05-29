# Signals context

New area (`signals/` package) — the Phase 2 watchlist pipeline. Owns the
INV-13 `Candidate` wire contract and the INV-10 gate.

## P2-T-01 — 2026-05-29 (feat/p2-t-01)

**What was done:**
- Created `signals/` package (`__init__.py` re-exports `Candidate`, `Ranker`).
- `signals/ranker.py`:
  - **`Candidate`** — flat pydantic v2 model, the FROZEN INV-13 wire contract.
    Fields, in this exact order: `instrument, timeframe, strategy_name,
    direction, entry_ref, stop_distance, target_distance, oos_sharpe_mean,
    quality_score, rank, spread_ok, session_ok, news_flag, generated_at`.
    `direction` is `str` ("LONG"/"SHORT"), `generated_at` is a UTC RFC-3339
    `...Z` `str`. No nested `signal` object — relevant `Signal` fields are
    flattened. Downstream consumers (T-02 portfolio, T-03 charts, T-05
    narration, T-07 cli, Hermes job) build against this — do not rename/retype
    without an INV-13 amendment.
  - **`Ranker(store, calendar, *, strategy_builder=, spread_ok=, session_ok=,
    eval_lookback_bars=)`** with `rank(now: datetime) -> list[Candidate]`.
    Pipeline as 6 pure stages: gate → evaluate → filter (spread/session) →
    news → conflict → rank.
  - **INV-10 gate:** `_gate()` calls `store.load_approved_set()`; empty → return
    `[]` and log "Approved-set is empty …".
  - **Gate join (DRIFT-01):** the approved row keys the dimension as
    `row['granularity']`; `Signal` calls it `timeframe`. SAME dimension. Match is
    `signal.instrument==row['instrument'] AND
    signal.strategy_name==row['strategy_name'] AND
    signal.timeframe==row['granularity']`. Candidate surfaces it as `timeframe`.
  - **Evaluate:** loads recent candles (`load_candles`, generous day-span
    lookback), builds the strategy via `strategy_builder`, runs
    `generate_signals`, takes the most-recent bar's `Signal` (max by
    `generated_at`). FLAT signals → no candidate.
  - **News gate:** for either leg-currency, high-impact within `NEWS_WINDOW_HIGH`
    (4h) → drop; medium within `NEWS_WINDOW_MEDIUM` (1h) → `news_flag=True`;
    else False. Uses `calendar.upcoming_events(currencies, window)` +
    `data.calendar.Impact`.
  - **Conflict (D-P2-1):** group by `(instrument, timeframe)`; if both LONG and
    SHORT present in a group, suppress ALL members. Cross-timeframe independent.
  - **Rank:** sort by `oos_sharpe_mean` desc, then `quality_score` desc, then
    `(instrument, strategy_name)` asc as a stable final tie-break. 1-based `rank`.
  - **INV-01:** no `execution`/`risk`/`orders` import anywhere — candidates only.
  - **INV-03:** `rank(now)` rejects naive `now`; `generated_at` formatted UTC `Z`.
- `tests/test_ranker.py` — 23 tests, all mocked (NO live HTTP): empty-set→[];
  naive-now reject; only-approved-emit; gate-join-uses-granularity; FLAT→none;
  empty-candles skip; news high-drop/medium-flag/low-none; spread+session fail
  drop; conflict suppress-both / same-dir keep / cross-tf independent; rank
  primary+tiebreak+stable-final; and the **INV-13 serialisation round-trip**
  (field names + order + types + flat-shape + JSON round-trip).

**Key patterns / gotchas:**
- **Spread/session are INJECTED hooks** (`spread_ok`, `session_ok`), defaulting
  to permissive (`True`) because no live spread feed / session schedule is wired
  in this task. The spec leans on `InstrumentMeta.typical_spread × k`; a later
  task can inject the real check without touching the pipeline. Default does NOT
  fabricate a filter result.
- **`strategy_builder` is injected** (`(strategy_name, instrument, timeframe) ->
  Strategy`). Default `_default_builder` mirrors the runner's registry, routing
  on the `strategy_name` prefix (`macrossover`/`donchian`/`bollinger`/`rsi`/
  `roc`/`session`) and building with documented default params. The approved-set
  stores the strategy's full `name` (param-encoded), so the exact instance can't
  be rebuilt from the registry key alone — the prefix-routed default is the
  pragmatic reuse; T-07 may pass a richer builder if it wants exact params.
- `Candidate.model_fields.keys()` order IS the contract — the round-trip test
  asserts the exact list. Adding/reordering a field is a visible test failure.
- `data.calendar.Impact` is imported lazily inside `_news_gate` so the module
  stays importable in mock-only tests; the `Impact` enum identity check
  (`is Impact.high`) is what the news gate keys on.

**Packaging note (BLOCKER, flagged to lead):**
- `signals/` is NOT in `pyproject.toml`'s `[tool.setuptools.packages.find]
  include` list. I attempted to add `"signals*"` but the edit was DENIED by the
  task constraint "do NOT touch pyproject". Empirically `import signals` works
  under `pip install -e '.[dev]'` + pytest/mypy from the project root (cwd
  resolution + the editable finder pick it up), so tests + mypy pass. BUT a
  consumer importing `signals` from outside the repo root (e.g. an installed
  wheel, or a different cwd) would NOT find it. **The coordinator should add
  `"signals*"` to the include list** (same serialized-edit pattern as the
  matplotlib dep) before/with T-07 wires `signals` into `cli.py`. This is a
  packaging declaration, not a dependency.

**AC verification results:**
- `python -m pytest tests/test_ranker.py -q` → **23 passed**, exit 0
- `python -m pytest -q` (full suite) → **446 passed, 85 warnings**, exit 0
  (warnings pre-existing, from backtest/metrics tests — not new)
- `python -m mypy signals/` → **"Success: no issues found in 2 source files"**, exit 0

**No new runtime dependencies** (pydantic + pandas + stdlib, all already in
pyproject). `pyproject.toml` NOT modified (constraint + edit denied — see
packaging note above).

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after
reviewer pass). Hold the Phase 2 fan-out (T-02/03/05/07) until this PR's INV-13
round-trip test is green on main.
