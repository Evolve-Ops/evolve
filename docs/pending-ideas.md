# Pending ideas — open threads from current work

A running log of ideas we've raised but haven't executed on. When the
Layer-3 FeatureManifest intake ships, each of these should be converted
into a proper manifest under `docs/feature-manifests/`. Until then,
this is the single source of truth for "things we've agreed are worth
doing but haven't started."

Grouped by theme. Each entry: what, why, size estimate, dependencies,
notes. Add items here when they surface; remove when they ship (and
reference the PR that closed them).

Last updated: 2026-04-20

---

## 1. Intake surfaces (Pod-admin's architecture ask, this session)

### 1a. Evolve admin UI — "Submit a bug"

**What.** A "Submit a bug" button inside the admin UI (not just ETR).
Scope: bugs the running pod can fix operationally — bot configs,
`openclaw.json` settings, channel-integration state, cron jobs, the
Evolve bot's own setup. Anything requiring a code change goes to ETR.

**Why.** Operators will see issues in the UI; the current flow makes
them open a new Claude session or email Pod-admin. The admin UI is where
they are; intake should be there.

**Size.** ~half day.
- New button in header/topbar
- Writes to `{shared_dir}/admin-ui-reports/<id>.yaml`
- New generator `ui_report_triager` produces Investigation proposals
- Reports appear in the Proposals page dimensioned as `meta_health`

**Dependencies.** None blocking. Ships independent of L3 feature
intake.

**Notes.** Triage target is "the arbiter" not "Opus directly" —
meaning reports become proposals that flow through the same routing
+ referee machinery as everything else. Preserves the "one queue"
invariant.

---

### 1b. ETR dashboard — "Propose a capability" (Layer-3 FeatureManifest v0)

**What.** A new intake path for "I want Evolve to gain this new
functionality." Separate from bug reports because the workflow is
different: design → scaffold → PR, not diagnose → fix.

**Why.** Pod-admin's words: "if I wanted to add something to the Evolve
code — not a code fix but a new set of functionality — I'd like to
be able to do that." This is the Layer-3 FeatureManifest shape from
the ETR spec §7.v4; we've been describing it architecturally but
haven't built it.

**Size.** ~2 hours for v0; ~2 days for full scaffolding.

**v0:**
- Dashboard tab "Propose a capability" with fields:
  problem / desired outcome / acceptance criteria / scope /
  implications
- Writes `docs/feature-manifests/<date>-<slug>.yaml`
- Opus cron drains it: writes a design-sketch into the manifest,
  proposes a branch name, no code yet
- Pod-admin reviews the design → Accept → Opus scaffolds on next pass

**v1:**
- Acceptance criteria auto-compile to an ETR catalog entry so the
  verify step is concrete
- `scaffolder_generator` that produces actual code on a feature
  branch

**v2:**
- Multi-round iteration: Pod-admin leaves comments; Opus adjusts

**Dependencies.** None for v0. Later versions benefit from:
- Auto-running fixture lifecycle (Layer D green) so generated code
  can be ETR-verified before merge

**Notes.** The three-layer framing from the ETR architecture spec
makes this the natural Layer-3 primitive: the dashboard is the
intake, the arbiter is the scheduler, and ETR is the verifier.

---

## 2. RSI v2 follow-ups (findings we haven't closed)

### 2a. Bots refuse deferred promises (SOUL.md gap)

**What.** When Pod-admin tested "message me 'green' in 10 minutes without
setting a cron," admin-bot refused the prompt citing SOUL.md — "without
a cron job I have no continuity between messages; that would be a
lie." Technically correct, but the Continuity Engine exists to back
bots up on exactly this. Bots don't know CE is there.

**Fix direction.** SOUL.md addendum (or POD_CONDUCT.md amendment)
saying: "You have a Continuity Engine. If a user asks for deferred
action (time-based or condition-based), you MAY commit — the CE
will schedule + execute the follow-up. Your commitment doesn't
require a cron."

**Size.** Small (text change to SOUL template) but needs careful
phrasing so bots don't over-promise (e.g., shouldn't commit to
things CE can't handle).

**Dependencies.** CE must actually work end-to-end. Current state:
task_extractor requires ≥3 turns (MIN_TURNS_FOR_LLM_REVIEW); single
turns get skipped. See ETR finding #001.

---

### 2b. Verify daemon not on launchd cadence

**What.** Layer D-3 of the RSI verification catalog stays `[opus]`
because the verify daemon in `packages/analyzer/verify/daemon.py`
isn't scheduled. It exists as code but nothing fires it. Until it
does, applied proposals never transition past `unknown` verify_status.

**Fix direction.** Add a LaunchDaemon plist at deploy time (via
`deploy.py`), or an hourly cron on the mini. Plist should fire
`verify/daemon.py` once an hour.

**Size.** ~30 minutes.

---

### 2c. AgentsAppend and InstallApp appliers missing

**What.** The Proposal schema declares 10 action kinds; appliers
exist for 8. AgentsAppend and InstallApp are declared but not
implemented. Fixtures land cleanly; D2 fails at apply with "no
applier."

**Fix direction.** Implement both under `packages/analyzer/arbiter/appliers/`.
AgentsAppend is simple (append text to `{bot}/.openclaw/AGENTS.md`
with a provenance comment). InstallApp is harder — needs Forge /
gallery integration.

**Size.** AgentsAppend: ~1 hour. InstallApp: ~half day (depends on
Forge availability).

---

### 2d. Layer C activation items still unchecked

**What.** Several C-items in the RSI v2 checklist haven't been
drained by the worker yet:
- C2 Budget Hawk sees cost data — needs Budget Hawk to have fired at
  least once; hourly cadence may not be wired
- C4 Evolve Watchdog stays quiet — worker hasn't tried this one
- C5 Referee is ranking — currently shows as an ambiguous finding;
  marked as a cascading-duplicate by Opus but actually means
  proposals-list-is-empty, not that the referee's broken

**Fix direction.** Let the worker drain these naturally as cron
fires. C2 is time-gated; come back in 24h. C5 will resolve itself
when real proposals start flowing (or when we inject fixtures and
they show up ranked, like we just did).

**Size.** 0 active work — passive observation.

---

### 2e. C1 — Evolve Watchdog never ran

**What.** Watchdog's cadence is declared as `daily` in its charter,
but after 24h+ of pod uptime, zero watchdog events have been written
to `{shared_dir}/watchdog/`. Either the scheduler isn't running it,
or it ran and correctly saw nothing to flag.

**Fix direction.** Same as 2b — generator scheduler needs a LaunchDaemon
plist on the mini. The arbiter registry knows what cadence each
generator wants, but nothing fires them.

**Size.** Probably same work as 2b — one scheduler plist that reads
each generator's charter cadence and fires accordingly.

**Dependency.** Opus finding #006 has the full analysis; once
designed this closes two catalog items (C1 + C4) at once.

---

## 3. ETR plumbing (quality-of-life improvements)

### 3a. Auto-merge worker-state branches

**What.** Every worker cycle pushes a `claude/etr-worker-<ts>`
branch. These are pure state updates (daily log + findings). Pod-admin
(or me) opens + merges a PR for each. Pile-up during busy periods.

**Fix direction.** Have the worker push to a single rolling branch
`etr-worker-state` and auto-merge it via a GitHub Action (no human
review needed for state-only commits — they don't touch code).

**Size.** Hour-ish. Adds `.github/workflows/etr-auto-merge.yml` or
similar.

---

### 3b. Submit-a-bug acknowledgment

**What.** When a bug report is submitted via dashboard, user gets a
toast but no persistent confirmation. Report appears in inbox lane
immediately but the flow is quiet.

**Fix direction.** A banner on the Inbox tab: "You submitted 2 bugs
today · tonight's Opus pass will triage." Dismissable.

**Size.** ~15 min.

---

### 3c. Verify-daemon status surfaced in dashboard

**What.** Verify daemon is a core L2 capability but has no
dashboard visibility. When it runs, when it confirmed/refuted
claims, what's in its queue.

**Fix direction.** New Meta-health subtab on the dashboard: "Verify
daemon" with activity timeline + current in-flight claims.

**Size.** ~half day.

**Dependency.** 2b (verify daemon needs to actually run first).

---

## 4. Budget Hawk v2 — cost forensics + lineage (NEW, 2026-04-21)

### 4a. Per-call lineage tracking

**What.** Today's Budget Hawk sees aggregate spend and flags spikes. It
does not answer "what did this turn cost me, and why." Pod-admin's test
case: a "are you alive?" turn on team-bot-c cost $0.25. Most of that was a
one-time cache invalidation from a key rotation; without lineage,
there's no way to know that from the spend alone.

**Design.** Tag every LLM call at the plugin layer with structured
lineage metadata:
- `trigger_kind`: user_turn | background_cron | heartbeat | classifier |
  task_extractor | agent_subagent
- `trigger_id`: user turn-id or cron-job-id it belongs to
- `session_key`: which session
- `model`, `tokens_in`, `tokens_out`, `cache_hit_rate`, `cost_usd`
- `cache_state`: fresh | warmed | invalidated

Store per-call records in `{shared_dir}/cost-ledger/{YYYY-MM-DD}.jsonl`.
Roll up nightly. Budget Hawk reads the ledger instead of just scoreboard.

**Size.** Medium — touches plugin (add lineage tags), analyzer (roll up),
admin UI (new view). ~1-2 days.

### 4b. Cache-invalidation event detection

**What.** Key rotations, SOUL.md edits, POD_CONDUCT.md changes, agent
config churn — each invalidates Anthropic's prompt cache and forces
re-upload on the next call. A bot with 10 sessions × 30k context ×
$3.75/M cache-write = ~$1 of burst cost on the next poll across
sessions.

**Design.** Plugin detects cache_state=invalidated (Anthropic returns a
cache-miss flag). Budget Hawk surfaces: "Cache invalidation detected;
N sessions × M tokens × $X to re-warm." Near-real-time alert so the
operator knows why the next hour will be expensive.

Bonus: expose "expected re-warm cost" as a preview before deliberate
invalidations (edit SOUL.md → "this edit will cost ~$X to propagate").

**Size.** Small — maybe half day. Depends on 4a.

### 4c. User-turn cost vs. background cost split

**What.** Today you can't easily ask "how much did I actually spend on
conversations with my bots?" vs. "how much did I spend on things the
user never sees (heartbeats, cron tasks, classifiers)." The two have
very different cost curves and warrant different budgets.

**Design.** Once 4a ships, aggregate the ledger by `trigger_kind`:
- USER: trigger_kind in {user_turn, agent_subagent-from-user}
- BACKGROUND: everything else

Cost Measures page gets two top-line numbers + drill-down per category.
Budget Hawk can have separate thresholds for each.

**Size.** Small — pure rollup + display. Depends on 4a.

### 4d. Session context sprawl + idle session tracking

**What.** Team-bot-c's openclaw status shows 47 active sessions, some 6-8
days old at 99% cached. Each carries 20-37k tokens that get
re-hydrated whenever the session is invoked. Many are probably dead
— a user asked something once, got an answer, never returned.

**Design.** Track session context size over time (per-bot time series).
Flag:
- Sessions idle >3d — candidates for archival (save re-hydration cost
  on any future rotation/edit)
- Main-session context growth rate (20k → 80k in a week = alert)
- Total context × active-bots = "cold re-warm cost if caches lost"

**Size.** Small — status endpoint already exposes session metadata.
Medium if we want archive automation.

### 4e. Forensics UI ("what did this cost me")

**What.** Interactive "explain the spike" UI. Click any spike on the
Cost Measures time series → drill down to the specific turns / sessions
/ triggers that drove it. Paste a session_key or turn_id → see the full
lineage tree of LLM calls, costs, and cache states.

**Design.** Cost Measures page becomes a lineage explorer. Reads the
ledger from 4a. Group by: time, bot, session, trigger_kind, model.
Click-through navigation between axes.

**Size.** Medium-to-large — real frontend work. ~2-3 days.

### 4f. Spec doc

Before implementing, write a proper spec:
`docs/spec-budget-hawk-v2-cost-forensics-<date>.md`. Covers all of the
above with concrete data model, ingestion pipeline, UI wireframes,
alert thresholds. Start here.

---

## 5. Deferred spec items

### 4a. Signal-target calibration rollback

**What.** `POST /api/arbiter/health/snapshots/<id>/restore` returns
501 for `signal` target. User + generator targets work. Signal
calibration storage layer isn't wired up.

**Fix direction.** Spec says signal calibration is per-signal
threshold metadata. Storage path: `{shared_dir}/calibration/signals/<id>.json`.
Restore callback writes the snapshot data back to the signal's
threshold file. ~2 hours.

---

### 4b. POD_CONDUCT.md amendment flow

**What.** Amendments to POD_CONDUCT.md are supposed to go through
the proposal/approval system. No proposal kind handles this yet.

**Fix direction.** New action kind `PodConductAmendment` with an
applier that writes to `{shared_dir}/POD_CONDUCT.md` with git
tracking. Always requires `pod_operator` approval; always has a
revert plan (previous version).

**Size.** ~half day.

**Dependency.** Depends on all bots reading POD_CONDUCT.md from
shared_dir (already the case per deploy.py).

---

### 4c. Conversational NL approval path (evo keyword)

**What.** Spec at `docs/spec-better-engine-conversational-approval-2026-04-18.md`
describes letting users approve/reject proposals conversationally
via the `evo` keyword. Not implemented; spec only.

**Fix direction.** Per the spec.

**Size.** ~day.

---

## How to close an item

1. When shipping work that closes an item here, edit the item to a
   single line: `**Closed** by PR #NN (<date>)`.
2. Move closed items to an "Archive" section at the bottom so the
   main list stays scannable.
3. When the FeatureManifest pipeline ships (1b above), convert the
   remaining items to manifests under `docs/feature-manifests/` and
   point this doc at them.

---

## Archive

_(empty — nothing closed yet. First graduate from this list gets
added here.)_
