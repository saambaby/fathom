# Fathom — Go-Live Runbook

**Status:** Active (Phase 5 capstone)
**Author:** saambaby
**Last updated:** 2026-05-30
**INV-07 gate:** BLOCKED — prerequisite track record has not yet been recorded.
The live cutover does NOT proceed until every box in Section 1 is ticked.

---

## CRITICAL ORDERING REQUIREMENT

The cutover sequence in Section 2 is a **hard prerequisite, not a suggestion**.
The ordering exists because the live gate auto-passes the attestation inside
`fathom execute` ONLY because `LIVE_TRADING_ENABLED=true` is the persisted
evidence that the attested preflight ceremony was completed first. **Never set
`LIVE_TRADING_ENABLED=true` before a passing `fathom preflight
--attest-track-record` run.** The flag IS the attestation record. Setting the
flag without a prior passing attested preflight defeats the entire defense-in-depth
design (D-P5-2) and bypasses the INV-07 gate.

Going live is **operator-only and deliberate**. No automated step performs the
cutover. The lead/agent never flips `LIVE_TRADING_ENABLED` or `ENV=live`. The
cutover is a manual, reviewed, single-operator action.

---

## Section 1 — Prerequisites (INV-07 Hard Gate)

The live cutover is **blocked** until every item below is ticked. These are not
suggestions; they are the INV-07 prerequisite (see `docs/invariants.md`).

### 1.1 Demo track record requirements

- [ ] **Phase 2 T-08 acceptance closed:** the live Discord alert/watchlist delivery
  is confirmed working and the operator has recorded the result.
- [ ] **Phase 3 T-11 acceptance closed:** the live demo-loop (real OANDA demo
  account, real-time execution, real-time monitoring) has run for a sustained
  period with a positive, stable edge recorded.
- [ ] **Phase 4 T-06 acceptance closed:** the operator panel acceptance is recorded
  and the monitoring/charting pipeline is confirmed reliable.

### 1.2 Plumbing reliability requirements

- [ ] Execution/monitoring plumbing has proven reliable on fake money over a
  sustained demo period (at least [operator-defined duration] of continuous demo
  trading with no unrecovered errors).
- [ ] `fathom reconcile` has been run regularly and always converges (broker-wins,
  INV-16).
- [ ] The deviation monitor (`scripts/run_monitor.py`) has been running during the
  demo period and Discord alerts are confirmed firing correctly.
- [ ] `fathom preflight` exits 0 (GO) on the current demo setup with all five
  mechanical checks passing.

### 1.3 Not yet met

**As of this writing, none of the above prerequisites are met.** Phase 3 T-11,
Phase 2 T-08, and Phase 4 T-06 are all still operator gates. Do not proceed past
this section until every box above is ticked with a dated, signed entry in
Section 6.

---

## Section 2 — Cutover Sequence (Operator-Only)

**Prerequisite:** all boxes in Section 1 must be ticked before starting this
sequence. Read the CRITICAL ORDERING REQUIREMENT at the top of this document
before proceeding.

This sequence is performed by a single operator in one deliberate session. Work
through the steps in strict order. Do not skip steps or reorder them.

### Step 1 — Set the live token and ENV

Edit `.env` (never commit it — INV-08):

```
OANDA_API_TOKEN=<your live token>
OANDA_ACCOUNT_ID=<your live account id>
ENV=live
```

Do **not** set `LIVE_TRADING_ENABLED=true` yet. The flag is set only after a
passing attested preflight (Step 3).

Verify `.env` is gitignored:

```bash
git status --short | grep -v "^?? .env"
# .env must not appear as a tracked or staged file
```

### Step 2 — Run fathom preflight — must be GO

```bash
fathom preflight --attest-track-record
```

This command:
- Checks account reachability (OANDA returns a valid account summary).
- Checks the kill switch is armed and not tripped (account state present, fresh,
  day P&L within cap).
- Checks the INV-04 bracket contract (build_bracket rejects a zero-stop candidate).
- Checks env/flag/token consistency (ENV=live, token present, account ID present).
- Records the operator's explicit attestation that the demo track record satisfies
  INV-07 (the `--attest-track-record` flag is this attestation).

**The output must show `GO` with all five checks passing. Any `NO-GO` blocks the
cutover — resolve the failing check and re-run until GO before proceeding.** The
`env_flag_token_consistency` check verifies that `live_trading_enabled` (the
`settings.live_trading_enabled` field, set via `LIVE_TRADING_ENABLED=true` in
`.env`) is consistent with `ENV=live`.

Record the GO output (timestamp + all check lines) in Section 6.

### Step 3 — ONLY after a GO: set LIVE_TRADING_ENABLED=true

Only after Step 2 produces a GO result, edit `.env`:

```
LIVE_TRADING_ENABLED=true
```

**Never set this flag before a passing attested preflight.** This flag is the
persisted record that the attested preflight ceremony was completed. The live gate
in `fathom execute` reads this flag as evidence that the ceremony happened; setting
the flag without that ceremony bypasses the INV-07 gate.

Do not also set `LIVE_RISK_FRACTION` above 0.001 (0.10%) at this stage. Section 3
covers the ramp schedule.

### Step 4 — Execute one small candidate (typed account-id confirmation)

Select a single small candidate from the current watchlist:

```bash
fathom execute <instrument> --timeframe <TF> --strategy <name>
```

When prompted, type the OANDA live account ID exactly to confirm. This is not
bypassable with `--yes` or any flag. The gate requires all four conditions:
`ENV=live` AND `LIVE_TRADING_ENABLED=true` AND a passing preflight AND the typed
confirmation. Any one missing causes an immediate refusal with a named reason.

The order is sized at `LIVE_RISK_FRACTION` (default 0.10% of equity). At this
stage, this is the intended small-size start.

### Step 5 — Confirm the bracketed fill, monitor, and reconcile

After the order is submitted:

1. **Confirm the bracketed fill.** The position must appear in `fathom positions`
   with both a stop-loss price and a take-profit price. A naked position (missing
   bracket) is a breach of INV-04.

   ```bash
   fathom positions
   ```

2. **Confirm `scripts/run_monitor.py` is running.** The deviation monitor must be
   active during any live session.

   ```bash
   python scripts/run_monitor.py --instruments <instrument>
   ```

3. **Confirm `fathom reconcile` matches broker truth.** Run reconcile and verify
   the local positions table matches the OANDA account (INV-16).

   ```bash
   fathom reconcile
   ```

   The output must show `adopted=0 closed=0 matched=N` (no broker-only or
   locally-only positions). Any drift is logged at WARNING; investigate before
   proceeding.

Record the fill confirmation, monitor start time, and reconcile output in Section 6.

---

## Section 3 — Small-Size Start and Manual Ramp

### Initial size

Begin at `LIVE_RISK_FRACTION=0.001` (0.10% of equity per trade). This is the
Phase 5 default. Do not increase it on the first day.

The `Field(le=0.0025)` validator in `config/settings.py` rejects any
`LIVE_RISK_FRACTION` value above the INV-05 cap (0.25%) at startup —  a typo that
would exceed the cap raises a `ValidationError` before any order can be placed.
There is no way to accidentally set 2.5% instead of 0.25%.

### Ramp policy

- The ramp is a **deliberate operator decision**, never automatic.
- Increase `LIVE_RISK_FRACTION` only after a documented live track record with
  positive out-of-sample realized P&L over at least [operator-defined N] live trades.
- Each ramp step is a single `.env` edit with a dated entry in Section 6 recording
  the justification.
- The maximum is `LIVE_RISK_FRACTION=0.0025` (the INV-05 0.25% cap). The validator
  enforces this hard ceiling — values above it raise `ValidationError` at startup.
- There is no automated ramp, no scheduled ramp, and no code path that increases
  the fraction without an operator `.env` edit.

---

## Section 4 — Rollback / Stand-Down

If at any point the operator decides to stand down from live trading (unexpected
behavior, adverse conditions, a failed reconcile, or any other reason):

### Immediate stand-down

1. **Set `LIVE_TRADING_ENABLED=false` in `.env`.**

   ```
   LIVE_TRADING_ENABLED=false
   ```

   This is instant: the gate in `fathom execute` reads the flag synchronously and
   refuses all subsequent live order attempts. No in-flight network call is needed.

2. **Set `ENV=demo` in `.env`.** Revert to the demo token/account if needed.

3. **Run `fathom reconcile` immediately** to sync local state to broker truth
   (INV-16). Check `fathom positions` for open live positions.

4. **Flatten/close open live positions via the OANDA interface.** The operator
   closes open positions directly through the OANDA web interface or the OANDA
   app. Fathom does not have an automated close-all command. Record each manual
   close with timestamp and reason in Section 6.

5. **Verify the monitor sees no open positions** — re-run `fathom positions` and
   confirm the list is empty before ending the stand-down session.

### Automated backstop — the daily-loss kill switch

The daily-loss kill switch is an automated backstop that operates independently
of the `LIVE_TRADING_ENABLED` flag. If the day's realized P&L hits the configured
loss cap, `fathom execute` refuses any new entries for the remainder of the UTC
day (INV-05). This is the last line of defense; the operator stand-down above
remains the primary control.

---

## Section 5 — Monitoring During Cutover

During the first live session, the following must be watched actively. The operator
does not walk away during the first live trades.

### What to run

Keep `scripts/run_monitor.py` running throughout the session:

```bash
python scripts/run_monitor.py --instruments <instrument> [--db-path PATH]
```

Keep `fathom reconcile` scheduled or run it manually every few minutes during
the first session to keep local state in sync.

### What to watch

- **Slippage on the fill.** Compare the fill price from `fathom positions` to the
  candidate `entry_ref`. Significant slippage (more than the expected spread)
  indicates a feed or latency issue.

- **Adverse path.** Watch the position from `scripts/run_monitor.py` output. A
  position moving immediately and significantly against entry can indicate a
  data/signal issue — not just bad luck.

- **Feed health.** The monitor checks for heartbeat timeouts. If the feed goes
  silent (no ticks for `heartbeat_timeout_seconds`, default 15s), it logs a
  WARNING. A prolonged silence during a live session is a stand-down trigger.

- **Discord alerts.** If `DISCORD_WEBHOOK_URL` is set, the monitor sends alerts
  on severe deviations. Confirm the webhook fires during the first session by
  watching the Discord channel in real time.

- **Reconcile drift.** Run `fathom reconcile` after the first fill. The output
  should show `adopted=0 closed=0`. Any drift (adopted or closed positions) means
  the local store and broker are out of sync — investigate before placing a second
  trade.

### Normal exit

A live session ends when:
- All open positions have hit their brackets (stop-loss or take-profit triggered).
- The operator decides to stand down (Section 4).
- The kill switch trips (daily-loss cap reached).

After all positions are closed, run `fathom reconcile` one final time and verify
`fathom positions` shows an empty list. Record the session summary in Section 6.

---

## Section 6 — Go/No-Go Decision Record

This section is the dated, reviewed record of the go/no-go decision and each
subsequent ramp step. The operator fills this in; nothing is automated.

**Template for an entry:**

```
Date: YYYY-MM-DD
Operator: <name>
Decision: GO | NO-GO | RAMP | STAND-DOWN
INV-07 prerequisites closed: T-08 [Y/N] · T-11 [Y/N] · T-06 [Y/N]
fathom preflight output: [paste GO/NO-GO + check summary here]
Notes: <rationale, observed edge, conditions>
Signed off by: <reviewer name>
```

---

### Entry 1 (fill on first live trade)

```
Date:
Operator:
Decision:
INV-07 prerequisites closed: T-08 [  ] · T-11 [  ] · T-06 [  ]
fathom preflight output:
Fill confirmation (fathom positions output):
scripts/run_monitor.py started at:
fathom reconcile output:
Notes:
Signed off by:
```

---

*(Add subsequent entries below as needed — ramp decisions, stand-downs, re-entries.)*
