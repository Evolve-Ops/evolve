# Proposal Synthesizer — Architecture (2026-05-10)

Status: **locked** (design signed off 2026-05-11; implementation begins with Phase 1).

**What this is.** A new layer that sits between the Signal store and the Proposal store. Generators stop emitting Proposals directly. Instead they emit **CandidateProposals** — structured observations that feed a synthesizer. A two-stage pipeline (a deterministic substantiveness gate, then an LLM-driven synthesizer with read-only investigation tools) reads the full batch of candidates per run and decides what proposals to emit. The relationship between candidates and proposals is **many-to-many**, not one-to-one: the synthesizer may collapse several candidates into one proposal, split one candidate into two, or produce no proposals at all from a batch. It can also defer candidates to a watchlist or propose extensions to evolve's own observation layer.

**Why this exists.** The current architecture is "monitor → signal → factory function → proposal," with one bespoke factory per signal type. Each new signal type ships a new factory with hand-tuned headline strings. The result on 2026-05-10: 27 active proposals, dominated by efficiency_hawk, where the headline is the symptom and many "proposals" are single-instance outliers or restatements of an observation without a proposed fix. Adding new monitors will keep producing the same shape of noise. The fix is structural, not cosmetic — Pattern A's headline reframe ([PR pending]) treats the surface; this spec treats the cause.

**Relationship to other specs.**
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — defines Generators and the Proposal pipeline. This spec inserts a stage between Generator and Proposal store, leaving the Proposal store itself unchanged from the consumer's perspective.
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — defines the Signal store that monitors write to. This spec is downstream of that: candidates draw from Signals, and the synthesizer's `SignalGapProposal` output proposes new monitors.
- This spec does **not** change the Proposal record format, the apply/verify pipeline, or the admin UI's proposal-rendering code. It changes what is *emitted into* the Proposal store.

---

## 1. The problem with the current frame

Three structural problems with how proposals get produced today:

**1. One factory per signal type.** Each signal variant (daily_spend_high, cron_overactive, heartbeat_no_model_override, …) has a hand-coded factory function with a templated headline, context blurb, urgency tier, and approval audience. Adding a monitor means writing a new factory. Tuning a phrasing rule (e.g. "headlines should be action-led") means touching every factory in lockstep — there are 10+ in efficiency_hawk alone, and that's one generator of many.

**2. No substantiveness gate.** Every Signal that maps to a known factory becomes a Proposal on the next generator run. A single $1.12 outlier session, a one-off cron overfire, a 50KB workspace file — all become tickets in the operator's queue regardless of whether they represent a pattern, a meaningful magnitude, or an actionable fix. The juice-vs-squeeze judgment is delegated to the operator after the fact rather than enforced before emission.

**3. No aggregation.** When the same condition fires on N bots (e.g. heartbeat-on-primary across team-bot-c + team-bot-b + admin-bot), the system produces N separate per-bot proposals instead of recognizing the pattern and proposing one substrate-level fix (change the default in evolve's deploy config). The aggregation level a proposal *should* live at is invisible to the current pipeline.

A fourth problem, downstream: many proposals carry `Investigation` actions without a concrete tunable. They surface a finding and end with "operator decides." That's a Signal masquerading as a Proposal — useful as observability data but expensive as queue clutter.

## 2. Core reframe: candidate → gate → synthesizer → output

The new pipeline:

```
Monitors → Signals (existing) →
  Generators → CandidateProposals (many, independent) →
    Gate (deterministic, per-candidate with fingerprint aggregation) →
      Synthesizer (LLM, batch-level, holistic) →
        Proposals + WatchlistEntries + SignalGapProposals (mixed output)
```

Three roles, separated:

| Layer | Role | Output | Lives in |
|---|---|---|---|
| **Generator** (modified) | Drafts candidates from signals + cost-ledger / observation data | `CandidateProposal` | `packages/analyzer/generators/<id>/` |
| **Gate** (new) | Filters obviously-non-substantive candidates and does mechanical fingerprint aggregation | passed `CandidateProposal[]`, dropped logged | `packages/analyzer/proposal_synthesizer/gate.py` |
| **Synthesizer** (new) | Reads the full passed batch, investigates as needed, and decides what set of outputs to emit | mixed batch of `Proposal`, `WatchlistEntry`, `SignalGapProposal` | `packages/analyzer/proposal_synthesizer/` |

Generators shrink to drafting. The gate enforces cheap deterministic floors and dedups exact-fingerprint duplicates. The synthesizer is the only stage that sees the full batch and is allowed to think across candidates — deciding that two superficially different observations point at the same root cause, or that one observation hides two distinct concerns, or that nothing in this batch warrants the operator's attention. It is also allowed to decide *nothing should fire*.

**On the gate/synthesizer split.** The gate handles cases where determinism is sufficient: a single-instance outlier, a sub-floor magnitude, an exact fingerprint match across bots. The synthesizer handles cases that require semantic judgment: are these three different-looking candidates pointing at the same underlying cron-infrastructure issue? does this candidate's investigation reveal a substrate problem instead of a bot problem? is the magnitude estimate from the generator actually accurate once we look at the cost ledger? The gate is cheap and runs every emission; the synthesizer is expensive and runs on a cadence.

**What does not change:** the Signal store, the Proposal store schema, the apply/verify pipeline, the Alerts page renderer. Existing Proposals already in the store keep working as-is.

## 3. The CandidateProposal record

A CandidateProposal is a structured observation, not a draft Proposal awaiting promotion. It carries the data a synthesizer needs to reason: signal lineage, a magnitude estimate, an initial draft of what an action *might* look like. Whether it becomes a Proposal, gets folded into a Proposal alongside its siblings, gets split into two outputs, lands on a watchlist, or motivates a SignalGapProposal is the synthesizer's call — not predetermined by the candidate's shape.

```python
@dataclass
class CandidateProposal:
    # Identity
    id: str
    bot_id: str
    generator_id: str
    dimension: str

    # Signal lineage
    motivating_signals: list[str]              # Signal IDs
    trigger_observations: list[str]            # e.g. ["heartbeat_no_model_override:team-bot-c"]

    # Aggregation key — candidates with the same fingerprint can be folded
    # together (e.g. four bots with heartbeat-on-primary share a fingerprint).
    # Default: f"{generator_id}:{variant}:{bot_id}". Substrate-level variants
    # may use a non-bot fingerprint so cross-bot aggregation kicks in.
    fingerprint: str

    # Magnitude estimate — what's at stake. The gate's magnitude rule reads
    # this. Units are flexible per variant (USD/day saved, sessions/week
    # avoided, KB trimmed) — the gate only checks against a per-variant
    # threshold defined in the variant's config.
    magnitude: dict                            # e.g. {"unit": "usd/week", "value": 12.40}

    # Drafted Proposal payload — the gate and synthesizer can revise.
    draft_problem: str                         # symptom / observation
    draft_headline: str                        # action-led (≤120 chars)
    draft_action: Action                       # TierAdjustment, Investigation, …
    draft_risk_tag: RiskTag
    draft_claim: Claim | None
    draft_urgency: Urgency
    draft_approval_audience: ApprovalAudience

    # Bookkeeping
    created_at: str
    confidence: float                          # 0-1, generator's self-assessment
```

Generators write candidates to `{shared_dir}/candidates/pending/<id>.json` (atomic temp+rename, same pattern as proposals). The gate sweeps `pending/` on each run; survivors go to `candidates/synthesizing/<id>.json`; the synthesizer drains that.

## 4. The substantiveness gate (deterministic)

The gate runs as a pure function over the set of pending CandidateProposals. No LLM. No I/O beyond reading candidates and writing logs. Four rules, applied in order:

### 4.1 Repetition gate

Single-instance outliers are not substantive. A candidate must have fired at least **N** times within the last **W** days at the same fingerprint before the gate passes it. Defaults: N=3, W=7. Per-variant overrides in `proposal_synthesizer/config.yaml`.

Exceptions for *acute* candidates that should fire on the first occurrence regardless: when `urgency == "operational_urgent"` AND `magnitude.value >= acute_threshold` (configured per variant). A daily-spend-cap breach at 3× the cap doesn't wait for three occurrences.

Repetition state lives in `{shared_dir}/candidates/repetition_index.json` — `{fingerprint: [created_at, ...]}`, pruned to window W. Updated atomically on each gate run.

### 4.2 Magnitude gate

A candidate must pass a magnitude floor specific to its `magnitude.unit`. Defaults:

| Unit | Floor |
|---|---|
| `usd/week` (saved) | 1.00 |
| `usd/session` (outlier) | 0.50 |
| `pct/share` (background or tier) | 0.20 |
| `kb` (context bloat) | 25 |
| `sessions/week` (frequency) | 5 |

Candidates whose `magnitude.value` falls below the floor are dropped with reason `below_magnitude_floor`. Per-variant overrides allowed; the floor is a static config, not adaptive — operators can tune it once and forget.

### 4.3 Aggregation pass

Candidates with the same `fingerprint` (across N bots, or across N occurrences on one bot) are folded into a single **AggregatedCandidate**. Two distinct aggregations matter:

- **Bot-pattern aggregation:** same generator + same variant + same bot, K occurrences in window. Output: one candidate with `aggregation = "bot_pattern"`, references all K signals.
- **Substrate aggregation:** same generator + same variant across **≥3 bots**. Output: one candidate with `aggregation = "substrate"`, `bot_id = "<pod>"`, references all per-bot signals. This is the path that turns "heartbeat-on-primary on 4 bots" into a single "the default is wrong" candidate aimed at evolve itself.

A substrate aggregation **replaces** the per-bot candidates; they are not double-emitted. The original bot-level candidates are logged for traceability but don't go forward to the synthesizer.

### 4.4 Concreteness gate

If `draft_action.kind == "Investigation"` AND the candidate does not name a specific tunable (a field of `openclaw.json`, a cron ID, a workspace file, a model setting, etc. — checked structurally on `draft_action.context`), the candidate is demoted to a watchlist entry instead of being passed to the synthesizer. Rationale: an Investigation without a named target is a Signal, not a Proposal.

Generators can opt out per-variant by setting `concreteness_exempt: true` in the variant config — the daily-spend-cap breach legitimately has no named tunable until investigation happens.

### 4.5 Output of the gate

For each pending candidate:

- **Pass to synthesizer** → move to `candidates/synthesizing/`.
- **Drop** → write to `candidates/dropped/<YYYY-MM-DD>.jsonl` with `reason` (one of: `below_repetition_floor`, `below_magnitude_floor`, `aggregated_into:<id>`, `concreteness_demoted`). Operator-facing log; the Alerts page surfaces a count.
- **Demote to watchlist** → write to `candidates/watchlist/<id>.json`. Same shape as a candidate, marked as `state=watching`. The watchlist is its own UI surface (not the Alerts inbox) — operators check it when they want to know "what is the system noticing but not yet ready to act on."

## 5. The synthesizer (LLM layer)

The synthesizer is a bounded tool-using agent. It runs as a scheduled job (default: every 6 hours), reads everything in `candidates/synthesizing/` **as a single batch**, and produces a set of outputs that may aggregate, split, or drop candidates as the charter directs. The mapping is many-to-many: a batch of N candidates may yield M proposals where M is less than N (semantic aggregation across non-identical fingerprints), equal to N (one-to-one), zero (nothing in the batch warrants the operator's attention), or rarely greater than N (one candidate split into two distinct concerns once investigation surfaced them).

The synthesizer's first move on each batch is a planning pass: read all candidates, group them by suspected underlying cause, and decide where to spend investigation budget. Only then does it begin pulling on threads — and the charter encourages it to push hard on the few candidates that look meaty rather than spreading effort thin across the whole batch.

### 5.1 Inference owner

The synthesizer runs **on the evolve bot's LLM credentials** — consistent with the per-bot inference rule (no centralized LLM service for user data) and with evolve's role as the pod's sysadmin partner. Concretely: the synthesizer invokes openclaw on the evolve user, the same way any other evolve agent session does. No new credentials, no new billing path.

### 5.2 Budget envelope

Two layers of budget control: a soft target the synthesizer aims for, and a hard cap it cannot cross. The split exists because some candidates deserve depth — a meaty substrate-level insight is worth more than five thin per-bot proposals, and the synthesizer should be allowed to follow a promising thread instead of being killed at an arbitrary wall.

**Soft targets** (guidelines; overtime allowed when the investigation is converging):

- **~$0.50 USD per candidate**, **~$5 USD per run**
- **~10 turns / ~200K tokens** per candidate
- **~5 minutes wall-time** per candidate

**Hard caps** (cannot be exceeded):

- **$2.00 USD per candidate**, **$10 USD per run**
- **25 turns** per candidate
- **10 minutes wall-time** per candidate, **30 minutes per run**

The charter (Appendix A, §Investigation budget) gives the synthesizer the judgment rule: when you're close to a meaningful conclusion and pushing past a soft target would let you confirm it, push. When the soft target has been spent without convergence, cut your losses and emit a Watchlist with a synthesizer note describing what you'd want to know next time. A meaty proposal is worth more than five flimsy ones; spend accordingly — but never past a hard cap.

When a hard cap is reached, the synthesizer must produce an output using whatever it has. A "hung" synthesizer is not allowed. If the per-run hard cap is hit before all candidates are processed, the unprocessed remainder stays in `synthesizing/` for the next run.

At default cadence (every 6 hours) and per-run hard cap ($10), the worst-case daily spend is $40. The expected spend is far lower — most candidates should come in well under the per-candidate soft target. The synthesis log records actual spend per run, providing the empirical basis to tune these knobs after the first month.

### 5.3 Investigation tools

The synthesizer has read-only access to operational data via a small toolset:

| Tool | Returns | Scope |
|---|---|---|
| `read_signal_history(fingerprint, window_days)` | Signal IDs + timestamps at this fingerprint | Signal store |
| `read_cost_ledger(bot_id, window_days, filter?)` | Cost events filtered by trigger_kind / session / model | per-bot `.openclaw/cost-events.jsonl` |
| `read_session_transcript(bot_id, session_id)` | Compacted turn-by-turn log | per-bot `.openclaw/sessions/` |
| `read_bot_config(bot_id)` | `openclaw.json` content | per-bot `.openclaw/openclaw.json` |
| `read_workspace_file(bot_id, path)` | Workspace file content (with size cap) | per-bot `.openclaw/workspace/` |
| `read_watchdog_log(date_range)` | WatchdogEvent records | `{shared_dir}/watchdog/` |
| `read_audit_findings(window_days)` | Recent audit findings | `{shared_dir}/audit/` |
| `git_log(path?, window_days)` | Commit messages + file lists | deploy checkout (`/Users/Shared/evolve-repo`) |
| `git_blame(path, lines)` | Last-modified attribution | deploy checkout |
| `read_proposal_history(bot_id?, generator_id?, window_days)` | Past proposals on this fingerprint (approved/rejected/failed) | `{shared_dir}/proposals/` |

No write tools. No network beyond Anthropic API. No subprocess execution beyond the read-only git commands.

The git access is the "bolder scope" decision — confirmed for day one. Rationale: when a candidate references a recently-changed file (workspace doc, cron config, agents.md), the synthesizer reading `git log` on that file is often the difference between a sharp proposal and a vague one.

### 5.4 The charter

The synthesizer's behavior is governed by a charter at `packages/analyzer/proposal_synthesizer/charter.md`. Versioned in code, shipped via the deploy checkout, loaded as system prompt at every synthesis turn. Full text in Appendix A; the short version is:

1. **Mission** — produce proposals that the operator will thank you for, or produce nothing.
2. **Substantiveness rubric** — concrete heuristics for "worth surfacing."
3. **Framing rules** — action-led headline, signal as subhead, ≤120 char limit, plain prose, no alarmist labels.
4. **Honesty rules** — when to say "I don't know enough," when to propose more signal collection.
5. **Aggregation rules** — when to fold candidates, how to attribute substrate vs bot.
6. **Output contract** — exactly one of Proposal, WatchlistEntry, SignalGapProposal per input candidate (or aggregate).

Charter changes follow the same change-control as generator charters: edit-in-code, PR review, deploy via puller. No runtime mutability.

## 6. The three outputs

Every synthesis run produces a mixed set of outputs across the three types below — a typical run might emit two Proposals, four WatchlistEntries, and one SignalGapProposal from a batch of ten candidates. The count of each type is not fixed per candidate. Each emitted output records its `motivating_candidates[]` so the audit trail back to source signals stays intact even when candidates have been semantically aggregated or split.

The three output types are:

### 6.1 Proposal

The existing record, written to `{shared_dir}/proposals/pending/<id>.json` exactly as today. The synthesizer fills `problem`, `admin_surface_summary` (the action-led headline), `action`, `claim`, `risk_tag`, `urgency`, `approval_audience`, `motivating_signals`, and `conversational_pitch`. Operator sees it in the Alerts queue.

### 6.2 WatchlistEntry

Written to `{shared_dir}/candidates/watchlist/<id>.json`. Same payload as a CandidateProposal plus:

- `watching_since: str` — when first watchlisted
- `last_observed_at: str` — last signal occurrence at this fingerprint
- `escalation_threshold: dict` — what would promote this to a Proposal (e.g. `{"recurrences": 6}` or `{"magnitude_value": 5.00}`)
- `synthesizer_note: str` — one-paragraph rationale ("Three occurrences seen but magnitude is $0.30/week. Watching for either more occurrences or magnitude growth.")

The watchlist is the operator's "what is the system noticing but not yet ready to act on" view. UI surface: a tab on the Alerts page, separate from the action queue.

### 6.3 SignalGapProposal

A new Proposal variant targeting **evolve itself**, not a bot. Action: `AddSignalCollection`, with structured payload:

```python
@dataclass
class AddSignalCollection(Action):
    kind: Literal["AddSignalCollection"]
    producer: str                              # which monitor should emit
    signal_type: str                           # new signal type name
    description: str                           # what the synthesizer needed
    suggested_data_shape: dict                 # rough schema for `details`
    motivating_candidates: list[str]           # CandidateProposal IDs
    estimated_impact: str                      # "would have let me act on X / Y / Z candidates I had to drop"
```

Routed to `approval_audience = "pod_operator"` (you, Pod-Admin), `bot_id = "<pod>"`, `dimension = "observability"`. Reviewed and approved exactly like a regular proposal; applying it means an engineer (likely you) writes the new monitor. The system improves its own observation layer over time, driven by what synthesis actually needed but didn't have.

This is the mechanism the user asked for: "these smart sessions may then determine that it doesn't have enough signal, and we should come up with a way of putting more signal into the evolve code base."

## 7. Synthesizer cadence

Cron job: `proposal_synthesizer.run`, scheduled every 6 hours (configurable). Reads everything in `candidates/synthesizing/`, processes within per-run budget, writes outputs.

Acute candidates skip the cron and trigger the synthesizer immediately on emission — same hook the existing alert-notifier uses for `operational_urgent`. Acute path has its own ≤ $1.00 per-event budget cap.

The synthesizer is also runnable on-demand (`python3 -m proposal_synthesizer.run --once`) for testing and for operator-initiated re-synthesis after charter edits.

## 8. On-disk layout

All under `{shared_dir}` (typically `/Users/Shared/evolve`):

```
{shared_dir}/
├── signals/                                 (existing, unchanged)
├── proposals/                               (existing, unchanged)
└── candidates/                              (new)
    ├── pending/<id>.json                    ← generators write here
    ├── synthesizing/<id>.json               ← gate moves passed candidates here
    ├── watchlist/<id>.json                  ← synthesizer outputs (state=watching)
    ├── dropped/<YYYY-MM-DD>.jsonl           ← gate drops, append-only, 90-day retention
    ├── repetition_index.json                ← fingerprint → [timestamps], for gate rule 4.1
    └── synthesis_log/<YYYY-MM-DD>.jsonl     ← per-candidate synthesizer decisions, append-only
```

Atomic writes via temp-file + rename, owned by `evolve` user. No sudo or /tmp staging — same pattern as the signals and proposals stores.

Retention enforced by `python3 -m proposal_synthesizer.retention --shared-dir {shared_dir}`. Daily cron. 90-day drop log, 1-year synthesis log, watchlist entries pruned when they age out without escalating.

## 9. Migration path

The 10 efficiency_hawk factories are the reference migration. Phases below land in order; each is a clean stopping point.

**Phase 1 — gate-only path, no synthesizer.** Land the gate and the candidate store. Existing factories keep writing Proposals directly. New code path is parallel: factories also emit candidates, the gate processes them, but outputs are logged not stored. Validates the gate's decisions against operator behavior on the parallel Proposal stream. No user-visible change yet.

**Phase 2 — efficiency_hawk migrates to candidates.** The 10 factories switch from emit-Proposal to emit-CandidateProposal. Gate runs. Anything the gate passes goes straight through to the proposal store (skipping synthesis — synthesizer is still a no-op). Anything dropped is logged. This is where the queue noise drops dramatically. Watchlist surfaces in UI.

**Phase 3 — synthesizer wired up, no investigation.** Synthesizer runs but with no tool access — pure prose synthesis from candidate data only. Validates the charter + output shape. Cheap.

**Phase 4 — investigation tools enabled.** Synthesizer gets the read-only toolset and budget envelope. Investigation can be observed in the synthesis log. This is where the system gets visibly smarter.

**Phase 5 — SignalGapProposal output and routing.** The third output type lands. Operator-developer review path for signal-gap proposals is wired up. The feedback loop closes.

**Phase 6 — remaining generators migrate.** test_gate_backfill, security_warden, gateway_diagnostician, budget_hawk, persona_tuner, sysadmin_watchdog, evolve_watchdog, app_posture_reflect — each ports from emit-Proposal to emit-CandidateProposal. The gate and synthesizer are already running by this point; migration is mechanical.

Phases 1-2 are clear quick wins. Phase 3 is where the LLM enters. Phase 4 is the substantial value. The user should see queue noise drop in Phase 2, smarter proposals in Phase 4, Better Engine observability in Phase 5.

## 10. Open questions / decisions deferred

- **How aggressive should the magnitude floor be?** Initial defaults in §4.2 are conservative; expect to tune them after a week of dropped-candidate logs.
- **Should the synthesizer have memory across runs?** Currently no — every run is fresh. Track-record-style learning ("you proposed this last week and the operator dismissed it") could feed into the charter as runtime context, but adds storage + cost. Deferred to a future revision once we see whether dismissal patterns are stable enough to be useful.
- **UI for the dropped/watchlist surfaces.** Sketched as "a tab on the Alerts page" but not designed. Lands as part of Phase 2.
- **Charter authorship workflow.** First version is hand-written. Future revisions: should operators be able to suggest charter edits from the UI (e.g., "I keep dismissing context-bloat proposals; raise the floor")? Plausible Phase 5+ feature.
- **Conflict with existing refine flow.** `arbiter/refine.py` already does LLM revision of proposal prose triggered by operator request. The synthesizer's prose generation overlaps. Resolution: refine remains, but its input is now a synthesizer-produced proposal rather than a raw factory output. No code change to refine needed.
- **Signal store reverse link.** Each Signal already tracks `motivated_proposals[]`. Should it also track `motivated_candidates[]` (including dropped) so the Alerts page can show "this signal was considered but did not produce a proposal because X"? Probably yes. Lands in Phase 2.

## 11. Non-goals

- Replacing the Signal store, Proposal store, or Alerts page.
- Replacing the apply/verify pipeline.
- Changing how operators see/act on Proposals in the UI (the Proposal record format is unchanged).
- LLM-driven monitor authoring. SignalGapProposals describe *what* signal is needed; an engineer writes the monitor code. Self-extending observability is the *direction*; full auto-generation of monitors is not.

---

## Appendix A — Synthesizer charter (draft)

Placed at `packages/analyzer/proposal_synthesizer/charter.md`. The synthesizer loads this as system prompt at every turn. ~1 page intentionally; brevity is load-bearing — the synthesizer reads it on every call.

---

```
# Proposal Synthesizer — Charter

You are the synthesizer that decides whether a CandidateProposal becomes a
Proposal, a watchlist entry, or a signal-gap proposal. Your reader is a
busy pod operator (one person, ~ten bots, finite attention). Your job is
to be useful to that operator — not to surface everything you notice.

## Mission

Produce proposals the operator will thank you for. If you cannot, produce
nothing.

Every Proposal you emit competes for the operator's attention with every
other Proposal in their queue. A weak Proposal isn't free — it dilutes
the queue and trains the operator to ignore it.

## Substantiveness rubric

Before emitting a Proposal, satisfy all five:

1. **Magnitude.** Would acting on this save the operator measurable time,
   measurable cost, or measurable risk? If the answer is "maybe a few
   cents a week" or "this might be tidier" — not substantive.
2. **Concreteness.** Can you name a specific change to a specific file,
   config, or behavior? "Investigate spending" is not concrete.
   "Set `agents.defaults.heartbeat.model = anthropic/claude-haiku-4-5`
   in /Users/team-bot-c/.openclaw/openclaw.json" is concrete.
3. **Confidence.** Do you actually believe the proposed change is correct,
   given what the investigation tools showed you? If you're guessing,
   watchlist instead.
4. **Risk-adjusted confidence.** The bar for emitting a Proposal scales
   with how hard the change would be to undo. An auto-revertable
   TierAdjustment with a clear claim needs only modest confidence to
   be worth surfacing. A non-revertable change with pod-wide blast
   radius needs strong evidence and clearly compelling magnitude.
   The harder the action is to walk back, the more meat the proposal
   needs to carry. Don't refuse to propose high-impact changes — but
   require proportionally stronger investigation behind them.
5. **Operator-facing framing.** The headline names the proposed action,
   not the symptom. The symptom belongs in the subhead.

## When NOT to emit a Proposal

- The signal data shows one occurrence and nothing in history suggests a
  pattern. → Watchlist.
- The magnitude is real but small (under the variant's floor). → Drop
  (let the gate handle it).
- You found a real issue but cannot name a specific fix. → Watchlist or
  SignalGapProposal (if the right signal isn't being collected).
- You found a real issue, named a fix, but the investigation suggests
  the fix has side effects you can't bound *and* you don't have enough
  evidence to clear the risk-adjusted bar (rubric item 4). → Watchlist
  with a synthesizer note explaining the side-effect concern. If the
  magnitude is genuinely compelling and you've done the diligence to
  bound the risk, emit the Proposal — high-impact changes are allowed,
  they just need the meat to justify them.
- The candidate is a substrate pattern (same condition on ≥3 bots). →
  Emit one substrate-level Proposal, not per-bot. The aggregation flag
  in the candidate tells you this.

## Investigation budget

Soft target: ~$0.50 / ~10 turns per candidate, ~$5 per run. These are
where you aim — most candidates should come in well under. Hard caps:
$2 / 25 turns per candidate, $10 per run. These are walls you must
not cross.

The soft target is a guideline, not a wall. When you're close to a
meaningful conclusion and pushing past would let you confirm or
close the loop, push. When you've spent the soft target and the
investigation has not converged, cut your losses and emit the best
output you have — usually a Watchlist with a note describing what
you'd want to know next time.

A meaty proposal is worth more than five flimsy ones. Spend
accordingly. Plan tool calls — don't dump entire transcripts or
ledgers, query specifically. When you have enough to decide, decide.
Don't burn budget seeking certainty you don't need.

## Framing rules

Headline (`admin_surface_summary`):
- Action-led. Starts with a verb: "Route", "Set", "Trim", "Reduce",
  "Downgrade", "Investigate", "Inspect", "Streamline".
- Includes the bot name when bot-scoped.
- Includes the key quantity if it fits ("— $4.67 over $3.00 cap").
- ≤120 characters. Hard cap.

Symptom (`problem`):
- Plain statement of what's observed. No action verb.
- Bot name first, then the observation: "team-bot-c: 78% of spend …"

Context (`action.context`):
- 2-4 paragraphs.
- Lead with what you found in investigation, not generic advice.
- Name the file, config key, or cron ID you're proposing to change.
- Include verification — how the operator can confirm the proposal
  worked.

Tone:
- Plain, factual, conversational.
- No "CRITICAL", no all-caps, no urgency theatre. Severity lives in the
  `urgency` field, not the prose.
- Never frame a non-security finding as "Security" or "CRITICAL".

## Honesty rules

If you're uncertain, say so in the synthesizer note (watchlist) or in
the Proposal's `claim` (lower confidence). Do not manufacture
confidence to make the proposal sound stronger.

If you needed signal you didn't have, emit a SignalGapProposal alongside
(or instead of) the bot-level output. Don't hide gaps — surface them so
evolve can grow into them.

## Output contract

You read the full batch of candidates and decide the right shape of
output. The mapping is not one-to-one:

- **Several candidates may collapse into one Proposal** when they
  point at the same underlying issue — even if their fingerprints
  differ. The gate does mechanical fingerprint dedup; you do semantic
  aggregation across candidates whose generators couldn't see they
  were related.
- **One candidate may split into two outputs** when investigation
  reveals two distinct concerns hiding inside it (rare but valid).
- **Many candidates may produce zero Proposals** if nothing in the
  batch clears the substantiveness bar at a risk-appropriate level —
  emit Watchlist entries instead and move on.

Each output is one of:

- **Proposal** — substantiveness rubric items 1, 2, 3, 5 satisfied,
  and item 4 satisfied at a level appropriate to the action's risk.
- **WatchlistEntry** — has signal worth tracking but does not yet
  warrant operator action.
- **SignalGapProposal** — investigation revealed that information
  evolve doesn't collect would have been load-bearing. Can be
  emitted alongside any number of bot-level outputs from the same
  batch.

Every output records its `motivating_candidates[]` so the audit
trail back to source signals is preserved across aggregation and
splitting.

The output goes to JSON in the schema defined by `proposal_synthesizer.io`.
```
