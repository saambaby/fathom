# Context: panel area

## P4-T-05 — admin-panel (Streamlit dashboard) — 2026-05-29

**Branch:** `feat/p4-T-05-panel` | **PR:** https://github.com/saambaby/fathom/pull/114

### What was built
- `panel/app.py` — Streamlit dashboard; 5 views over `panel.data` view models:
  1. **Charts** — TradingView Lightweight Charts™ (`renderLightweightCharts`) with candlestick series, horizontal line overlays for active/proposed entry/stop/target, Apache-2.0 attribution watermark (D-P4-3).
  2. **Equity** — equity curve + drawdown line chart from `panel.data.equity_series`.
  3. **Blotter** — open positions + unrealised P&L + `day_pl` + risk-in-use vs `risk_budget`.
  4. **Watchlist** — `Candidate[]` (INV-13 shape unchanged).
  5. **Deviation Log** — newest-first from `panel.data.deviation_log`.
- Sidebar: refresh button → `signals.scan.run_scan(db_path=..., dry_run=True)` (order-free).
- `st.cache_data(ttl=30)` on all store reads (library_default: explicit TTL, never forever).
- `streamlit.runtime.exists()` guard: module-level view dispatch skipped in bare-import mode.
- `pyproject.toml` — added `"panel*"` to `[tool.setuptools.packages.find].include`.
- `tests/test_admin_panel.py` — 8 tests: INV-01 AST probe, individual boundary checks, clean-subprocess import, AppTest smoke.

### INV-01 boundary approach
- **AST probe** (not sys.modules walk): walks `panel/app.py` + `panel/__init__.py` source AST; asserts no forbidden imports/names (`execution.orders`, `risk.sizing`, `cli`, `build_bracket`, `submit_order`).
- **Reason**: `risk/__init__.py` re-exports `risk.sizing` so importing the permitted `risk.limits` (via `panel.data`) always loads `risk.sizing` as a package side effect — sys.modules walk would false-positive.
- **Clean subprocess check** `cd /tmp && python -c "import panel.app, sys; print('LEAK' if 'execution.orders' in sys.modules else 'clean')"` → prints `clean`.

### Gotchas
- `st.cache_data` decorator cannot have `# type: ignore[misc]` on it in this mypy version — causes `unused-ignore` error; decorators are untyped and mypy accepts them without the comment.
- The `delta_color` literal typing in `st.metric` requires only valid Literal values; use default behaviour (omit `delta_color`) for dynamic P&L colouring to avoid mypy errors.
- Module-level execution in Streamlit apps runs on every script re-run; guard view dispatch with `streamlit.runtime.exists()` so `import panel.app` in a bare Python process doesn't crash (Streamlit warns but does not crash on `st.set_page_config` / `st.radio` etc.; it does crash on `st.spinner` without a session context).
- The editable install must be refreshed (`pip install -e .`) after adding `panel*` to `pyproject.toml` — the finder MAPPING in `__editable___fathom_0_1_0_finder.py` is stale otherwise.

### AC check results (exit codes)
- `mypy .` → 0 errors (87 source files), exit 0
- `pytest -q` → 1040 passed, exit 0
- INV-01 subprocess check → `clean`, exit 0

### Merge plan
`gh pr merge 114 --squash --delete-branch`
