# UI Audit — Instruction-text & infographic editorial sweep (Track B)

**Date:** 2026-06-13 · **Auditor:** META:ui Track B (read-only) · **Scope:** every "How it
works" card, explainer banner, `<details>` explainer, `help-btn` tooltip, and infographic in
`packages/admin/evolve_admin/web/index.html` + JS renderers. Judged against the operator's four
tests: **CLEAR · SUCCINCT · ACCURATE · NECESSARY/HELPFUL.**

This is an audit deliverable. No `index.html` / `base.css` / `static/js/*` was edited. Findings
are classified **CLEAR-FIX** (→ `ui` mechanical fix), **ACCURACY-FORK** (→ route to owning page
aspect), **CUT-CANDIDATE** (→ remove), **KEEP** (passes all four).

---

## Executive summary

**Overall the instruction text is good — better than the brief feared.** The `help-btn` tooltips
(75 of them) are the strongest surface in the app: specific, accurate, they cite the storage
location and the default value. The 4 Reports `explainer-banner`s are tight and correct. The
Users "how identity works", Backup "tradeoffs", and AI-pod "How Evolve models work" explainers
all pass cleanly. The "How Sessions Are Routed" flowchart is **data-driven from real config** and
accurate; so is the "auto for config; you for instructions" claim and the `evo security` NL
mapping — all three spot-checks the brief flagged as suspect came back **verified correct.**

The problems are concentrated and fixable. Five things matter:

1. **Emoji as functional step-icons in the two `.si-pipeline` infographics** (Recommendations
   🔍👤⚙️📊, Forge 📋🛠️🔍🧪🚀) — a clean **§9.12 violation**. This is the headline infographic
   finding. → CLEAR-FIX (emoji→`currentColor` SVG).
2. **Getting-Started names a coach that does not exist** — "Adjacency Explorer" is not a
   registered generator (the real capabilities coaches are `app_suggester` /
   `pod_capability_lift` / `engagement_amplifier`). → ACCURACY-FORK (rsi).
3. **"Security warden … Writes to Maintenance"** (Getting-Started) is **stale** — warden findings
   land in **Reports → Alerts** via the Signal store now; the Maintenance page is gateway/cron/
   logs. → ACCURACY-FORK (reports).
4. **Redundancy between Getting-Started subtabs and the page-level cards** — the Recommendations
   workflow is explained three times and Continuity twice; the duplicates have already drifted
   (they carry inaccuracies #2/#3 the page cards don't). → CUT-CANDIDATE / consolidate.
5. **Vocabulary drift for one concept** — "Better Engine" / "coaches" / "generators" /
   "Recommendations" / "Self-Improvement" all name the same machinery, sometimes in adjacent
   sentences ("Better Engine watches" subtitle over a card that says "a coach notices"). The
   friendly "coaches" label even breaks down at the chip level, where **"Active coaches" renders
   raw `snake_case` generator IDs.** → CLEAR-FIX (ui standardize) + rsi (canonical term + chip
   label map).

**Headline emoji/infographic verdict:** two infographics violate §9.12; everything else
(routing flowchart, pod-model ASCII) is text-based, accurate, and worth its space — keep.
**Headline redundancy verdict:** Getting-Started has become a second, staler copy of the page
explainers. Trim its overlapping subtabs to pointers; let the page cards be the source of truth.

---

## Ranked findings table

| # | Surface | file:line | Tests failed | Sev | Class | Route/Action |
|---|---------|-----------|--------------|-----|-------|--------------|
| 1 | Recommendations `.si-pipeline` step-icons (🔍👤⚙️📊) | [index.html:1774](packages/admin/evolve_admin/web/index.html#L1774) | ACCURATE(§9.12) | P1 | CLEAR-FIX | emoji→SVG |
| 2 | Forge `.si-pipeline` step-icons (📋🛠️🔍🧪🚀) | [index.html:1487](packages/admin/evolve_admin/web/index.html#L1487) | ACCURATE(§9.12) | P1 | CLEAR-FIX | emoji→SVG |
| 3 | Getting-Started "Who writes" — **"Adjacency Explorer"** phantom coach | [index.html:6100](packages/admin/evolve_admin/web/index.html#L6100) | ACCURATE | P1 | ACCURACY-FORK | **rsi** |
| 4 | Getting-Started background — **"Security warden … Writes to Maintenance"** | [index.html:6173](packages/admin/evolve_admin/web/index.html#L6173) | ACCURATE | P1 | ACCURACY-FORK | **reports** |
| 5 | Getting-Started → Recommendations subtab (dup of page card) | [index.html:6085](packages/admin/evolve_admin/web/index.html#L6085) | NECESSARY | P1 | CUT-CANDIDATE | trim→pointer |
| 6 | Vocabulary drift: Better Engine / coaches / generators / Recommendations | [index.html:1749](packages/admin/evolve_admin/web/index.html#L1749) | CLEAR | P1 | CLEAR-FIX | ui (+rsi term) |
| 7 | "Active coaches" chips render raw `snake_case` IDs | [self-improvement.js:316](packages/admin/evolve_admin/web/static/js/pages/self-improvement.js#L316) | CLEAR | P2 | ACCURACY-FORK | **rsi** (label map) |
| 8 | Forge how-it-works prose (6 sentences) | [index.html:1476](packages/admin/evolve_admin/web/index.html#L1476) | SUCCINCT | P2 | CLEAR-FIX | tighten |
| 9 | Getting-Started → Continuity subtab vs Background card (dup) | [index.html:6106](packages/admin/evolve_admin/web/index.html#L6106) | NECESSARY | P2 | CUT-CANDIDATE | dedup |
| 10 | Tooltips use legacy "tier0/1/2/3" vs role names (Fast/Standard/…) | [index.html:2260](packages/admin/evolve_admin/web/index.html#L2260) | ACCURATE/CLEAR | P2 | ACCURACY-FORK | **model-tiers** |
| 11 | Alert channel tooltip "only telegram" vs Subscriptions "Slack/Discord" | [index.html:4329](packages/admin/evolve_admin/web/index.html#L4329) | ACCURATE | P2 | ACCURACY-FORK | **reports** |
| 12 | "7-day check-in" stated universal; `window_days` varies (mostly 7) | [index.html:1770](packages/admin/evolve_admin/web/index.html#L1770) | ACCURATE | P3 | ACCURACY-FORK | **rsi** (minor) |
| 13 | In-content buttons use Unicode/emoji glyphs (↻ ✓ ✗ ＋ ⚠ ✦ ⟳) | many | ACCURATE(§9.12) | P3 | CLEAR-FIX | sized follow-on |
| 14 | Bot-config "Direct host edits: `sudo -u <bot> openclaw…`" CLI hint in web UI | [index.html:2482](packages/admin/evolve_admin/web/index.html#L2482) | CLEAR | P3 | KEEP/note | see [[feedback_no_sudo_cli_hints_in_web_ui]] |

**Passes-all-four (KEEP, not itemized):** the 4 Reports `explainer-banner`s (Subscriptions /
Alerts / Proposals / Watchlist); the bulk of the 75 `help-btn` tooltips; Users "how identity
works"; Backup "Cloud vs. local" tradeoffs; AI-pod "How Evolve models work"; "How Sessions Are
Routed" flowchart; "The pod model" 3-layer ASCII; Apps "How testing works"; the app-wizard
3-step card.

---

## Concrete rewrites — top CLEAR-FIX offenders (make the fix bite mechanical)

### A. Emoji → SVG step-icons (findings #1, #2) — §9.12

Both `.si-pipeline`s use emoji as functional icons. §9.12: *"Don't use emoji as functional icons
(they render differently per OS, don't theme, can't be sized consistently)."* Replace each
`<div class="si-step-icon">📋</div>` with a 16–18px `stroke="currentColor"` SVG (same idiom as
`.expand-icon` / the Phase-4 sidebar icons), and add one CSS rule so they size/theme:

```css
/* base.css, near .si-step-icon */
.si-step-icon svg { width: 18px; height: 18px; stroke: currentColor; fill: none;
                    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
```

Suggested Lucide-style glyphs (drop-in `<svg viewBox="0 0 24 24">…</svg>` bodies):

| Step | Emoji today | Icon | SVG polyline/path body |
|---|---|---|---|
| Recommendations · Spot | 🔍 | search | `<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>` |
| Recommendations · You decide | 👤 | user | `<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>` |
| Recommendations · Apply | ⚙️ | sliders | `<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>` |
| Recommendations · Check-in | 📊 | activity | `<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>` |
| Forge · Manifest | 📋 | file-text | `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/>` |
| Forge · Build | 🛠️ | tool | `<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>` |
| Forge · Critique | 🔍 | eye | `<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>` |
| Forge · Test | 🧪 | check-circle | `<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>` |
| Forge · Ship | 🚀 | upload | `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>` |

Add `aria-hidden="true"` on the `.si-step-icon` (the `.si-step-label` carries the accessible
name). No copy change — labels/subs stay.

### B. Forge how-it-works prose (finding #8) — tighten 6 sentences → 3

> **Now** ([index.html:1476](packages/admin/evolve_admin/web/index.html#L1476)): *"The forge
> turns an app idea into a running app. Every app starts with a manifest — its spec, file list,
> and tests — that comes from a Gallery template, an Evo chat, or an RSI proposal. The bot drafts
> the code (using its own LLM), critiques its own work over a few rounds, runs the app's tests,
> then either you approve it or — when the request was already a deliberate conversation — it
> ships automatically. The manifest is updated at the end so it always reflects what's on disk."*

> **Proposed:** *"The forge turns an app idea into a running app. Every app starts from a
> **manifest** — its spec, files, and tests (from a Gallery template, an Evo chat, or an RSI
> proposal). The bot drafts the code, self-critiques over a few rounds, runs the tests, then ships
> — on your approval, or automatically when the request was already a deliberate conversation. The
> manifest is refreshed at the end so it always matches what's on disk."*

### C. Recommendations subtitle / vocabulary (finding #6)

The page subtitle says the **Better Engine** watches; the card directly below says a **coach**
notices; the data layer calls them **generators**; the subtab is **Coaches**; the page is
**Recommendations**; the nav/RSI name is **Self-Improvement**. Pick one operator-facing metaphor
("coaches" is the friendliest and already dominant in the UI) and make the engine the thing the
coaches belong to.

> **Now** ([index.html:1749](packages/admin/evolve_admin/web/index.html#L1749)): *"Better Engine
> watches the pod and surfaces recommendations. You decide which ones land."*

> **Proposed:** *"Coaches watch the pod and surface recommendations. You decide which ones land."*
> (and use "coaches" — not "Better Engine" — in the Getting-Started copy at
> [6089](packages/admin/evolve_admin/web/index.html#L6089), so the page and the onboarding agree).

*Note: "Better Engine" is fine as the proper name of the subsystem in the CLI (`evo better`) — the
ask is to stop alternating it with "coach" inside the same operator-facing sentence/section.*

### D. Getting-Started → Recommendations subtab (finding #5) — cut to a pointer

The subtab ([6085–6103](packages/admin/evolve_admin/web/index.html#L6085)) restates the page
how-it-works card almost verbatim **and** carries the phantom-coach inaccuracy (#3). A
getting-started page should orient and link, not maintain a parallel (staler) explanation.

> **Proposed (replace both cards with one):** *"**Recommendations** are improvements the coaches
> spot — a cost spike, a capability gap, a security finding. You approve, snooze, or dismiss each
> one; approved config changes apply automatically and get a check-in to confirm they helped. See
> the **[Recommendations](#)** page for the live queue and the full pipeline, or type `evo` to any
> bot to act on the top one from chat."*

This deletes the "Who writes the recommendations" card (whose generator list is the thing that
drifted) and points at the page card, which stays the single source of truth.

### E. Getting-Started → Continuity (finding #9) — dedup

The same page explains the Continuity Engine twice: the dedicated subtab
([6106](packages/admin/evolve_admin/web/index.html#L6106)) and the Background "Four features"
card ([6160](packages/admin/evolve_admin/web/index.html#L6160)). Keep the subtab (it has the
`tasks list` / `approve` CLI), and in the Background card replace the Continuity tile's body with
a one-liner + link to the subtab rather than a second full explanation.

---

## Proposed bite plan (dispatchable, grouped)

**Bite B3a — Emoji→SVG step-icons (ui, mechanical).** Findings #1, #2. Replace the 9 emoji in the
two `.si-pipeline`s with the `currentColor` SVGs above + the `.si-step-icon svg` CSS rule + an
`aria-hidden`. Touches `index.html` + `base.css`. Self-contained; lint-clean; theme-test both
modes. *Closes the §9.12 step-icon violation and the style-guide §14 #17 note about the pipeline
diagrams.*

**Bite B3b — Getting-Started dedup + de-stale (ui drives; rsi + reports sign off content).**
Findings #3, #4, #5, #9. Trim the Recommendations subtab to a pointer (rewrite D), remove the
"Adjacency Explorer" line and "Writes to Maintenance" line, dedup Continuity (rewrite E). The two
copy *deletions* are pure ui; the two *replacements* need a one-line confirm from rsi (correct
capabilities-coach name) and reports (correct warden destination) — fold their answers in.

**Bite B3c — Vocabulary + prose tightening (ui).** Findings #6, #8. Standardize on "coaches" for
the operator-facing metaphor across the Recommendations subtitle + Getting-Started; tighten the
Forge prose (rewrite B). Pure copy; no behavior.

**Bite B3d — Tooltip & glyph sweep (sized follow-on; ui + model-tiers + reports).** Findings #10,
#11, #13, #7. (a) Retire legacy "tier0/1/2/3" wording in the ~3 classifier/engine tooltips →
role names, with model-tiers confirming the mapping. (b) Reconcile the alert-channel
"only telegram" tooltip against the Subscriptions "Slack/Discord" copy with reports. (c) Humanize
the "Active coaches" chips (rsi supplies an id→label map). (d) The broad in-content
Unicode/emoji-glyph→SVG migration (↻ ✓ ✗ ＋ ⚠ ✦) is a *larger* sweep — **size it separately**,
don't bundle it here; the sidebar already migrated in Phase 4, in-content buttons are the
remaining surface.

---

## Accuracy-routing list (for the coordinator to hand off)

| Finding | Suspected inaccuracy | Verified? | Owning aspect |
|---|---|---|---|
| #3 | "Adjacency Explorer" is not a registered generator; real capabilities coaches are `app_suggester` / `pod_capability_lift` / `engagement_amplifier` (`user_adjacency` is `user_profile_inferrer`'s *dimension*, not a coach) | **Confirmed wrong** (full generator dir reviewed) | **rsi** |
| #4 | "Security warden writes to Maintenance" — warden emits Signals → **Reports → Alerts**; Maintenance is gateway/cron/logs | **Confirmed stale** (Maintenance page is [index.html:2594](packages/admin/evolve_admin/web/index.html#L2594), subtitle "gateway status, cron jobs, infrastructure, logs") | **reports** |
| #7 | "Active coaches" chips show raw `snake_case` IDs while everything else says "coaches" | **Confirmed** ([self-improvement.js:316](packages/admin/evolve_admin/web/static/js/pages/self-improvement.js#L316) renders `g.id`) | **rsi** (id→label map) |
| #10 | Tooltips say "tier0/1/2/3"; the model UI retired numbered tiers for roles (Fast/Standard/Power/Max/Judge) | Confirmed vocab mismatch ([ai-optimization.js:641](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L641) `TIER_DISPLAY` maps the legacy keys to roles) | **model-tiers** |
| #11 | Alert-channel tooltip "Currently only telegram is implemented" vs Subscriptions banner "Telegram, Slack, Discord" | Plausible context split (alert *config* vs bot *chat thread*) — reads contradictory to an operator | **reports** |
| #12 | "Anything with a measurable claim gets a 7-day check-in" — `Claim.window_days` is per-claim (5×7, 1×14, 1×1); 7 is representative, not universal | Confirmed nuance ([schema/proposal.py:1056](packages/analyzer/schema/proposal.py#L1056) required field, no default) | **rsi** (minor — soften to "≈7-day") |
| — | "auto for config; you for instructions" (Recommendations card) | **Verified ACCURATE** (Investigation/WorkflowInstruction tooltip confirms manual; config auto-applies) | none |
| — | "How Sessions Are Routed" flowchart matches routing logic | **Verified ACCURATE** (`_aiRoutingText` reads real `models.routing`/`tiers`, [ai-optimization.js:601](packages/admin/evolve_admin/web/static/js/pages/ai-optimization.js#L601)) | none |
| — | "what can my bot do?" → `evo security` | **Verified ACCURATE** ([subcommands.py:806](packages/admin/evolve_admin/evo/subcommands.py#L806)) | none |

---

## Cross-references (Track A overlap, not Track B's to fix)

- The Apps testing, Users identity, and AI-pod "How Evolve models work" explainers use bespoke
  inline-styled `<details>` rather than the `.collapsible-card` primitive (§9.14). Content is
  fine; the *mechanism* is B2 (collapse-affordance canonicalization), not this editorial sweep.
- §14 #17 (content-width caps on the how-it-works cards) is still open because their bodies mix
  prose + `.si-pipeline` + badge rows. Bite B3a doesn't change that, but once the pipelines are
  SVG the per-prose-element `.card-prose` cap becomes cleaner to apply.
