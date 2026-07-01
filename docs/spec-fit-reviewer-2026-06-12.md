# Spec: The Fit Reviewer — the L2 capability engine

**Status:** buildable / design · **Date:** 2026-06-12 · **Author:** Claude (design) + pod-admin (vision)

**Supersedes the Phase-C section of**
[spec-effectiveness-layer-2026-06-09.md](spec-effectiveness-layer-2026-06-09.md) (§5, §6, §7,
§10, §12-C). That spec named the Fit Reviewer and set its disciplines; this one makes it
**buildable**, grounded in the substrate that actually exists on the live pod (inspected
read-only 2026-06-12). The Effectiveness-Layer spec stays the conceptual parent — its
three-layer model (§2) and its triage of the 138 (§11) are unchanged. The decision record
[decision-rsi-synthesis-layer-2026-06-09.md](decision-rsi-synthesis-layer-2026-06-09.md) is
the cardinal lesson this spec must not relapse on.

> **One line:** the Fit Reviewer reads a bot's *real usage against its declared purpose*,
> picks the *one* capability the evidence demands, cites the behavior that demands it, maps it
> to a *real installable gallery app*, and emits a single high-altitude proposal — or, when the
> evidence is thin, emits nothing.

---

## 0. The cardinal lesson (why this is not the 138 again)

The 138 low-value items came from **unsupervised pattern-mining over observation tuples in a
vacuum** — "this bot uses Sonnet at 10:30" dressed as a recommendation. The fix is not a better
miner. It is two disciplines the engine **enforces structurally**, not stylistically:

1. **Purpose-anchored.** The engine never asks "what patterns exist?" (unanswerable, infinite).
   It asks "is this bot serving *what it's declared for*, and what one capability would help
   most?" (answerable, bounded). No declared purpose → the engine does not run a capability
   pass for that bot.
2. **Cite-or-don't.** Every capability claim cites specific observed behavior, or it is not
   made. A hallucinated "build this app" is far costlier than a hallucinated config nudge, so
   **the evidentiary bar rises with altitude.** A suggestion whose citation doesn't survive a
   second look is dropped, not shipped. No fabricated numbers — every number traces to the
   observation store or a transcript quote.

Both are checkable gates in code (§3.4, §3.6), not prompt etiquette. The rest of the spec is
mostly about making those two gates real.

---

## 1. What the live substrate actually supports (grounded, not aspirational)

Inspected read-only on the pod 2026-06-12. This section is load-bearing: the spec is built on
what the data *is*, and where it's thin, the spec says so and routes around it.

### 1.1 Observation tuples — rich enough to **target**, not to **cite**

`/Users/Shared/evolve/observations/<bot_id>/<YYYY-MM-DD>.jsonl`. Each tuple is
`(noun × verb × mood × engagement)` + `extraction_confidence` + `session_id` + `source_hash`.
**There is no raw text** — by design (the tuple layer is the privacy-preserving rollup; raw
text stays in-bot per the capture policy).

`team-bot-a` (multi-user project-management bot, the proof candidate), all-time aggregate:

| Axis | Distribution (top) |
|------|--------------------|
| **noun** | `task-management` **69**, `evolve-system` 29, `home-management` 14, `document-generation` 13, `slack-comms` 9, `health-fitness` 6, `email` 3, `ops` 2, `calendar` 2, `travel` 1 |
| **verb** | `recording` **64**, `troubleshooting` 29, `reviewing` 16, `planning` 12, `tracking` 11, `coordinating` 4, `summarizing` 3, `scheduling` 1 |
| **mood** | `neutral` 110, `enthusiastic` 24, **`frustrated` 11**, `urgent` 1 |
| **engagement** | **uniformly `1`** across all 148 tuples |

What this tells us, concretely:

- **The tuples are a real targeting signal.** `task-management` dominates team-bot-a's life (69, the
  plurality), with `document-generation` (13) and `slack-comms` (9) close behind — and **team-bot-a
  has zero apps installed** (`/Users/Shared/evolve/applications/team-bot-a/` does not exist). That is
  a textbook L2 gap: the #1 thing the bot is used for has no supporting capability.
- **Friction is observable.** `frustrated` mood (11) and `troubleshooting` verb (29, the #2
  verb) mark where the user is fighting the bot. `calendar` appears only with
  `verb=troubleshooting` — a hint, not a citation.
- **The tuples CANNOT carry the citation.** "You asked about scheduling 12× and resolved 9 by
  hand" is *not derivable* from `(calendar, troubleshooting, neutral, 1)`. The number, the
  "by hand", the user's words — none of it is in the tuple. **`engagement` is uniformly 1, so
  it is dead weight; do not rank on it.**

**The architectural consequence (the spine of this design):**

> The tuples are the **cheap targeting layer** — they tell the reviewer *which* purpose-aligned
> domain to look at and *that* there is enough recurring activity (distinct sessions × distinct
> days) to be worth an expensive look. The **transcript reflection** is the **evidence layer** —
> it produces the cite-or-don't quotes. Targeting is pure Python (free, runs always); the
> reflection is one bounded LLM call (rare, gated on the targeting floor). This is exactly the
> "cheap telemetry → one sanctioned synthesis" shape the principle demands.

`personal-bot` (personal-assistant, pod-admin's daughter, light use) is the **negative control**: 4 days,
a handful of tuples, dominated by `evolve-system`/`code` (the bot was used to *look at Evolve*,
not to do PA work). No purpose-aligned domain clears a support floor. The correct Fit Reviewer
output for personal-bot today is **zero suggestions** — and that is a PASS, not a miss (§4).

### 1.2 Three blocking prerequisites the live pod exposes

The proof bot is not ready to be reviewed today. The spec must own this, not paper over it:

| Prereq | Live state (2026-06-12) | Consequence |
|--------|--------------------------|-------------|
| **Purpose anchor** | `network.json::bots.team-bot-a.purpose` is **`null`** (so is personal-bot's). The schema + write path shipped (Phase B, `bot_purpose.py`) but **no real bot has a declared purpose**. | The capability pass **must not run** without a purpose. Bite 1 declares team-bot-a's purpose (`project-manager` + a one-line mission). Inferred-purpose is a fallback for *targeting framing* only — never the anchor for a shipped capability proposal. |
| **User profile** | `/Users/Shared/evolve/profiles/` contains only `atlas.md`. team-bot-a/personal-bot have **no profile**. | `user_profile` is an *optional enrichment* input, not a dependency. The reviewer degrades gracefully when it's absent (it is, today). |
| **Installed apps** | team-bot-a/personal-bot have **no apps**. | Simplifies v1: the only live opportunity is `add_capability`. App-fit classification (retire/modify/surface, Effectiveness-Layer §7) has nothing to chew on yet — **deferred to a later phase**, not in v1. |

### 1.3 The action space is real, bounded, and vetted

- **Gallery** (`packages/gallery/catalog.json`): **14 real, installable apps**, each a
  `{pkg_id: "p-<hex>", name, description, categories, application_tags, keywords}`. These are
  the v1 action space. team-bot-a's dominant nouns map *directly* onto real apps:
  **Project Tracker** (`p-f6a7b8c9`, tags `task-management`/`calendar`), **Weekly Status
  Reporter** (`p-e5f6a7b8`, tags `document-generation`/`slack-comms`/`task-management`),
  **Meeting Agenda Builder**, **Slack Communications Manager**. The match is not a coincidence —
  it's what "serving its purpose" looks like when you read the data.
- **`InstallApp(app_id, source="gallery")`** (`schema/proposal.py:192`) binds an
  already-reviewed package. Touches `app_install` ⇒ **always human-approved**, never autonomous.
- **`BuildApp(bot_id, app_id, app_name, manifest)`** (`schema/proposal.py:322`) is the Forge
  path: it persists a brand-new manifest and runs a ~10-step **async** ForgeJob
  (`arbiter/appliers/build_app.py`, `applications/forge_jobs.py`). Heavier and riskier than an
  install. **Phase 2.**

> **Critical contrast with `app_suggester`:** its `catalog.json` (18 categories) `example_apps`
> (`"daily-health-log"`, `"expense-log"`) are **illustrative slugs, not gallery `pkg_id`s** — they
> map to nothing installable. So `app_suggester` *structurally cannot* emit an `InstallApp`; it
> emits an `Investigation` ("consider installing X"). The Fit Reviewer reads the **real** gallery
> and emits an `InstallApp` with a real `pkg_id`. Menu-of-fake-names vs. evidenced-real-install.

---

## 2. How this beats the three existing L2-shaped generators

The thing to beat is `app_suggester`, `engagement_amplifier`, `pod_capability_lift` (all
`dimension: capabilities`). Studied in full. They are *better than nothing* but share a ceiling:
**they ideate from a fixed vocabulary, not from what the user is actually trying to do.**

| | `app_suggester` | `engagement_amplifier` | `pod_capability_lift` | **Fit Reviewer** |
|---|---|---|---|---|
| **Ideation source** | curated `catalog.json` keyword-gap vs. manifest names | a firing `engagement_amplification_opportunity` Signal (noun,verb cluster) | aggregates the other two's Signals across bots | the **bot's purpose × its real transcripts** |
| **Can name a real installable app?** | No — illustrative slugs | No — "draft a manifest" | No | **Yes — a gallery `pkg_id`** |
| **Output** | `Investigation` ("consider…") | `Investigation` ("deepen…") | `Investigation` (pod-wide "consider…") | **`InstallApp`** (human-approved) |
| **Evidence** | counts attached *if* an upstream Signal scoped (bot,category) — else silent | tuple counts from the Signal | summed tuple counts | **transcript quotes** + tuple counts, cite-or-don't |
| **For team-bot-a today** | **silent** (no `app_suggester_gap` Signal exists; confidence-floor `0.85` > catalog-match `0.6`, `observe.py:53`) | silent (no amplification Signal) | silent (no upstream Signals) | **one cited `InstallApp` for Project Tracker** |

The differentiator is not "use an LLM." It is **ideate from observed intent, then bind to a
real action.** `engagement_amplifier` already reads tuples and is purpose-*aware* (confirmed vs.
emergent vs. contradicted against AGENTS.md) — good, and the Fit Reviewer reuses that framing —
but it can only say "deepen (noun,verb)"; it can't say "*here is the app, and here's the line in
your chat that proves you need it.*" That last clause is the altitude jump.

---

## 3. The engine

### 3.1 Nature and locus

A new component, distinct from today's generators in **cadence** (periodic, not signal-driven)
and **nature** (one bounded LLM reflection, not a heuristic detector). It reuses the proposal
store / arbiter pipeline for **output routing only**.

**Locus — runs in-bot, on the bot account, with the bot's own LLM credentials.** This is not a
preference; it is the per-bot-inference principle
([principle-per-bot-inference.md](principle-per-bot-inference.md)) and the only design that can
read transcripts at all. The proven precedent is **App Audit**
([spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md) §"Ownership: bot, not Evolve"):
each bot runs its own audit on its own account; `audit_runner.py` writes results to
`/Users/<bot>/.openclaw/workspace/evolve/audit_outbox/<id>.json`; the admin-side
`audit_poller.py` (hourly, via the `audit-scheduler` tick) ingests into the pod-wide stores and
archives. **The Fit Reviewer is the same shape.** No new daemon, no central inference.

```
  bot account (team-bot-a)                         evolve account (admin)
  ┌─────────────────────────────┐             ┌──────────────────────────────┐
  │ fit_review_runner.py        │             │ fit_review_poller.py         │
  │  (bot-scheduled, monthly)   │   outbox    │  (audit-scheduler tick)      │
  │  1. assemble ReviewContext  │  ───JSON──▶ │  4. validate (pkg exists,    │
  │  2. targeting floor (Py)    │             │     evidence non-empty)      │
  │  3. ONE LLM reflection ─────┤             │  5. build InstallApp Proposal│
  │     → 0–1 cited candidate   │             │     (altitude=L2, value)     │
  │     written to outbox       │             │  6. arbiter.store.write      │
  └─────────────────────────────┘             └──────────────────────────────┘
        bot's LLM creds, bot's data                reads structured output only
```

The poller reads the bot's **structured output** (a candidate suggestion with cited evidence),
never the raw transcript — consistent with "aggregation reads structured outputs, not raw
content."

**Honors DNT from v1.** A user who has opted out of observation (the security_warden
default-on opt-out) is not reviewed; the runner checks the opt-out before assembling context and
exits clean. Wipe path is the same as the observation store's.

### 3.2 Input — `ReviewContext` (assembled in-bot)

```
ReviewContext = {
  purpose:        {archetype, mission, captured, confidence},   # §1.2 — REQUIRED, captured="declared"
  targeting:      TargetingReport,                              # §3.3 — pure-Python, drives the focus
  transcript_sample,   # recent N sessions for the targeted domain only, within capture policy
  installed_apps,      # manifests + what each claims (empty for team-bot-a today)
  gallery_catalog,     # the 14 real installable apps (the bounded action space)
  user_profile,        # OPTIONAL enrichment; absent for team-bot-a/personal-bot today — degrade gracefully
  operational_digest,  # the DIGESTED L0/L1 findings — "what the maintenance crew noticed", as input
}
```

The reflection sees the transcript **only for the domain targeting already selected** — not the
whole history. This bounds cost and blast radius and keeps the LLM from free-ranging (the 138's
original sin).

### 3.3 The targeting step (pure Python — the cheap gate before any LLM)

`TargetingReport` is computed with **no LLM**. It is the discipline that makes the expensive step
rare and purpose-anchored:

1. Roll up the bot's tuples over the window (default 60 days) by `noun`: `distinct_sessions`,
   `distinct_days`, `frustration_share`, top co-occurring `verb`s.
2. Score each noun for **purpose alignment** against the declared archetype's playbook (the
   `project-manager` playbook weights `task-management`, `document-generation`, `slack-comms`,
   `calendar`; reuse `engagement_amplifier`'s confirmed/emergent/contradicted-vs-AGENTS.md
   framing).
3. **Support floor (the gate):** a noun is a candidate only if
   `distinct_sessions ≥ 3` AND `distinct_days ≥ 2` AND it is purpose-aligned (or strongly
   emergent). For team-bot-a, `task-management` (69 events, many sessions/days) clears easily;
   `travel` (1) does not. **For personal-bot, nothing clears** → the runner exits without an LLM call
   and writes a `no_candidate` outbox record with the reason. *This is the line that prevents
   the 138: no targeting floor cleared ⇒ no expensive reflection ⇒ no proposal.*
4. For each surviving candidate, pre-match the gallery catalog by `application_tags` ∩ noun-domain
   to a shortlist of **real `pkg_id`s** (so the LLM chooses among real apps, never invents one).

The targeting report is itself a useful, auditable artifact ("why did the reviewer look here?")
and is the **first buildable bite** (§5) precisely because it needs no LLM and proves the
substrate is sufficient.

### 3.4 The reflection step (the one sanctioned LLM call)

**This is the bounded, rare, per-bot place expensive synthesis is allowed.** Justification of
cost, explicitly:

- **One call per bot per cadence.** Default cadence **monthly**, activity-scaled: a bot that
  cleared no targeting floor is skipped entirely (personal-bot costs ~zero). A manual "review now"
  trigger exists for the proof run and for operators.
- **Bounded input:** purpose (1 line) + targeting report (small) + the targeted-domain
  transcript sample (capped, e.g. ≤ 6 recent sessions, ≤ ~8K input tokens) + the 14-app catalog.
  Model tier: the standard/power rung (not Haiku — this is the one place judgment matters), but
  output-capped (≤ ~600 tokens, 0–1 candidate).
- **I/O-free + injected LLM callable**, mirroring `user_profile_inferrer/extractor.py` (takes a
  callable so tests inject a stub; the runner layer owns file writes). This is what makes the
  engine unit-testable without a live model.

The reflection's job is narrow: *given this purpose, this evidence, and these real apps, is
there exactly one capability whose need the transcript demonstrates? If yes, name the app and
quote the evidence. If no, say so.* The prompt forbids: suggesting an app not on the shortlist;
asserting any number not present in the targeting report or quotable from the transcript; and
emitting more than one suggestion per run (flooding is the failure mode we're escaping —
"zero suggestions is a valid, common, good answer").

### 3.5 Output — a single capability candidate (then a Proposal)

> **As-built note:** the JSON below is the original design sketch. The
> **authoritative, shipped** candidate contract (the exact shape Bite 4 builds
> against) is in the §7 Bite-3 "As built" block and `fit_review/candidate.py`.
> Notable refinements: `decision`/`no_candidate` left the outbox entirely (a
> declined run writes nothing; the reason goes to the per-bot trail), evidence is
> a list of `{quote, session_id, ts}` objects (not strings), and the value field
> is `value_estimate` (== `schema.ValueEstimate`).

The in-bot runner writes a candidate record to the outbox:

```jsonc
{
  "kind": "fit_review_candidate",
  "bot_id": "team-bot-a",
  "decision": "suggest",                       // "suggest" | "no_candidate"
  "app_pkg_id": "p-f6a7b8c9",                  // MUST exist in gallery catalog
  "app_name": "Project Tracker",
  "archetype_lens": "project-manager / status + task tracking",
  "jobs_to_be_done": ["task tracking", "status reporting"],
  "evidence": [                                // REQUIRED, ≥1; each line cites a real observation
    "task-management was team-bot-a's #1 activity: 69 recording/tracking events; no app supports it",
    "transcript 2026-06-09: user dictated 7 project tasks in one session for the bot to hold",
    "document-generation (13) + slack-comms (9) co-occur — weekly status done by hand"
  ],
  "expected_benefit": "Gives team-bot-a a durable place to hold tasks + draft the weekly status it already assembles ad hoc.",
  "value_tier": "high",                        // DETERMINISTIC (§3.6), not LLM-asserted
  "value_basis": "purpose-aligned #1 domain, 69 events / N sessions / M days, no current coverage",
  "altitude": 2,
  "targeting_support": {"distinct_sessions": 0, "distinct_days": 0, "frustration_share": 0.0}  // filled from §3.3
}
```

> **Runner contract — citations MUST be structured, not prose.** The `evidence: [str]`
> form shown above is the *legacy* shape; the parser (`fit_review.candidate`) still
> accepts it so a field-name drift on rebase doesn't wedge the poller, **but prose lines
> carry no `session_id` and so are never citable.** A prose-only candidate therefore
> **silently fails Gate A** (§3.6) and is dropped. The Bite-3 runner MUST emit the
> structured shape — `cited_evidence: [{quote, session_id, ts?}]` — whose `session_id`s
> exist in the bot's turn store, or every one of its suggestions is dropped at the gate.

The poller (`fit_review_poller.py`) validates and converts to a `Proposal`:

- `action = InstallApp(app_id="p-f6a7b8c9", source="gallery")`
- `altitude = 2`, `value` = §3.6 (deterministic), `dimension = "capabilities"`,
  `surface = "improvement"`, `approval_audience = "bot_primary_user"` (the human who benefits),
  `urgency = "improvement"`, `risk_tag = {blast_radius: bot, reversibility: manual,
  touches: ["app_install"]}` ⇒ human-approved.
- `human_title = "Give team-bot-a a Project Tracker"`, `summary` = the one-sentence pitch,
  `explanation` = the cited evidence rendered, `coalesce_key = "fit_review:team-bot-a:task-management"`
  (so re-runs fold, never duplicate), `motivating_signals` = the operational-digest Signals it read.

### 3.6 Cite-or-don't and honest value — the two structural gates

**Gate A — cite-or-don't (poller-side, deterministic).** A candidate with `decision="suggest"`
is **dropped** unless: `evidence` is non-empty; **every** numeric claim in `evidence`/`value_basis`
reconciles against the observation store (the poller re-derives the counts and rejects on
mismatch — the LLM cannot inflate "69"); and `app_pkg_id` exists in `packages/gallery/catalog.json`
(mirrors `apps_inherit_bot_llm_validator.py`'s "verify, don't trust" posture). A transcript-quote
evidence line must carry a `session_id` the bot can attest to. Fail any ⇒ drop, log the reason.
**No fabricated value numbers ever reach a proposal.**

**Gate B — honest value (deterministic function, never an LLM number).** `value_tier` is
**computed** from the targeting support, not asserted:

```
value_tier = high   if purpose_aligned AND distinct_sessions ≥ 8 AND no_current_coverage
             medium if purpose_aligned AND distinct_sessions ≥ 3
             low    otherwise            (and "low" + InstallApp is usually dropped as not worth a card)
```

`value_basis` is the human-readable trace of that computation. There is **no dollar field** —
capability value is not dollar-denominated, and `estimated_savings_usd` (which already exists for
cost-model generators) must stay `None` here rather than carry a fabricated number. (Contrast: the
`project_recommendation_legibility` lesson — "cite-or-don't; pod-grounded value, no fabricated
numbers.")

---

## 4. Action-space recommendation: **gallery-install-first (v1), Forge phase 2**

**Recommendation: v1 emits `InstallApp` against the 14-app gallery only. Forge (`BuildApp`) is
phase 2, gated on a trusted ideation track record.** I validate the coordinator's lean.

**The deciding reason: decouple "is the idea good?" from "is the generated artifact safe?"**

A Fit Reviewer suggestion has two independent failure modes:
1. **Ideation failure** — the idea doesn't match what the user needs (the 138 risk).
2. **Artifact failure** — the *thing produced* is broken or unsafe.

Gallery-install has **zero artifact risk**: `InstallApp` binds an already-reviewed package; the
14 apps are vetted; the action is human-approved and reversible (manual uninstall). So a
gallery-first v1 isolates failure mode #1 — we can prove *ideation quality* on a bounded action
space where the only question is "did the reviewer pick the right real app for the right cited
reason?" That is **falsifiable against a fixed catalog**: the suggested `pkg_id` either matches
the evidence or it doesn't.

Forge (`BuildApp`) **compounds both risks**: it ideates *and* generates a novel manifest *and*
runs a ~10-step async job. Turning Forge loose before ideation is trusted is precisely the
mistake the 138 teaches — an expensive, hard-to-verify mechanism let loose before the cheap,
bounded version is proven. And it is unnecessary for the proof: team-bot-a's strongest gap
(`task-management`) is *already covered by a real gallery app*. We don't need to build what we
can install.

**Phase 2 (Forge) trigger, made concrete:** extend to `BuildApp` only when (a) the reviewer has
shipped ≥ N gallery suggestions with a positive adoption+usage-shift track record (§ soft
verification), **and** (b) a targeting candidate clears the floor but the gallery shortlist is
empty (a genuine, evidenced gap no vetted app fills) — that's the honest moment to build rather
than install. Until then, an unfilled gap yields **zero suggestions** (honest) rather than a
forced bad gallery match or a premature build.

---

## 5. Altitude + value: the schema bridge to legibility

This is the bridge to the presentation/legibility workstream
([spec-recommendations-rework-2026-06-02.md](spec-recommendations-rework-2026-06-02.md), which
already names the **Improvements** page as the home for "app-shaped product suggestions, written
from observed usage… target 1–2/app/week" and a collapsed **Cleanup** section). Today there is
**no altitude field** — ranking is `urgency × authority + savings_bonus` (`arbiter/ranking.py`),
and `coalesce_key` folds by *root cause*, not by *altitude*. Altitude is the missing orthogonal
axis (the `project_rsi_proposal_altitude` lesson: presentation × altitude are two axes that
multiply).

### 5.1 New fields on `Proposal` (`schema/proposal.py`) — spec-only here, built in Bite 2

```python
# Altitude — the value/ambition tier, orthogonal to urgency (which is about
# *attention*, not *ambition*). L0 hygiene / L1 optimize / L2 capability / L3 strategic.
# None = unset (backward-compat); charter carries a default, per-proposal override allowed
# (same pattern as `surface`). Deterministic; never LLM-asserted.
altitude: int | None = None          # 0 | 1 | 2 | 3

@dataclass
class ValueEstimate:
    tier: Literal["low", "medium", "high"]   # computed (§3.6 Gate B), never asserted
    basis: str                               # human-readable trace of the computation
    evidence_refs: list[str] = field(default_factory=list)  # the cited observations/sessions
    # No dollar field — capability value is not dollar-denominated; faking one is the 138 sin.

value: ValueEstimate | None = None
```

Both serialize unconditionally in `to_dict`/`from_dict` (None/[] on old proposals on disk) so the
admin server can branch without defensive null checks — the established pattern for every field
added since Phase A.

### 5.2 How the Recommendations/Improvements page uses it

Read in `web/routes_arbiter.py` (the `_sort_key`) and the Improvements renderer:

- **Lead with altitude, then score within tier.** Sort key becomes
  `(−altitude, −existing_score)` for the Improvements surface — L2/L3 capability ideas lead;
  L0/L1 never bury an L2.
- **Fold all L0 into one collapsed "Maintenance" digest row** (the rework spec's Cleanup section,
  now driven by a deterministic field instead of dimension-guessing). One row: "12 maintenance
  items — model swaps, cost caps, permission cleanups" → expands on click. The operator sees
  *capability ideas first, maintenance folded*. This is the legibility payoff: 1 high-altitude
  card above a folded digest, not 138 flat items.
- Altitude is **deterministic and honest** — set by charter default (Fit Reviewer → L2;
  hygiene guardians → L0) or per-proposal override. No model decides it.

> Keeping this PR **spec-only** for the field (per the brief's "prefer spec-only"); §6 makes the
> schema its own bite so it can land + be ranked-on before the engine exists.

---

## 6. Falsifiable proof artifact (the acceptance test)

**The claim to falsify:** *a Fit Reviewer run on one real bot produces one high-altitude,
grounded, digestible capability proposal a human judges genuinely useful — and visibly better
than what `app_suggester` produces for the same bot.*

**Bot: `team-bot-a`** (richest real usage; clear purpose-aligned dominant domain; zero apps; a real
gallery app fits). With `personal-bot` as the **negative control**.

**"Genuinely useful + grounded" made checkable** — the run PASSES iff **all** hold:

1. **Exactly one** capability proposal for team-bot-a (not zero, not a flood).
2. `action` is `InstallApp` with `app_id` ∈ `packages/gallery/catalog.json` (expected:
   `p-f6a7b8c9` Project Tracker or `p-e5f6a7b8` Weekly Status Reporter).
3. **Every** `evidence[]` line reconciles: each number re-derives from team-bot-a's observation store
   (the poller's Gate A passes), and each transcript quote carries an attestable `session_id`.
   Strike any citation → the claim cannot stand (that's the test of "grounded").
4. `altitude == 2` and `value.tier ∈ {medium, high}` with a `basis` that is the literal trace of
   §3.6 Gate B (no free-floating number).
5. It renders as **one** Improvements card (`human_title`, ≤ one screen), not a wall.
6. **Human gate (named, not hand-waved):** the operator (pod-admin) reads it and answers yes to
   *"should team-bot-a have this, for the reason given?"* — i.e. it matches what team-bot-a is *for*
   (project management) and what the user *does* (records tasks, drafts status). This subjective gate is the
   point; the deterministic gates 1–5 exist to make it trustworthy.

**The contrast, captured in the same artifact:** run `app_suggester` against team-bot-a today. Result
(verified from `observe.py`): it is **silent** — no `app_suggester_gap` Signal exists, and
catalog-match confidence `0.6` is below the `0.85` ungrounded floor (`observe.py:53`). Even if it
fired, it would emit an `Investigation` citing a keyword-coverage gap with illustrative
`example_apps` that **map to no installable app**. Side by side: *menu-match → nothing
installable, no behavioral evidence* vs. *evidenced ideation → a real `pkg_id` + transcript-cited
reason*. The artifact records both outputs verbatim.

**Negative control (equally required):** run on `personal-bot`. Expected: **zero** suggestions, with a
`no_candidate` record whose reason is "no purpose-aligned noun cleared the support floor
(distinct_sessions ≥ 3, distinct_days ≥ 2)." Zero-on-personal-bot is a **PASS** — it proves the engine
declines to fabricate when the substrate is thin. (A reviewer that emits a suggestion for personal-bot
*fails*, regardless of how plausible it sounds.)

---

## 7. Phased build plan (~30-min bites, each with its own proof)

**Bite 1 — Targeting report, pure Python (THE FIRST BUILDABLE BITE). ✅ SHIPPED.**
Declare `team-bot-a.purpose` (`project-manager` + mission). Build `fit_review/targeting.py`: read
purpose + observation tuples + gallery catalog + installed apps → a `TargetingReport` (§3.3) with
the support floor and the gallery shortlist. **No LLM, no proposal.**
*Proof:* run on team-bot-a → report surfaces `task-management` (+ `document-generation`, `slack-comms`)
as purpose-aligned, above-floor, with `Project Tracker`/`Weekly Status Reporter` shortlisted. Run
on personal-bot → "no candidate cleared the floor." This bite alone proves the substrate is sufficient
and de-risks everything downstream — it's first precisely because it's the cheapest falsifiable
step and it surfaces the purpose prereq.

> **As built** (`packages/analyzer/fit_review/`): `archetypes.py` (per-archetype
> playbooks), `targeting.py` (pure I/O-free core `build_targeting_report` + the
> real-substrate wrapper `build_targeting_report_for_bot`), `cli.py` (`python3 -m
> fit_review.cli`). The purpose write contract is consolidated into
> `bot_purpose.set_bot_purpose` (shared by the admin API and the CLI). Two added
> brakes beyond §3.3: a declared-purpose requirement (inferred purpose frames but
> never anchors an emission, per §8) and a closed `META_NOUNS` set
> (`evolve-system`) excluded from candidacy — it reflects operating Evolve itself
> through the bot, not a user-facing need. **Live proof run (real pod data,
> read-only, 60-day window, purpose declared in-memory):** the team PM bot →
> `targets_found`, candidates `task-management, home-management,
> document-generation, slack-comms, email`, lead target **Weekly Status Reporter
> `p-e5f6a7b8`** (covers its top-3 confirmed needs), `Project Tracker
> `p-f6a7b8c9`` shortlisted; the personal bot → `no_candidate`, zero shortlist
> (PASS — declines to fabricate). The `app_suggester` contrast (silent, illustrative
> slugs) is unchanged from §2/§6. The persistent purpose anchor on the live pod is
> the operator's `PUT /api/bot/<id>/purpose` at rollout (only `ledger` is declared
> so far; the rest remain `null`).

**Bite 2 — `altitude` + `ValueEstimate` schema + ranking/fold.**
Add the fields (§5.1) with serialization + backward-compat; charter-level default; teach
`ranking.py` / `routes_arbiter.py` to sort `(−altitude, −score)` on Improvements and fold L0 into
one collapsed Maintenance row.
*Proof:* a synthetic L2 proposal sorts above existing L0 hygiene; the L0s collapse to one row in
both themes. (Independent of the engine — ships value immediately for the existing 138.)

**Bite 3 — In-bot reflection (`fit_review/runner.py`). ✅ SHIPPED.**
Assemble `ReviewContext`; one bounded, injected-callable LLM reflection (mirror
`user_profile_inferrer/extractor.py`); read targeted-domain transcripts within capture policy;
write a `fit_review_candidate` (0–1) to the outbox. DNT-aware exit.
*Proof:* dry-run on team-bot-a (stub LLM in tests; live model in the proof run) writes one cited
candidate JSON naming a real `pkg_id`; the prompt's one-suggestion + shortlist-only constraints hold.

> **As built** (`packages/analyzer/fit_review/` + `app_audit_runner` wiring):
> `reflection.py` (the one bounded, I/O-free LLM call — injected callable,
> mirrors `extractor.py`) and `runner.py` (bot-side orchestrator + CLI + weekly
> cadence wrapper) — the runner *emits* the candidate dict that Bite 4's
> `candidate.parse_candidate` reads (the contract field names match that reader's
> canonical aliases). The five structural brakes are realized in order in
> `run_fit_review_for_bot`:
> (1) **opt-out first** — privacy before any purpose/tuple read (the capture
> buffer's absence *is* the opt-out signal; `securityScanning=false` is the
> belt-and-suspenders check); (2) **targeting floor** — `targeting.py`'s decision
> must be `targets_found`, else **no LLM call** (asserted in tests via a recording
> stub: `llm.calls == []` on `no_purpose` / below-floor); (3) **one bounded
> reflection**; (4) **cite-or-don't + bounded action space** — enforced *in code*
> in `reflection.reflect`: every cited quote must be VERBATIM-present in the
> transcript it was given (a fabricated/paraphrased quote is dropped; an
> unattestable `session_id` is dropped; an off-shortlist `pkg_id` is rejected) and
> if nothing survives the candidate is **not written at all**; (5) **deterministic
> value + altitude** (Gate B) computed from the targeting support, never
> LLM-asserted.
>
> **The candidate contract (the shape Bite 4's `parse_candidate` reads)** —
> emitted by the runner, written ONLY for a grounded suggestion (a declined /
> gated run writes nothing to the outbox; the reason lands in the per-bot trail).
> Envelope fields `kind`/`record_id` wrap the payload (the reader ignores them —
> it derives `run_id`/`bot_id` from the path):
>
> ```jsonc
> {
>   "kind": "fit_review_candidate", "record_id": "fitrev-<hex>",   // envelope
>   "bot_id": "team-bot-a",
>   "archetype": "project-manager",          // the bot's DECLARED archetype | null
>   "recommended_need": "<prose: the recurring need>",
>   "suggested_gallery_pkg_id": "p-e5f6a7b8", // a REAL gallery pkg_id | null
>   "cited_evidence": [                        // verbatim, cite-or-don't (≥1)
>     {"quote": "<verbatim user words>", "session_id": "<id>", "ts": "<iso8601>"}
>   ],
>   "value_estimate": {"tier": "high"|"medium"|"low", "basis": "<trace>",
>                      "evidence_refs": ["<session_id>", ...]},  // == schema.ValueEstimate
>   "altitude": 2,                             // L2 capability (constant)
>   "targeting_decision": "targets_found",
>   "support": {"distinct_sessions": <int>, "days": <int>},      // from targeting
>   "run_id": "fitrev-run-<hex>", "created_at": "<iso8601>"
> }
> ```
>
> **Outbox location:** `{shared_dir}/fit_review/outbox/<run_id>/<bot_id>.json` —
> the shared tree, exactly where the merged Bite-4 poller drains it
> (`fit_review_poller._outbox_root`). The runner runs in-bot as the bot user and
> writes there directly — the same posture as the OC plugin's TurnObserver, which
> writes `{shared_dir}/<bot_id>/turns/...` in-bot — so no admin round-trip is
> needed; the `evolve` poller reads via plain reads (with a `sudo /bin/cat`
> fallback for fresh-deploy ACL lag) and never touches the raw transcript, only
> this structured output. (Atomic temp+rename; one file per emitting bot.) The
> bot-local cadence sentinel + decision trail stay in the bot workspace
> (`{workspace}/evolve/fit_review/`); only the integration artifact crosses to
> `{shared_dir}`. Bite 4's Gate A still **re-verifies** deterministically —
> `pkg_id` exists in `packages/gallery/catalog.json` and each cited quote is
> re-checked against the transcript — because the runner's verbatim check is the
> cheap first gate, not the last word.
>
> **Cadence + wiring (no new launchd job):** the pass rides the existing hourly
> bot-side Tier-3 audit tick via `app_audit_runner._maybe_run_fit_review` →
> `fit_review.runner.run_if_due`, gated to **weekly** by a per-bot sentinel
> (`{workspace}/evolve/fit_review/last_run.json`). It shares the audit lock (≤1
> in-bot LLM pass per bot at a time) and is fully isolated (a Fit Reviewer failure
> is logged and swallowed — it never aborts or reds an audit run). The
> `python3 -m fit_review.runner --bot-id <bot> [--force]` CLI is the manual /
> proof-run surface. The reflection model is the pod's resolved **standard** role
> (not Haiku, not the bot's possibly-power default — spec §3.4 + cost discipline).

**Bite 4 — Poller + Proposal (`fit_review_poller.py`). ✅ SHIPPED.**
Extend the `audit-scheduler` tick to ingest fit-review outboxes; run Gate A (cite-or-don't,
pkg-exists, count-reconcile); build the `InstallApp` Proposal (altitude=2, deterministic value,
coalesce_key) via `arbiter.store.write_proposal`.
*Proof:* team-bot-a's candidate lands as **one** Improvements card; the `app_suggester` contrast +
personal-bot negative control are captured — i.e. the §6 acceptance test runs end-to-end.

> **As built.** Pure deterministic gates live in `packages/analyzer/fit_review/`:
> `candidate.py` (tolerant parser — accepts BOTH the candidate-contract field
> names AND the spec §3.5 names as aliases, the single reconciliation point
> with Bite 3) and `gates.py` (`evaluate_candidate` runs the four gates;
> `compute_value_tier` is Gate B; `build_install_app_proposal` mints the
> `surface="improvement"`, `altitude=2` proposal). The admin-side I/O wrapper
> `packages/admin/evolve_admin/applications/fit_review_poller.py` reads
> `{shared_dir}/fit_review/outbox/<run_id>/<bot_id>.json`, wires the real readers
> (gallery catalog, a recomputed `build_targeting_report_for_bot` for
> support-reconcile, `investigation.operator_already_declined` for cooldown), and
> archives ingested candidates — drained from the hourly `audit-scheduler` tick
> (Phase 0d), no new daemon. **Gate A (cite-or-don't) re-verifies each quote
> against the bot's transcript** ("verify, don't trust", §3.6) via an injected
> verifier; the default does a normalized substring re-read of the bot's turn
> store and degrades to the bot's session attestation only when that store is
> entirely unreadable — a deterministic check, never centralized inference. A
> benign near-miss (need thinned / now covered) surfaces a calmer informational
> Observation rather than vanishing; fabricated quotes and cooldowns drop
> silently. The emitted proposal is asserted to pass
> `proposal_routing.proposal_surfaces_in_pending_inbox` (it reaches the
> actionable Inbox via the altitude carve-out merged in #3157). `app_suggester`
> is untouched (the menu-match fallback; coexistence per §2/§6).

**Bite 5 — Soft verification (out of v1 critical path).**
Adoption (accepted vs. dismissed) + usage-shift (did the targeted noun's friction drop / did the
installed app get used) over weeks → reviewer authority. The honest, slow loop (Effectiveness-Layer
§9). Spec'd, not built in v1.

**Phase 2 — Forge (`BuildApp`).** Per §4's trigger: trusted track record + an evidenced gap the
gallery can't fill.

---

## 8. Open questions / risks (esp. the 138 failure mode)

- **How this design cannot become the 138 — the five structural brakes:** (1) **purpose-anchored**
  — no declared purpose ⇒ no capability pass; (2) **targeting floor** — no above-floor
  purpose-aligned noun ⇒ no LLM call ⇒ no proposal (the cheap gate that makes "review team-bot-a" and
  "review personal-bot" diverge correctly); (3) **cite-or-don't** — Gate A drops anything whose numbers
  don't reconcile or whose `pkg_id` is invented; (4) **bounded action space** — the LLM chooses
  among 14 real apps, never free-associates; (5) **one-per-run cap + altitude fold** — even a
  stray L0 can't drown the L2. The 138 had *none* of these; it was an unbounded miner with no
  anchor and no citation. The Fit Reviewer is the inverse on every axis.
- **Thin-substrate fabrication (the personal-bot risk).** *Mitigation:* the targeting floor is a
  pre-LLM gate, and zero-is-a-valid-answer is enforced (a `suggest` with no above-floor candidate
  is rejected by the poller). The negative control makes this a tested property, not a hope.
- **Purpose anchors are unset on every real bot today.** *Mitigation:* Bite 1 declares team-bot-a's;
  the wizard already captures purpose at creation for new bots. Inferred-purpose may *frame
  targeting* but must not *anchor a shipped capability proposal* (confidence < 1.0 ⇒ no capability
  emission) — otherwise we've reintroduced vacuum mining through the back door.
- **Gallery is only 14 apps.** Many real needs won't have a vetted match. *Mitigation:* that's a
  feature in v1 — return zero rather than force a bad match; it's also the concrete Phase-2 Forge
  trigger (§4).
- **LLM invents a `pkg_id` / inflates a count.** *Mitigation:* Gate A is deterministic and
  poller-side; the model's numbers are re-derived, not trusted.
- **Cost creep.** *Mitigation:* one call per above-floor bot per month; personal-bot-class bots cost
  ~zero; input capped; output ≤ ~600 tokens. Cheap-by-default preserved; this is the *one*
  sanctioned escalation.
- **`engagement` is dead (uniformly 1).** *Mitigation:* rank on `distinct_sessions` ×
  `distinct_days` × `frustration_share`, never engagement. (Flagged for the observation layer to
  fix independently; the Fit Reviewer doesn't wait on it.)
- **Cadence weekly vs monthly.** *Recommend monthly default, activity-scaled, + manual "review
  now".* Cheap enough to revisit once we see real adoption.
- **Same runner as App Audit, or sibling?** *Recommend a sibling pass in the same bot-side
  scheduler*, reusing the outbox/poller plumbing — different cadence and failure mode from audit,
  but identical transport.

---

## 9. Fit with existing principles

- **Per-bot inference, never centralized** — runs in-bot on the bot account; poller reads
  structured output only. ✓ ([principle-per-bot-inference.md](principle-per-bot-inference.md))
- **RSI must be cheap; LLM is escalation** — pure-Python targeting gates a single bounded
  reflection. ✓
- **Cite-or-don't-recommend** — Gate A, deterministic, poller-side. ✓
- **Apps inherit the bot's LLM** — the reflection uses the bot's own creds.
  ✓ ([principle-apps-inherit-bot-llm.md](principle-apps-inherit-bot-llm.md))
- **User-observation opt-out (DNT)** — honored from v1. ✓
- **Product defaults ship in code** — altitude/value fields + the Improvements fold are code, not
  per-pod proposals. ✓

---

**The one-line version (again):** the maintenance crew (L0/L1) keeps the bot running; the Fit
Reviewer is the chief of staff who, once a month, reads what the bot is *for* against what its
user actually *did*, points at the one capability the evidence demands, and proves it with the
user's own words — or stays quiet.
