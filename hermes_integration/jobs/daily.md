# Fathom Daily Watchlist Job

**Job type:** Hermes scheduled job (plain-English definition)
**Phase:** Phase 2 — Watchlist → Discord
**Last updated:** 2026-05-29
**Invariants:** INV-01, INV-02, INV-08, INV-10

---

## Purpose

Deliver a ranked, Claude-enriched watchlist to the trader's Discord channel on
each weekday after the major session close. Hermes runs the Fathom CLI to
produce candidates, applies a qualitative Claude news-risk veto per candidate,
renders a chart PNG per survivor, generates a one-line Claude narration per
chart, and posts the full bundle to Discord.

**The job ends at Discord delivery. Hermes never places orders (INV-01).**

---

## Trigger

- **Schedule:** Monday–Friday (weekdays only; skip weekend days).
- **Time:** Post-New-York-session close — default `22:00` in the operator's
  local timezone (recommended: configure as UTC, e.g. `22:00 UTC`). Adjust to
  post-London or post-Tokyo as preferred.
- **Hermes cron expression (example, UTC):**
  ```
  0 22 * * 1-5
  ```
  Change `22` to the hour that aligns with your preferred session close.

---

## Allowed Fathom CLI tools (INV-01)

Hermes is granted access to **exactly three** Fathom CLI subcommands:

| Tool | Purpose |
|------|---------|
| `fathom scan` | Refresh candles, rank approved strategies, apply portfolio limits, emit `Candidate[]` JSON on stdout. |
| `fathom watchlist` | Re-read the latest persisted watchlist (for manual re-runs or re-delivery). |
| `fathom chart <instrument>` | Render a chart PNG for one instrument; print the PNG path to stdout. |

**Hermes must NOT be given access to any order, execution, or risk tool.**
`execution/orders.py` and any order-placement API are explicitly off-limits.
Permitted tool references: `scan`, `watchlist`, `chart` — no others.

This boundary is the canonical INV-01 enforcement point. The worst a
prompt-injected headline can do is produce a bad *suggestion* that gets
vetoed or down-ranked; it can never produce a bad *trade* because Hermes
holds no order authority.

---

## Ordered job steps

### Step 1 — Run `fathom scan`

```
fathom scan [--db-path <path>] [--timeframes H1,H4,D]
```

- Reads the approved-set from the SQLite store (the INV-10 gate).
- Refreshes candles from OANDA (unless `--dry-run`).
- Runs `Ranker` → `PortfolioLimiter`.
- **Persists the ranked `Candidate[]` to the watchlist table.**
- Emits `Candidate[]` JSON on **stdout** (INV-13 shape).

**Use `fathom scan`'s stdout directly — do NOT call `fathom watchlist` as the
primary source.** (`fathom watchlist` is the persisted-read accessor for
re-reads and the Phase 5 panel; the daily job consumes the fresh stdout JSON.)

> **Empty watchlist (INV-10):** if `fathom scan` emits the empty-watchlist
> message (no JSON array or an empty `[]`), skip all remaining per-candidate
> steps and go directly to Step 5 — post "no candidates today" to Discord.
> Exit 0. This is a valid result, not an error.

The `Candidate[]` JSON already embeds the deterministic news gate:
- High-impact calendar events within the trade window → candidate already
  **dropped** before this step.
- Medium-impact events within the trade window → `news_flag: true` on the
  candidate (still present in stdout).

---

### Step 2 — Per-candidate news-risk assessment (Claude, per-candidate loop)

For **each** candidate in the stdout JSON, call Claude with the
`hermes_integration/prompts/news_risk.md` prompt template, substituting:

| Placeholder | Source |
|-------------|--------|
| `{{instrument}}` | `candidate.instrument` |
| `{{base_currency}}` | first 3 chars of `candidate.instrument` |
| `{{quote_currency}}` | last 3 chars of `candidate.instrument` |
| `{{direction}}` | `candidate.direction` |
| `{{entry_window_utc}}` | estimated entry window (UTC, RFC 3339) |
| `{{calendar_events}}` | upcoming events from the economic calendar |

Parse Claude's response with `parse_news_risk(raw: str) -> NewsRiskVerdict`
from `hermes_integration/news_risk.py` (INV-02 enforcement boundary):

| `suggest_action` | Action |
|------------------|--------|
| `"skip"` | **Veto the candidate.** Remove it from the list; do not render chart or narration. |
| `"reduce_size"` | **Flag the candidate.** Keep it; mark it with a `reduce_size` flag for the Discord post. |
| `"proceed"` | **Keep the candidate unchanged.** |

> **Malformed/unavailable Claude response (INV-02):** `parse_news_risk`
> defaults to `suggest_action="skip"` on any parse, validation, or network
> failure. The candidate is vetoed. This is the safe default — a missed
> opportunity is cheaper than an unchecked trade. The caller must never catch
> this and substitute "proceed".

After Step 2, the candidate list contains only `proceed` and `reduce_size`
survivors.

---

### Step 3 — Chart generation (per surviving candidate)

For **each** surviving candidate (not vetoed in Step 2), call:

```
fathom chart <instrument> [--timeframe <tf>] [--db-path <path>]
```

where `<instrument>` is `candidate.instrument` and `<tf>` is
`candidate.timeframe`.

- Returns the PNG file path on stdout.
- The PNG shows entry/stop/target levels and the signal bar.

> **Chart failure:** if `fathom chart` exits non-zero or produces no path,
> skip the chart for that candidate (log the failure). The candidate remains
> on the watchlist — chart failure is non-fatal. Proceed to narration for
> that candidate without a chart attachment.

---

### Step 4 — Narration (Claude, per surviving candidate)

For **each** surviving candidate, call Claude with the
`hermes_integration/prompts/narration.md` prompt template, substituting:

| Placeholder | Source |
|-------------|--------|
| `{{instrument}}` | `candidate.instrument` |
| `{{timeframe}}` | `candidate.timeframe` |
| `{{strategy_name}}` | `candidate.strategy_name` |
| `{{direction}}` | `candidate.direction` |
| `{{oos_sharpe_mean}}` | `candidate.oos_sharpe_mean` |
| `{{news_flag}}` | `candidate.news_flag` |

Claude returns exactly one plain-English line (no JSON, no markdown).

**Usability check (not INV-02):** use `should_use_fallback(claude_response)`
from `hermes_integration/narration.py`:

- If `True` (empty, whitespace-only, or over 280 chars) → substitute
  `fallback_narration(candidate)` from `hermes_integration/narration.py`.
- If `False` → use Claude's response directly.

> **Narration failure is cosmetic only — the candidate is always kept.**
> Unlike news-risk (INV-02), a narration hiccup does NOT veto the candidate.
> The fallback is a deterministic one-liner built from the candidate's own
> fields; it is always non-empty and never raises.

---

### Step 5 — Deliver to Discord via Hermes gateway

Assemble the message and post to the configured Discord channel:

**Non-empty watchlist format (per candidate, ranked by `rank` field):**

```
[rank]. <instrument> <timeframe> | <strategy_name> | <direction>
  <narration_line>
  [NEWS FLAG: reduce_size] (only if reduce_size from Step 2)
  [Attachment: chart PNG] (if chart rendered successfully in Step 3)
```

**Empty watchlist:**

```
Fathom daily scan — no candidates today. (YYYY-MM-DD UTC)
```

Post this message even if the scan produced zero candidates (INV-10).

> **Discord delivery failure:** retry per Hermes' gateway retry policy. The
> watchlist is persisted by `fathom scan` in the SQLite store regardless; it
> can be re-read via `fathom watchlist` for a manual re-delivery. Delivery
> failure does not constitute a job failure from Fathom's perspective.

---

## Failure modes and safe defaults

| Failure | Behaviour |
|---------|-----------|
| `fathom scan` empty watchlist | Post "no candidates today" to Discord; exit 0. |
| `fathom scan` exits non-zero | Abort job; post error notice to Discord (optional); log. |
| Claude news-risk malformed/unavailable | `parse_news_risk` returns `skip`; candidate vetoed (INV-02). |
| Claude narration malformed/unavailable | Use `fallback_narration`; candidate kept (NOT INV-02). |
| `fathom chart` non-zero exit | Skip chart for that candidate; candidate kept; log. |
| Discord delivery failure | Retry per Hermes gateway policy; watchlist persisted for re-read. |
| Prompt injection in calendar headline | Can produce at most a bad suggestion that gets vetoed/down-ranked; can never produce a trade (INV-01). |

---

## Operator runbook

### Prerequisites

Before registering this job, ensure the following are available:

1. **Hermes Agent** — a running Hermes instance with cron scheduling enabled.
2. **Fathom CLI** — installed in a virtualenv accessible to Hermes:
   ```
   pip install -e /path/to/fathom
   ```
   Verify: `fathom --help` lists `scan`, `watchlist`, `chart`, `backtest`.
3. **Fathom data store populated** — run at least one `fathom backtest` and
   `fathom scan` (with live OANDA credentials) to seed the approved-set and
   candle cache before the first Hermes run.
4. **Discord webhook or bot token** — a Discord channel where the watchlist
   will be posted.
5. **Anthropic API key** — for Hermes' Claude calls (news-risk + narration).

### Credentials and secrets (INV-08)

All secrets live in Hermes' `.env` file (never committed to git):

```
# Hermes .env — never commit this file
ANTHROPIC_API_KEY=sk-ant-...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
# (or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID for bot approach)

# Fathom-side (if Hermes calls fathom scan live — not --dry-run)
OANDA_API_KEY=...
OANDA_ACCOUNT_ID=...
OANDA_ENV=practice
```

Hermes passes these to its tool calls via environment injection — never as
CLI arguments and never logged.

### Registering the job in Hermes

1. Copy this file (`hermes_integration/jobs/daily.md`) into your Hermes jobs
   directory (or reference it directly, per your Hermes deployment).

2. Register the `fathom` CLI as a Hermes tool with the following tool
   definition. **Grant access to `scan`, `watchlist`, and `chart` only:**

   ```yaml
   tools:
     fathom_scan:
       command: fathom scan
       description: "Refresh candles, rank approved strategies, emit Candidate[] JSON."
       allowed_args: ["--instruments", "--timeframes", "--db-path", "--history-years", "--dry-run"]

     fathom_watchlist:
       command: fathom watchlist
       description: "Re-read the latest persisted watchlist as Candidate[] JSON."
       allowed_args: ["--db-path"]

     fathom_chart:
       command: fathom chart
       description: "Render a chart PNG for one instrument; print its path to stdout."
       allowed_args: ["instrument", "--timeframe", "--db-path", "--out-dir", "--history-years"]
   ```

   **Do NOT register `fathom backtest` as a Hermes tool** (it's a one-off
   operator command, not part of the daily pipeline). **Never register any
   order, execute, or risk tool** — `execution/orders.py` and any
   order-placement or risk-sizing API must not be registered as Hermes tools
   (INV-01). Do not grant Hermes execute or order tool access.

3. Register the Claude prompt paths in Hermes' tool config:
   - News-risk prompt: `hermes_integration/prompts/news_risk.md`
   - Narration prompt: `hermes_integration/prompts/narration.md`

4. Register the Discord delivery gateway with the `DISCORD_WEBHOOK_URL` (or
   bot token + channel ID) from `.env`.

5. Register the cron schedule. Example (22:00 UTC, weekdays):
   ```
   0 22 * * 1-5
   ```

### Wiring the response parsers

Hermes calls Claude and receives the raw JSON/text response. It must pass
those responses through Fathom's parsers before acting on them:

- **News-risk:** call `parse_news_risk(raw)` from
  `hermes_integration/news_risk.py`. Never bypass this parser or treat a raw
  Claude string as a trusted verdict.
- **Narration:** call `should_use_fallback(claude_response)` from
  `hermes_integration/narration.py`. If True, call `fallback_narration(candidate)`.

Both modules are importable from the installed `fathom` package; Hermes can
call them as Python library calls or via a small wrapper script. No `anthropic`
SDK import is needed in Fathom (D-P2-3).

### Verifying the setup (dry-run)

Before the first live run, perform a dry-run to confirm the tool chain works:

```bash
# 1. Confirm fathom scan (dry-run, against cached data) works and emits JSON.
fathom scan --dry-run

# 2. Confirm fathom watchlist re-reads the persisted output.
fathom watchlist

# 3. Confirm fathom chart works for one instrument in the watchlist.
fathom chart EUR_USD --timeframe H1
```

Check that step 1 emits valid `Candidate[]` JSON (or the empty-watchlist
message), step 2 re-reads it, and step 3 emits a PNG path.

### Acceptance gate (T-08)

Live acceptance requires a human operator to:

1. Configure a real Hermes instance with the above setup.
2. Confirm the watchlist lands coherently in Discord on **≥5 consecutive
   weekday runs** (charts + Claude rationale + news flags present; empty days
   post "no candidates"; a `skip` verdict vetoes; no secret token in output).
3. Record the results in `docs/phases/phase-2-results.md`.

This is a manual, human-admin gate (D-P2-5) — it cannot be automated.
