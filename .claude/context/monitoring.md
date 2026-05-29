# Monitoring context

## P3-T-08 — deviation-monitor — 2026-05-29 (feat/p3-T-08-monitor)

**What was done:**

Created `monitoring/` package from scratch:
- `monitoring/__init__.py` — package init; exports `DeviationEvent`, `Watcher`, `WatcherConfig`.
- `monitoring/watcher.py` — the always-on watcher: `DeviationEvent` model, `Alerter` Protocol,
  `ExecutionResponder` Protocol, `WatcherConfig`, `PositionSnapshot`, four pure rule predicates
  (`check_adverse`, `check_slippage`, `check_vol`, `check_feed_health`), debounce machinery,
  and `Watcher` with `run()` loop (queue-based + iterable modes).
- `scripts/run_monitor.py` — the always-on entrypoint (stream → queue bridge → watcher).
- `tests/test_deviation_monitor.py` — 38 new tests covering all 4 rules + debounce + UTC +
  INV-01 + feed-health resilience.

**DeviationEvent shape (frozen, producer):**
```
event_id: str          # sha256(trade_id|rule|debounce_window)[:32] — idempotent
instrument: str
deviation_type: Literal["adverse","slippage","vol","feed_health"]
detail: str            # human-readable figure
broker_trade_id: str | None   # None for feed_health
severity: Literal["info","warn","severe"]
created_at: AwareDatetime     # UTC-aware (INV-03)
```
T-09 `monitor-alerts` consumes this exact shape.

**Alerter interface for T-09:**
```python
class Alerter(Protocol):
    def send(self, event: DeviationEvent) -> None: ...
```
Inject any object satisfying this protocol. `NoOpAlerter` is the default stub.

**ExecutionResponder interface (auto-response, default-off, INV-01):**
```python
class ExecutionResponder(Protocol):
    def respond(
        self,
        action: Literal["flatten", "tighten_stop"],
        broker_trade_id: str,
        instrument: str,
    ) -> None: ...
```
`NoOpExecutionResponder` is the default stub. Watcher never calls v20 directly.

**Four deviation rules (pure predicates):**
1. `check_adverse(position, current_price, config)` — per-tick; fires warn when past
   `adverse_fraction` of stop_dist; severe when past the full stop distance.
2. `check_slippage(position, config)` — per-tick; fires on the recorded fill slippage;
   warn when above threshold, severe when ≥ 3x threshold.
3. `check_vol(position, recent_prices, config)` — per-tick after enough history;
   range of last `vol_lookback` ticks vs `vol_atr_multiplier × stop_dist`.
4. `check_feed_health(instrument, last_tick_time, config)` — wall-clock elapsed since
   last tick vs `heartbeat_timeout_seconds`; also triggered by `gap_detected=True` on
   a tick (stream reconnected).

**Debounce:**
Per `(broker_trade_id, deviation_type)` key; suppresses repeats within the same
`event_id` (same sha256 window). The window is `floor(epoch / debounce_seconds)`.

**WatcherConfig defaults (safe for demo):**
- `adverse_fraction=0.5`, `slippage_threshold=0.0002`, `vol_atr_multiplier=2.0`
- `vol_lookback=20`, `heartbeat_timeout_seconds=15.0`, `reconcile_interval_seconds=60.0`
- `debounce_seconds=300.0`, `severe_response="alert_only"` (INV-01)

**INV-01 enforcement:** `severe_response` defaults to `"alert_only"`. Auto-response
(`auto_flatten` / `tighten_stop`) is behind a config flag and delegated to the injected
`ExecutionResponder` — never v20 inline. Watcher has no `submit_order` method.

**Key patterns / gotchas:**
- `Watcher` accepts either `tick_iterable` (for tests/replay) or `tick_source` (a queue)
  — both set, queue wins; neither, exits immediately.
- `store_loader` is `Callable[[], list[PositionSnapshot]]` — injected lambda wraps
  `store.load_open_positions()` in production; lambdas in tests.
- `PositionSnapshot` is a lightweight pydantic model for the watcher — does not import
  `execution.models.Position` to avoid import cycles; production `store_loader` maps
  `Position` → `PositionSnapshot`.
- `pyproject.toml` `[tool.setuptools.packages.find]` now includes `monitoring*`,
  `execution*`, `risk*` (was missing these packages from the find include list).

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/test_deviation_monitor.py -v` → **38 passed**, exit 0
- `python -m pytest -q` (full suite) → **898 passed, 87 warnings**, exit 0
- `python -m mypy .` → **"Success: no issues found in 76 source files"**, exit 0

**New dependency added to pyproject.toml?** NO — `monitoring/` uses only stdlib + pydantic
+ pandas (already in deps). `pyproject.toml` `packages.find.include` extended to add
`monitoring*`, `execution*`, `risk*` (build config only, no new dep).

**CLAUDE.md trigger-table check:**
- New package added → `pyproject.toml` packages.find.include extended. CLAUDE.md Stack:
  no new library dep, no update needed.
- No new CLI command.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).
