# Feature: hermes-job-definitions

**Status.** ready
**Phase.** Phase 2
**Owner.** saambaby
**Last updated.** 2026-05-29

## Summary

The Phase 2 capstone: the plain-English **Hermes daily job** that wires the whole watchlist pipeline together and delivers it to Discord. On schedule, Hermes calls `fathom scan`, runs Claude per candidate for news-risk ([[news-risk-assessment]]) and narration ([[watchlist-narration]]), calls `fathom chart` for survivors, and posts the ranked watchlist + charts to the Discord channel via its own gateway. **Hermes is configured, not coded** — this feature's deliverable is the job definition + the prompt wiring + an operator runbook, *not* a built service. The job ends at Discord delivery: **Hermes never places orders (INV-01).**

## User-facing behaviour

The trader receives, on schedule (weekday, after a major session close, in their timezone), a Discord message with: a ranked list of candidate pairs, a one-line Claude rationale per candidate (with a news-risk flag where `event_risk != low`), and a chart PNG per surviving candidate showing entry/stop/target. Candidates the news-risk verdict marks `skip` are vetoed; `reduce_size` are flagged.

Deliverable artefacts (in `hermes_integration/jobs/`):
- `daily.md` — the plain-English Hermes job: trigger, the ordered tool calls, the Claude prompt steps, the delivery target, and the failure/safe-default behaviour.
- An operator runbook section: how to register the job in Hermes, point it at the `fathom` CLI as a tool, and connect the Discord gateway.

## Sequence diagram

> Included: 4 actors (Hermes, fathom CLI, Claude, Discord) + scheduled/async coordination + per-candidate fan-out — well past the threshold.

```mermaid
sequenceDiagram
    participant Cron as Hermes scheduler
    participant H as Hermes session
    participant F as fathom CLI
    participant Cl as Claude
    participant D as Discord

    Cron->>H: trigger (weekday, post-session-close)
    H->>F: fathom scan
    F-->>H: ranked watchlist — JSON Candidate[] on stdout (INV-13 shape;<br/>high-impact-news candidates already dropped, medium flagged)
    loop per candidate
        H->>Cl: news_risk prompt (currencies + calendar events)
        Cl-->>H: {event_risk, reason, suggest_action}  (validated; malformed→skip, INV-02)
        alt suggest_action == skip
            H->>H: veto candidate
        else proceed / reduce_size
            H->>F: fathom chart <instrument>
            F-->>H: chart PNG path
            H->>Cl: narration prompt (candidate facts)
            Cl-->>H: one-line rationale (fallback to template on failure)
        end
    end
    H->>D: deliver ranked watchlist + charts
    Note over H,D: job ends at delivery — Hermes places NO orders (INV-01)
```

### Failure modes considered

- `fathom scan` returns an empty watchlist (empty approved-set, INV-10) → Hermes posts "no candidates today" rather than failing.
- Claude news-risk malformed/unavailable → `parse_news_risk` defaults to `skip` (INV-02) → that candidate is vetoed (fails safe, never "proceed").
- Claude narration unavailable → deterministic `fallback_narration` (cosmetic; candidate stays).
- A prompt-injected calendar headline can only ever produce a bad *suggestion* that gets vetoed/down-ranked — never a trade (INV-01, the whole point of the boundary).
- Discord delivery fails → retry per Hermes' gateway; the watchlist is still persisted by `fathom scan` for the panel (Phase 5).

## Acceptance criteria

- [ ] `daily.md` defines the trigger, the exact ordered tool calls (`scan` → per-candidate news-risk → `chart` → narration → deliver), and the delivery target.
- [ ] The job applies the news-risk verdict: `skip` ⇒ veto, `reduce_size` ⇒ flag, `proceed` ⇒ keep (consuming the [[news-risk-assessment]] contract).
- [ ] **Hermes is given access ONLY to `fathom scan|watchlist|chart`** — never an execute/order/risk entrypoint (INV-01). The runbook states this explicitly.
- [ ] Empty watchlist and malformed-Claude paths are specified to fail safe (INV-10, INV-02).
- [ ] The operator runbook is sufficient to register the job, wire the CLI as a tool, and connect Discord — without code changes to Fathom.
- [ ] **Acceptance is operational (D-P2-5):** a configured Hermes instance delivers a coherent, actionable watchlist to Discord on ≥5 consecutive daily runs (human-reviewed).

## Component design

Not a coded component — a **configuration artefact**. `daily.md` is the Hermes job (plain English / its `/cron` form). The only Fathom code it leans on is the CLI ([[cli-commands]]) and the two prompt+helper modules ([[news-risk-assessment]], [[watchlist-narration]]). No Discord library, no `anthropic` SDK in Fathom (Hermes owns both — D-P2-3, D-P2-4).

**Watchlist source (AMBIGUOUS-03 resolution):** the job consumes `fathom scan`'s **stdout JSON** directly (the `Candidate[]` array, INV-13 shape) — it does *not* call `fathom watchlist` (that's the persisted-read accessor for re-reads / the Phase 5 panel). **News relationship (AMBIGUOUS-01 resolution):** `fathom scan` has *already* dropped high-impact-news candidates (deterministic gate) and flagged medium ones (`news_flag`); the per-candidate Claude news-risk step is the *finer qualitative veto* on the survivors (it can still `skip`/`reduce_size` a candidate the deterministic gate passed). The two layers are pre-filter (hard, deterministic) then LLM-veto (soft, qualitative) — not a contradictory double-filter.

## Non-goals

- No order placement, sizing, or risk logic — the job ends at Discord (INV-01; Phases 3–4).
- No intraday job (Phase 2 is daily/swing only — Decision #6; the intraday variant is the same structure on a faster schedule, added later).
- No Fathom-side Discord client or Claude client.

## Touches

- [INV-01] — **the canonical INV-01 feature**: Hermes's authority ends at the watchlist; it has no order path.
- [INV-02] — consumes the validated news-risk verdict; malformed → skip.
- [INV-10] — empty approved-set ⇒ empty watchlist ⇒ "no candidates" message.
- [INV-08] — Discord/Anthropic credentials live in Hermes' config / `.env`, never committed.

## Depends on

- [[cli-commands]] — the tools Hermes calls.
- [[news-risk-assessment]] — the verdict contract + parser.
- [[watchlist-narration]] — the narration prompt + fallback.
- Hermes Agent (external, configured) + a Discord channel + the Anthropic key (operator-provided).

## Approach

Author `daily.md` as the job definition with the ordered steps from the sequence diagram, explicitly bounding Hermes' tool access to the read-only/watchlist CLI (INV-01). Write the operator runbook for registering the job, wiring the CLI tool, and connecting Discord. The Fathom code it depends on (CLI, prompts, parsers) is delivered by the other Phase 2 specs; this spec is the integration + configuration layer and the human-operational acceptance.

## Open questions

- **D-P2-4 — RESOLVED (recommended, overridable):** delivery via Hermes' Discord gateway; Fathom emits JSON + PNGs, builds no delivery code.
- **D-P2-5:** the live ≥5-run Discord acceptance requires a running Hermes + Discord channel — an operator/human gate, not an automatable test. Flag in the taskgraph as a manual, human-admin task.
- Schedule specifics (which session close, which timezone) — operator preference; `daily.md` carries a sensible default (weekday, post-NY-close, user TZ).

## Out of scope

- Execution, risk, monitoring, the admin panel (Phases 3–5). Anything that acts on the watchlist beyond delivering it.
