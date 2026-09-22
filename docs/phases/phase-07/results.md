# phase-07 — Results

**Closed:** 2026-09-22  
**Verdict:** ✅ **completed** — standalone CLI platform shipped; Hermes/Discord watchlist path retired; Layer-2 architecture redrawn.

---

## What shipped (T1–T5)

| Task | PR | Outcome |
|---|---|---|
| T1 pine-generation | #166 | `fathom pine` — watchlist → Pine v6 (stdout + clipboard) |
| T2 hermes-teardown | #167 | chart/PNG + daily job + Discord watchlist contract removed; T-08 superseded |
| T3 ai-package-migration | #168 | `hermes_integration/` → `ai/`; in-process news-risk + narration on `OpenAICompatClient` |
| T4 market-brief | #165 | session brief / regime tags / session verdict (advisory fallbacks) |
| T5 analyze-command | #169 | `fathom analyze` — scan → brief → news-risk → narration → Pine; `analysis_log` |

Stack-assembly (2026-09-22): **green** — `fathom` help lists `pine`/`analyze`; `chart` rejected; analyze offline path exit 0 with no LLM; execute dry-run import boundary intact; editable reinstall required once after the `ai/` rename.

## Done-when checklist

- [x] Architecture redraw — [`docs/product/architecture.md`](../../product/architecture.md): Hermes/Discord-watchlist nodes removed; `ai/` + Pine + LLM provider + on-demand analyze flows present; ADRs 001/003/004 marked implemented.
- [x] Hermes residue — active operator docs / product layer describe removal or live surfaces only; historical phase `name` strings and explicit superseded notes retained (sanctioned). `fathom chart` gone from CLI.
- [x] `fathom execute` gate survived the rename (stack-assembly + suite; dry-run path operator-confirmed against a live watchlist candidate after backtest/scan).
- [x] Offline analyze path — no `LLM_API_KEY` → INV-02 skip / advisory fallbacks, no crash (stack-assembly + tests).
- [ ] Live `fathom analyze` + TradingView paste (≥3 candidates / ≥2 instruments) — **operator concurrent** after `fathom backtest --instruments ALL --history-years 5` → `scan` (demo-store prereq). Record paste notes here when done.

## Operator follow-through (demo store)

```bash
fathom backtest --instruments ALL --history-years 5   # gap-aware; refreshes approved_set
fathom scan
fathom analyze                                         # live LLM_* key
fathom pine                                            # paste into TradingView
fathom execute "<instrument>:<tf>:<strategy>" --dry-run
```

Candle cache at close of engineering: ~2.4M rows / 68 instruments; `approved_set` empty until the in-flight 5y backtest completes.

## Unlocks

- [`phase-08`](../phase-08/phase.md) companion commands (`review` / `journal` / `ask`) — may start.
- [`phase-09`](../phase-09/phase.md) veto ledger retrofit on the analyze news-risk insertion point.

## Status

Phase-07 acceptance: **PASSED** on engineering + architecture + stack-assembly. Pine/analyze live walk is the residual operator surface (same class as phase-02's transferred Discord gate) and does not block phase-08 kickoff.
