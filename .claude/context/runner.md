# Runner context

## POC-T-07 — 2026-05-28 (feat/poc-t-07)

**What was done:**
- Created `scripts/__init__.py` (empty, so `scripts` is a discoverable package in editable install).
- Created `scripts/poc_run.py`:
  - CLI args: `--instruments`, `--granularities`, `--history-years`, `--fast-periods`,
    `--slow-periods`, `--dry-run`, `--db-path`.  Defaults match poc.md parameters.
  - `_UTCFormatter`: custom `logging.Formatter` that formats `formatTime` with
    `%Y-%m-%dT%H:%M:%SZ` via `datetime.fromtimestamp(..., tz=timezone.utc)` (INV-03).
  - Flow: Settings → OandaClient → Store → (optional) `fetch_and_cache` →
    `MACrossover` × `WalkForwardValidator` for all combos → `_print_approved_table`.
  - Per-instrument `CostParams`: JPY pairs use `pip_value=0.0001`... wait, corrected:
    JPY pairs use `pip_value=0.01`; non-JPY majors use `pip_value=0.0001`.
  - `--dry-run`: skips Settings / OandaClient construction; opens store at `--db-path`
    and runs walk-forward against whatever is cached. Does NOT make live HTTP calls.
    Integration test uses this flag — no credential required.
  - Empty approved set: prints `"No combinations passed walk-forward criteria."` to
    stdout and **exits 0** (not 1 — empty set is a valid PoC result, per T-07 AC).
  - Invalid (fast >= slow) combinations are silently skipped (no error, no log spam).
  - `_print_approved_table`: tabular stdout output with columns: Instrument, Gran,
    Fast, Slow, OOS Sharpe, Trade Count, Swap Modelled.
  - INV-08 enforced: Settings / OandaClient not constructed at all in `--dry-run`;
    only `env` and `oanda_base_url` are logged (not token or account ID).
- Created `tests/integration/test_poc_runner.py`:
  - `_populate_store`: builds 800 sinusoidal daily candles (≈2.2 years) for a real
    SQLite file in `tmp_path`. Verified `count > 700`.
  - `populated_db` / `empty_db` fixtures use `tmp_path` (pytest-managed temp dirs).
  - `_run_poc()` helper: runs `scripts/poc_run.py` as a subprocess via
    `subprocess.run` with `sys.executable`, `capture_output=True`, `cwd=project_root`.
    Always passes `--dry-run` so no HTTP is made.
  - `TestPocRunnerEmptyStore`: exit 0, "no combinations" message, UTC timestamps,
    no token/secret strings in output.
  - `TestPocRunnerWithData`: exit 0, table or empty message, `False` in stdout when
    table present (`swap_modelled=False`), UTC timestamps, no traceback.
  - `TestPocRunnerMultipleParams`: multi-combo run still exits 0.
- Fixed `tests/test_oanda_client.py` line 75: removed `# type: ignore[arg-type]`
  made redundant by `pydantic.mypy` plugin in strict mode.

**Key patterns / gotchas:**
- `--dry-run` in the runner avoids constructing `Settings` / `OandaClient` entirely —
  so the test needs no `.env` and no env vars at all.  If `Settings` construction fails
  (no `.env`), the runner exits 1 with an error log (non-dry-run path only).
- `subprocess.run` CWD must be the project root so `scripts/poc_run.py` can import
  `config`, `data`, `backtest`, `strategies` without a path hack.  The fixture uses
  `Path(__file__).parent.parent.parent` to locate the root.
- `pyproject.toml` already includes `"scripts*"` in `packages.find`; no change needed.
- The approved-set table includes a `Swap Modelled` column that shows `False` (D-03).
  The integration test asserts this is present when the table is printed.
- Empty approved set is a valid result — integration test confirms exit 0 in both
  empty-store and populated-store paths.
- All log timestamps use the `_UTCFormatter` which calls
  `datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`.
  The integration test validates with regex `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z`.

**AC verification results (raw, captured exit codes):**
- `python -m pytest tests/integration/test_poc_runner.py -q` → 15 passed, exit 0
- `python -m pytest -q` (full suite) → 145 passed, exit 0
- `python -m mypy scripts/ tests/integration/` → "Success: no issues found in 4 source files", exit 0
- `python -m mypy scripts/ config/ data/ strategies/ backtest/ tests/integration/` → "Success: no issues found in 18 source files", exit 0
- `python -m mypy .` (whole tree) → "Success: no issues found in 25 source files", exit 0
- `python scripts/poc_run.py --dry-run --db-path /tmp/poc_smoke_test.db` (empty DB):
  stdout: `"No combinations passed walk-forward criteria."`, exit 0, no traceback.

**Smoke test (--dry-run against empty DB):**
```
$ python scripts/poc_run.py --dry-run --db-path /tmp/poc_smoke_test.db
No combinations passed walk-forward criteria.
EXIT: 0
```

**No new dependencies** — no changes to `pyproject.toml`.

**CLAUDE.md:** `scripts/poc_run.py` was already documented in the Commands section
("python scripts/poc_run.py — end-to-end PoC..."). Confirmed match; no update needed.

**Merge plan:** `gh pr merge <N> --squash --delete-branch` (lead action after reviewer pass).
