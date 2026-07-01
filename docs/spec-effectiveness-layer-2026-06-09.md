# Spec: The Effectiveness Layer — the bot's "chief of staff"

**Status:** draft / design · **Date:** 2026-06-09 · **Author:** Fable (design) + pod-admin (vision)

> **Phase C (the Fit Reviewer) is now made buildable in
> [spec-fit-reviewer-2026-06-12.md](spec-fit-reviewer-2026-06-12.md)** — grounded in the live-pod
> substrate, with the engine design, the gallery-install-first action-space call, the
> altitude/value schema bridge, a falsifiable proof artifact on `team-bot-a`, and a bite-sized build
> plan. §5/§6/§7/§10/§12-C below are superseded by that spec; the three-layer model (§2) and the
> triage of the 138 (§11) remain the conceptual parent.

A new, higher layer of RSI that looks at *how a person actually uses their bot* and
suggests ways the bot could serve them better — calendar-keeping for a personal
assistant, better reporting for a project manager, automation ideas for a home bot —
and judges whether existing apps are underused because they're low-value or because
they need to change. This is a **different layer of abstraction** from today's RSI,
which generates low-level sysadmin proposals (and observations dressed as proposals —
138 of them queued today).

---

## 1. Motivation — the 138, and a layer-of-abstraction confusion

Today's RSI generators detect operational patterns (cost spikes, config drift,
gateway health, engagement outliers) and surface them as user-facing **proposals**.
Two things went wrong:

1. **Wrong altitude.** Even the generators in the `capability_growth` dimension
   (`app_suggester`, `pod_capability_lift`) detect by **keyword-matching manifest
   names against a vocabulary** (`generators/app_suggester/observe.py`). They can
   notice "your manifests don't contain the word *calendar*." They cannot notice
   "*this person keeps wrestling their schedule by hand*." Keyword-gap ≠
   understanding the user's goals.
2. **Wrong type.** Many entries are **observations**, not proposals — "you should
   look into X." An observation is a FYI, not an action.

The result is 138 low-value items in a queue that was meant to deliver something
entirely different: a high-level read of the user's work and concrete ways the bot
could be more effective for *them*.

Notably, the codebase already half-knows the distinction: `arbiter/routing.py` splits
dimensions into **operational** (`substrate_health`, `cost`, `safety`) and
**improvement** (`utility`, `capability_growth`, `voice_fit`, …). The improvement
dimensions exist; the generators inside them just operate too low.

## 2. The three-layer model

| Layer | Question | Who does it today | Cadence | Engine |
|-------|----------|-------------------|---------|--------|
| **0 — Operations** | Is the bot healthy, secure, affordable? | guardians (sysadmin_watchdog, security_warden, budget_hawk…) | continuous | cheap Python |
| **1 — Tuning** | Is it running efficiently? | optimizers (efficiency_hawk, cache_ttl_tuner…) | continuous | cheap Python |
| **2 — Effectiveness / Fit** | Is the bot *good at the job this person hired it for*, and what would make it better? | **nobody — this is the gap** | periodic | **LLM synthesis** |

Layers 0–1 are *mechanic work*. Layer 2 is the bot's **chief of staff**: it
periodically steps back and asks "are we doing the right work, and what should we
take on next?" This is the same "synthesis layer (pattern-miner → objective-aware
proposal)" the 2026-06-09 diligence review named as the one unbuilt piece of RSI.

## 3. The keystone — telemetry → synthesis → a few suggestions

The single architectural move that fixes the 138 *and* delivers the vision:

> **Today's generators stop talking to the user. They become telemetry that *feeds*
> Layer 2.** Cheap continuous signal (Layers 0–1) → one expensive periodic
> **synthesis** (Layer 2) → a handful of high-level, purposeful suggestions.

The 138 stop being a proposal queue and become **raw material**. Layer 2 is the brain
that reads the low-level signals + the actual conversation history + *what the bot is
for*, and emits the three suggestions that matter. This also preserves the
"RSI-must-be-cheap" principle: cheap telemetry, *one* sanctioned rare-expensive
synthesis — not LLM-everywhere.

## 4. Bot purpose — the anchor

Layer 2 needs to know **what a bot is for**. No such field exists today (`purpose`
lives on generator charters, never on bots). Add it per-bot (network.json bot block,
or the bot's evolve workspace config):

```jsonc
"purpose": {
  "archetype": "personal-assistant",   // enum, see below; extensible
  "mission":   "Keep my schedule, triage email, and handle travel.",  // one line, freeform
  "captured":  "declared",              // "declared" (operator/wizard) | "inferred"
  "confidence": 1.0,                    // 1.0 when declared; <1 when inferred
  "reviewed_at": "2026-06-09T…"
}
```

**Archetypes (starting set, extensible):** `personal-assistant`, `project-manager`,
`home-automation`, `research-analyst`, `customer-facing`, `custom`. Each archetype
carries a **playbook** — the effectiveness lenses Layer 2 applies (a PM playbook
looks for reporting/comms friction; a home-automation playbook looks for manual
sequences that should be routines; etc.). Playbooks are the extension point.

**Capture:** declared at creation — the conversational bot-creation wizard is the
natural place to ask "what's this bot for?" (closes a low-friction-creation gap).
Inference is the fallback and a refinement signal: the reviewer may notice "you set
this up as research but mostly use it for scheduling — want to reframe it?"

## 5. The Fit Reviewer — the engine

A new component, distinct from today's generators in **cadence** (periodic, not
signal-driven) and **nature** (LLM reflection, not heuristic detection). It reuses the
proposal store / arbiter pipeline for *output routing* only.

- **Cadence:** weekly or monthly per bot (configurable per archetype/activity). Driven
  by a scheduled job (extend the audit-scheduler, or a dedicated fit-review job).
- **Locus:** runs **inside the bot, with the bot's own credentials** (like
  `user_profile_inferrer`) — privacy by architecture (per-bot inference, never
  centralized). The bot already holds its own transcripts.
- **Honors the DNT / observation opt-out** from v1 (the user-observation principle):
  a user who's opted out is not analyzed, and there's a wipe path.

### Input contract — `ReviewContext` (assembled per run)

```
ReviewContext = {
  purpose,                      // §4
  transcript_sample,            // recent N sessions of real conversation (in-bot)
  usage_summary,                // noun×verb×engagement tuples rolled up
  installed_apps,               // manifests + what each claims to do
  app_usage,                    // per-app invocations_per_week trend (already a metric)
  user_profile,                 // who the user is (user_profile_inferrer output)
  operational_signals,          // the DIGESTED Layer-0/1 proposals — "what the
                                //   maintenance crew noticed", as input not output
}
```

### Output contract — 0–3 `EffectivenessSuggestion`s

```jsonc
{
  "title": "Offer to keep your calendar",
  "archetype_lens": "personal-assistant / life-admin automation",
  "jobs_to_be_done": ["scheduling", "meeting coordination"],
  "evidence": [                         // REQUIRED — see §6
    "12 scheduling asks last week; 9 resolved by hand in-chat",
    "no calendar app installed; 'when am I free' asked 5×"
  ],
  "suggestion_type": "add_capability",  // add_capability | modify_app | retire_app |
                                        //   surface_app | workflow_change | reframe_purpose
  "specific_action": "Install a calendar app (Google Calendar) and offer to add events when scheduling comes up.",
  "expected_benefit": "Removes the ~9/week manual scheduling round-trips.",
  "confidence": 0.8
}
```

**Zero suggestions is a valid, common, good answer.** The reviewer is not required to
find something; flooding is the failure mode we're escaping.

## 6. Grounding — anti-confabulation (non-negotiable)

Every suggestion **must cite the usage evidence** that motivates it (the `evidence[]`
field). No evidence → no suggestion. This is the cite-or-don't-recommend principle
already established (the evo-confabulation lesson) applied to the reviewer. It is what
separates *"you asked about scheduling 12× and resolved 9 by hand"* from generic
*"have you considered a calendar?"* fluff. A suggestion whose evidence doesn't survive
a second look is dropped, not shipped.

## 7. App-fit evaluation — a first-class Layer-2 question

For each installed app, combine the **invocation trend** (`app.invocations_per_week`,
already a registered metric) with an **LLM read of the related transcripts**, and
classify:

| Class | Signal | Action |
|-------|--------|--------|
| **thriving** | used + the related need is met | leave it |
| **underused — low value** | rarely used; no related need in transcripts | **retire** |
| **underused — mis-fit** | the related need *exists* but the user works around the app | **modify** (reposition / change scope) |
| **underused — undiscovered** | the need exists, the app fits, the user doesn't know it's there | **surface** it |

The retire-vs-modify distinction the operator asked for **requires reading the *why***
— which a usage count cannot do and an LLM read of the transcripts can.

## 8. Output surface + proposal type

`EffectivenessSuggestion` is a **distinct type**, routed to the **bot's owner** (the
human who benefits), on a user-facing surface framed as opportunity — *"Ways [bot]
could help you more"* / an **Ideas** page — **not** the sysadmin proposals tab. The
operational proposals stay where operators expect them; effectiveness suggestions get
their own, calmer home.

## 9. Verification — the soft adoption loop

Layer 2 does **not** use the verify daemon's hard-metric loop (right for Layer 1,
wrong here). Verification is *adoption + usage-shift*, observed over weeks:

- Did the owner **accept** the suggestion (or dismiss it)?
- If acted on, did **usage shift** in the expected direction (e.g., manual
  scheduling round-trips dropped; the new app actually got used)?

A Fit Reviewer whose suggestions are adopted *and* move usage earns authority; one
whose suggestions are ignored or don't help loses it. Same authority concept as the
operational generators, but the metric is adoption-and-usage-shift, not a single
resolver. The loop is slower and fuzzier — and that's honest for this altitude.

## 10. Closing the loop — suggestion → forge

The payoff state realizes the original vision end-to-end: Fit Reviewer detects *"a PA
bot this active should keep a calendar"* → owner approves → **the forge generates the
calendar app manifest**. Detect-need → suggest-capability → build-it. This is the app
framework (the differentiator with no public competitor) doing what it was always for.

## 11. Triage of the existing 138 (near-term — separable from Layer 2)

Independent of building Layer 2, deflate the queue now by reclassifying:

| Current item shape | Reclassify as | Goes to |
|--------------------|---------------|---------|
| concrete, reversible, verifiable **action** | **proposal** (keep) | the proposals surface |
| observation / "look into it" | **FYI** | a low-priority digest (not a queue) |
| low-level signal a synthesizer should read | **telemetry** | feeds Layer 2; not surfaced |

Plus: raise the bar in the operational generators' charters/routing — *emit a proposal
only when there's an action; otherwise it's telemetry.* This quiets the queue
immediately and is the right cleanup regardless of Layer 2.

## 12. Phased build plan

- **A. Triage (deflate the 138).** Reclassify proposal vs FYI vs telemetry; raise the
  operational-proposal bar; add a FYI digest surface. *No Layer 2 needed.*
- **B. The purpose anchor.** `purpose` schema + capture in the bot-creation wizard +
  an inference fallback.
- **C. Fit Reviewer v1.** One archetype, one bot, weekly. Assemble `ReviewContext`,
  run the LLM reflection in-bot, emit `EffectivenessSuggestion`s to a new Ideas
  surface. Evidence-grounded. *This is the proof-of-vision milestone — does it feel
  like what you wanted?*
- **D. App-fit classifier** (§7).
- **E. Close the loop** — suggestion → forge builds the app (§10).
- **F. Soft verification** — adoption + usage-shift tracking + reviewer authority (§9).

## 13. Open questions / decisions

- **Archetype taxonomy:** fixed enum vs freeform mission only? *Recommend: fixed enum
  (drives the playbook) + freeform mission.*
- **Cadence:** weekly vs monthly? Cost vs freshness; likely activity-scaled.
- **Generic-vs-specific risk:** the reviewer's prompt + the grounding requirement
  mitigate, but staying specific-not-bland needs a small eval set. This is craft, not
  architecture — flagged honestly.
- **Reuse vs parallel track:** how much of the generator/charter/arbiter machinery
  does the Fit Reviewer reuse vs. a dedicated path? *Recommend: reuse the proposal
  store + a new type + surface; a dedicated scheduler + in-bot runner.*
- **Transcript access + privacy:** runs in-bot (the bot owns its transcripts), honors
  the DNT opt-out + wipe path from v1.

## 14. Fit with existing principles

- **Per-bot inference, never centralized** — the reviewer runs in the bot. ✓
- **RSI must be cheap** — this is the *one* sanctioned rare-expensive layer; telemetry
  stays cheap. ✓
- **Cite-or-don't-recommend** — the grounding requirement (§6). ✓
- **App framework is the differentiator** — closing the loop with the forge (§10). ✓
- **Low-friction bot creation** — purpose captured in the creation wizard (§4). ✓
- **User-observation opt-out (DNT)** — honored from v1 (§5). ✓

---

**The one-line version:** today's RSI is the bot's maintenance crew; this spec adds
its chief of staff — and the move that builds the chief of staff is the same move that
silences the 138 (they become its briefing materials, not the user's inbox).
