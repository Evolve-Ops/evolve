# spec: recommendations + alerts rework — 2026-06-02

> Follow-on: the Phase 1 coalesce/humanize mechanics introduced here
> (`coalesce_key` / `human_title` / `sub_findings`) are generalized into a
> named, generator-facing standard in
> [docs/design-recommendation-legibility-2026-06-12.md](design-recommendation-legibility-2026-06-12.md)
> (piloted on `model_discovery`).

Status: Phase 1 shipped (2026-06-02/03). Phase 2 reverted and
re-planned 2026-06-04 — see §"Course correction 2026-06-04" below.
The original Phase 2 design (Health page + Improvements page +
additive sidebar) shipped and was reverted in
[#2045](https://github.com/evolve-ops/evolve/pull/2045) after operator
feedback that it made the noise worse, not better.

## Course correction 2026-06-04

The first cut of Phase 2 took an additive shape: new Health
sidebar item + new Improvements sidebar item, alongside the
existing Maintenance / Reports → Alerts / Recommendations surfaces.
The operator observation:

> Currently, I now see THREE lists of issues. We have the Health
> page, the Reports/Alerts tab, and the Recommendations Suggestion
> queue — all of which have similar items. This has made the
> problem worse not better.
> ...
> The Health page seems less capable than the Alerts tab. The
> Alerts tab has useful buttons for filtering the alerts by
> producer, bot, and severity. Health does not.

The additive Phase 2 added a third list instead of consolidating
two existing ones. The §"Three buckets" / §"Health page" / §"UI
shape" / §"Generator routing" sections below describe the
**reverted** plan — they remain in the doc for archaeology only.
Don't act on them.

### Corrected model — three existing surfaces, content boundaries enforced

| Surface | What lives here | What does NOT |
|---|---|---|
| **Recommendations** | High-level, app-side proposals — what the bot DOES (not the sysadmin piping). Sparse and opinionated; the natural home for the Phase 3 app-usage advisor. | Sysadmin / config / cleanup hygiene |
| **Subscriptions** | Opt-in reports with clearly explained criteria, adjustable thresholds, frequency, digest options. The place that says "hey, this happened." | Triage queue; one-off operator decisions |
| **Alerts** | All sysadmin / config / cleanup / sysmessage findings. With filters (bot / producer / severity) and better sorting. The single "what needs attention" pane. | High-level proposals; subscription thresholds |

### Threshold-config breakthrough

The "Configure" tab currently under Alerts is wired to
**subscription** thresholds, not alert thresholds — it tunes which
Signals are dispatched to which channels, at what frequency. That
tab is misplaced. It moves under Subscriptions as "Thresholds" (the
existing Subscriptions "Configure" tab handles the orthogonal
subscribe/unsubscribe action).

### What stays from the Phase 1 work

Phase 1 PRs already merged and still useful under the corrected
model:

- [#2006](https://github.com/evolve-ops/evolve/pull/2006) — coalesce
  proposals across bots. Still good; collapses per-bot fan-out
  wherever proposals render.
- [#2006](https://github.com/evolve-ops/evolve/pull/2006) +
  [#2013](https://github.com/evolve-ops/evolve/pull/2013) /
  [#2016](https://github.com/evolve-ops/evolve/pull/2016) — humanized
  titles + on-disk rewrite script. Plain English is right in any
  list.
- [#2011](https://github.com/evolve-ops/evolve/pull/2011) +
  [#2038](https://github.com/evolve-ops/evolve/pull/2038) — `surface`
  field on Charter. The data is right; only the consuming UI
  routing changed. New semantics: `surface=improvement` →
  Recommendations; `surface=firing|drift|cleanup` → Alerts.
- [#2039](https://github.com/evolve-ops/evolve/pull/2039) — Phase 3
  spec for the app-usage advisor. The generator still emits
  `surface=improvement` and still populates Recommendations,
  just without the Improvements-page detour.

### Revised Phase 2 plan

Three independently-shippable PRs, in order:

- **Phase 2.v2-A — Route proposals by `surface`.** Filter the
  Recommendations Inbox to show only `surface=improvement`. Render
  proposals with any other surface — including `null/unclassified`
  — on the Alerts page alongside the existing Signals. The null
  catchall lands on Alerts (Slice 1B flip 2026-06-04) because the
  motivating case was `audit_poller`'s `app_audit_tier3` findings:
  they emit without a charter and therefore carry `surface=null`,
  but they're broken-install / forge-emission hygiene, not
  high-level recommendations. Same `/api/arbiter/proposals` data
  source; client-side routing. The operator gets the existing
  Alerts page's filter affordances "for free" on these proposals.
- **Phase 2.v2-B — Move the Alerts "Configure" tab to Subscriptions
  as "Thresholds."** Mechanical relocation of the DOM + the matching
  page-router wiring. Existing Subscriptions "Configure" subtab
  stays; this becomes its sibling.
- **Phase 2.v2-C — Improve Alerts filtering + sorting.** Bot /
  producer / severity filters get an "include proposals" toggle (or
  default on). Sort order tightens up so the loudest items don't get
  buried by the chattiest producer.

Phase 3 (app-usage advisor) plans unchanged; its output now lands on
Recommendations directly, no Improvements page involved.

---

## (Reverted plan — kept for archaeology, do not act on)

## Background

On 2026-06-02 the test pod was carrying 41 open Proposals and ~30 open
Alerts. The two lists overlap in shape so heavily that the user
flagged them as "undifferentiated checklists." Concrete failure modes
observed:

1. **Root-cause fan-out.** One missing GitHub PAT produced 8 backup
   Signals. One auth-profiles migration not run produced 9 legacy-key
   Signals. One weak-tier default produced 9 Signals. One empty
   `primary` field produced 7 Proposals. The 41+30 items collapse to
   ~15 distinct issues.
2. **Install hygiene masquerading as RSI.** Most current Proposals
   are install-state findings (PAT missing, FileVault off, legacy
   creds, baselines unseeded, fingerprints mismatched, primary='').
   They are not improvements; they are "this install isn't finished."
3. **Jargon leakage.** Proposal titles read in generator-id terms
   ("primary_model_floor_advisor", "envelope growth", "manifest
   drift"). The Plex-test user has no way to parse "primary floor."
4. **The actual product-recommendation slot is empty.** No generator
   reads how an app is being used and proposes features. The user
   wanted "task-manager is used 47×/week for date setting — consider
   recurring tasks." Today's portfolio only reads config files and
   baselines.

The fix is structural: separate Health-shaped findings (sysadmin
hygiene, drift, install posture) from Improvement-shaped findings
(app-usage product suggestions), and coalesce by root cause so the
operator sees ~15 rows instead of 71.

## Principle

> Findings cluster on a 2×2 of **how it surfaces** (firing event vs.
> slow drift vs. optional cleanup) and **what shape the action takes**
> (operational fix vs. product evolution). Today both axes are
> collapsed into one "Proposals" list. The rework splits them.

## Three buckets

| Bucket | Sidebar location | What goes here | Approx. volume on test pod today |
|---|---|---|---|
| **Health** | Operate → Health (renamed from Alerts) | Install hygiene, drift, safety findings, ongoing operational health | ~50 raw findings → ~15 coalesced rows |
| **Improvements** | Improve → Improvements (rebuilt from Proposals) | App-shaped product suggestions, written from observed usage | 0 today; target 1–2/app/week |
| **Cleanup** | Footer section of Health, collapsed by default | Low-stakes hygiene: stale exec entries, network_egress audit metadata, baseline reblesses | ~10 |

Health is the operator's "what needs attention" pane. Improvements is
the optimistic, infrequent "here's what would make your bots better"
pane. Cleanup is the dustbin for things that *technically* are
findings but aren't worth a row of the operator's eyeballs.

### Why not keep Proposals as a separate page

The user's observation is correct: today's Proposals page is
indistinguishable from Alerts. The only honest difference is that
Proposals carry a suggested action object (ConfigPatch, UpdatePluginAllowDeny,
UpsertCronJob, Investigation). But "has an action" is not a useful
axis to split a UI by — both pages are still "list of things to do."
The useful axis is *what kind of thinking does the operator have to
do*: triage a fire (Health) vs. evaluate a product idea (Improvements).

## UI shape

### Health page

One page, three segmented sections:

```
┌─ Health ─────────────────────────────────────────────────┐
│ [ Firing 4 ] [ Drift 9 ] [ Cleanup 12 ▾ ]                │
├──────────────────────────────────────────────────────────┤
│ FIRING                                                   │
│  🔴 FileVault is off (pod-wide)                          │
│  🔴 evolve .zshrc hash changed since baseline            │
│  🔴 personal-bot EMAIL_POLICY.md is writable             │
│  ⚠  2 bots: backup failing 6× in a row · security-bot,  │
│        bot-b                                             │
├──────────────────────────────────────────────────────────┤
│ DRIFT                                                    │
│  GitHub PAT not configured · affects 8 bots' backups     │
│  9 bots: weak model tiers in fallbacks                   │
│  9 bots: legacy credential key shape                     │
│  8 bots: running older Evolve version than admin server  │
│  4 generators failed to load (fingerprint mismatch)      │
│  bot-a: script inventory drift (-3 missing)              │
│  ea-pack: 5 audit findings on this app                   │
│  task-manager: 5 audit findings on this app              │
│  team-bot-a: prompt cache invalidation 39%               │
├──────────────────────────────────────────────────────────┤
│ CLEANUP (▾ click to expand)                              │
│  · 8 network_egress entries to add on personal-bot       │
│  · 2 stale exec entries on i-3f53d00c                    │
│  · 2 bots: adopt expected plugins.allow                  │
│  · accept new cron jobs into baseline (2 bots)           │
└──────────────────────────────────────────────────────────┘
```

Section semantics:

- **Firing** — actively-broken state right now. CRITICAL audit
  findings, failing backups, gateway down, generator-load failures
  that disable other checks. Sorted by severity, then by recency.
- **Drift** — install hygiene + slow drift. Coalesced by root cause:
  one row per root cause, with "affects N bots" subscript when
  multiple bots share it. Click expands to per-bot list. Sorted by
  blast radius (bots affected × severity).
- **Cleanup** — low-stakes housekeeping. Collapsed by default; one
  row per category with a count. Operator can bulk-accept or ignore.

### Improvements page

Sparse, opinionated, app-focused. Each row is a product suggestion
for a specific app, sourced from a new generator class (see
spec-app-usage-generator, to be written separately).

```
┌─ Improvements ───────────────────────────────────────────┐
│                                                          │
│  task-manager · bot-a                                    │
│  Used 47× this week, mostly to set due dates from Slack. │
│  Consider: recurring tasks, snooze, daily digest.        │
│  [Read full proposal] [Snooze] [Dismiss]                 │
│                                                          │
│  ea-pack · bot-a                                         │
│  Morning brief sent 6/6 days; evening sweep skipped 4/6. │
│  Possible cause: evening cron failed silently. Investigate.│
│  [Read full proposal] [Snooze] [Dismiss]                 │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Empty state until the new generator ships: "Improvements appear here
when Evolve has watched an app long enough to suggest changes. No
suggestions yet — check back after a few days of usage."

### Sidebar

Per current IA (Operate / Improve / Settings, see memory entry
`project_evolve_three_bucket_ia`):

- **Operate** — Overview, Bots, **Health** (new name for Alerts),
  Cost, Usage, Channels, Credentials, Backup, Maintenance, Security
- **Improve** — **Improvements** (rebuilt), App Library, ...
- **Settings** — Models, Network, ...

The Alerts → Health rename is mechanical. The Proposals page is
*replaced* by Improvements; existing Proposals route into Health or
Cleanup based on generator (see next section).

## Generator routing

Every existing generator gets a `surface` field on its charter,
pinning it to Health-Firing, Health-Drift, Health-Cleanup, or
Improvements. The page reads `surface` instead of inferring from
status/severity.

| Generator | Today | Routes to | Notes |
|---|---|---|---|
| `workspace_security` | proposal · security_critical | Health-Firing | "credential in workspace file" is a fire |
| `app_audit_tier3` | proposal · operational_urgent | Health-Drift (per app) | Coalesce by app, not per-finding |
| `primary_model_floor_advisor` | proposal · improvement | **Bug — fix sensor** | primary='' is a parse failure, not advice. Generator should detect this and surface as Health-Firing "X bot has no primary model" |
| `plugin_curator` | proposal · improvement | Health-Cleanup | "no allowlist" is hygiene, not improvement |
| `app_permission_review` (network_egress add) | proposal · hygiene | Health-Cleanup | Audit-only, no runtime enforcement |
| `app_permission_review` (stale exec) | proposal · improvement | Health-Cleanup | Stale entries |
| `cost_spike` | proposal · improvement | Health-Drift | Operational cost issue |
| `bloat_investigator` | proposal · improvement | Health-Drift | Operational |
| `cron_caps_filler` | proposal · hygiene | Health-Cleanup | Add caps to existing cron |
| `cost_watchdog` (Signal source) | alert | Health-Drift (or Firing if breaker tripped) | Already on Alerts; absorb as-is |
| `audit` (Signal source) | alert | Health-Firing (CRITICAL) / Health-Drift (warn) | Severity decides section |
| `backup_signal` | alert | Health-Drift (PAT missing) / Health-Firing (failing N× in a row) | |
| `install_integrity_monitor` | alert | Health-Drift | Legacy creds, channel token handshake |
| `deploy_drift_monitor` | alert | Health-Drift | Out-of-sync versions |
| `registry` (generator failed to load) | alert | Health-Firing | Disables other checks — fire |
| `monitor_coverage` | alert | Health-Firing | A silent monitor is a fire |
| `compliance_scan` | alert | Health-Drift | |
| `pod_report` | alert | Health-Firing (rollup) | Already a rollup; surfaces summary |
| `sysadmin_watchdog` | alert | Health-Firing | |
| `integration_probe` | alert | Health-Drift | |
| `session_economics` | alert | Health-Drift | Cache invalidation patterns |
| *(new)* `app_usage_advisor` | n/a | Improvements | Spec separately |

### Coalescing rules

A coalesced row groups N raw findings into one display row when:

1. They share the same `signature_key` (generator-specific stable
   identifier of the root cause; e.g.,
   `backup_signal:no_pat:{repo_visibility}` produces one signature
   across all 8 bots).
2. They differ only in `bot_id`.

The row shows the root-cause title + "affects N bots" subscript;
clicking expands to the per-bot list with each bot's specific detail
(file path, last-attempt timestamp, etc.). Bulk actions (Snooze all,
Dismiss all, Resolve all) operate on the group.

Signatures are declared on the generator/Signal charter as
`coalesce_key:` — a template like `"backup_signal:no_pat"` with no
bot-id interpolation. Generators that don't declare one are treated
as un-coalescable (one row per finding). Migration: add
`coalesce_key:` to the ~6 generators driving the worst fan-out
first (backup_signal, weak_tier audit, legacy-cred, primary_floor,
generator-load, deploy_drift); the long tail can be added as
patterns emerge.

### Plain-language title rule

Every charter ships two title fields:

- `internal_title:` — generator-id-ish, used in logs and telemetry
- `human_title:` — short noun phrase the Plex-test user can read
  cold, no jargon, no generator name

Example: `primary_model_floor_advisor`
- internal: `"primary floor advisory: {bot} primary='' not in any tier"`
- human: `"{bot} has no primary model set"`

The Health page renders `human_title`; the detail view shows both for
debuggability.

A migration sweep adds `human_title` to every existing generator.
Where the existing title is already plain (e.g., `cost_watchdog`'s
`"{bot}: maintenance ratio 67% over last 7d"`), the human_title is
the same string with the bot prefix stripped (the bot is already
shown as a separate column).

## Quick wins (orderable, no big refactor needed)

These three changes move the needle without the full bucket split.
Recommend shipping them in this order before the Health/Improvements
rename:

1. **Coalesce by signature across bots.** One row per root cause with
   "affects N bots." Add `coalesce_key:` to the 6 worst-fan-out
   generators (backup_signal, weak_tier, legacy-cred,
   primary_model_floor_advisor, registry, deploy_drift). Estimated 26
   rows collapse to 6.
2. **Humanize titles.** Add `human_title:` field; populate for every
   current generator. Mechanical; one PR.
3. **Reclassify generators by surface.** Move `plugin_curator`,
   `cron_caps_filler`, `app_permission_review` (network_egress and
   stale exec), `primary_model_floor_advisor` out of the
   "improvement" bucket. The first three go to Cleanup; the last is
   a sensor bug — fix the generator to detect primary='' and emit a
   Firing-shaped Signal instead of an Investigation Proposal.

After these three: ship Phase 2 (see updated plan below). The
Improvements page can ship empty as the placeholder for the
app-usage generator work.

## Current surface inventory (post-Phase-1, 2026-06-03)

A close read of `packages/admin/evolve_admin/web/index.html` while
scoping Phase 2 revealed the rework's mental model of "rename Alerts
→ Health" doesn't map cleanly to the actual UI. The alerts surface
isn't a standalone sidebar item — it's distributed across three
places:

| Surface | Sidebar bucket | Today's role |
|---|---|---|
| **Maintenance** (`data-page="maintenance"`) | Operate | Carries the `badge-alerts` nav badge; lane for cron jobs, gateway status, infra logs. The user's mental "Alerts" is here. |
| **Reports → Alerts subtab** (`data-subtab="alerts"` on the Reports page) | Operate | Detail view of the same Signal store, with severity-aware tabs (Firing / Snoozed / History). |
| **Recommendations → Inbox subtab** (`data-page="self-improvement"`) | Improve | Proposal queue; what Phase 1 PR #1+#2 cleaned up. |

A "rename Alerts → Health" rename would have to ripple through all
three surfaces' DOM ids, subtab routing, and badge wiring. That's
more mechanical surface area than the rework needs to deliver value.

**Revised approach for Phase 2:** **build Health as an additive
sidebar item under Operate** (sits alongside Maintenance and
Reports), not as a rename of any existing surface. Health becomes
the new "where's the fire" pane that consolidates Signals + Proposals
into Firing / Drift / Cleanup sections. Legacy surfaces stay during
the transition; deprecation comes in Phase 2B once Health is trusted.

## Non-goals

- **Not changing Signal/Proposal storage.** The arbiter on-disk
  layout (see CLAUDE.md `{shared_dir}/proposals/`, `{shared_dir}/signals/`)
  stays. This is a presentation-layer rework.
- **Not designing the app-usage generator.** That's a separate spec
  (`spec-app-usage-generator-YYYY-MM-DD.md`). This spec only carves
  out the surface it'll write into.
- **Not changing notification routing.** Alert subscriptions, daily
  digests, push notifications continue to fire from Signals as today.
  The Health page is one consumer of the Signal store; routing rules
  are unchanged.
- **Not introducing per-role policy gates.** Per
  `feedback_ui_authorization_presumed`, UI access remains the
  authorization layer.

## Open questions

### Resolved

1. ~~**Severity vs. blast radius for sort order in Drift section.**~~
   **Resolved 2026-06-03 — defer.** The coalescer in PR #1 already
   collapses N-bot fan-out into one row, so the choice between "8
   WARN bots beats 1 CRITICAL bot" and the reverse only matters when
   coalescing fails. Punt to operator feedback once Health ships.
2. ~~**What happens to in-flight Snooze state on the Proposals page
   when it's replaced?**~~ **Resolved 2026-06-03 — N/A.** Phase 2A
   is additive, not a rename. The existing Recommendations page
   keeps reading its own snooze store; Health reads the same
   underlying `/api/signals` + `/api/arbiter/proposals` endpoints.
   No migration needed.
3. ~~**Where does "Take this on" (the Investigation flow) live?**~~
   **Resolved 2026-06-03.** PR #1's coalescer already puts the
   Investigation button on the expanded per-bot row, not the group
   header. Bulk-investigation across 8 bots wasn't meaningful then
   either; the answer carries to Health unchanged.
4. ~~**Improvements page in v1.5 vs. v1.x defer.**~~ **Resolved
   2026-06-03.** Phase 2C ships an empty Improvements page (the
   scaffolding); Phase 3 fills it. Decoupling lets the structural
   work land without waiting on the generator spec.

### Still open

5. **Section assignment for the long tail of unclassified Signal
   producers.** PR #3 populated `surface` on 8 generators per the
   routing table; the other ~20 stay `None`. For Phase 2A the
   fallback is "everything `None` → Drift," but that bucket gets
   noisy. Two paths: (a) keep adding `surface` to charters in batch
   PRs until the tail is empty, (b) build an "Unclassified" group
   inside Drift that the operator can sort to the top to motivate
   future routing PRs. Decide once Phase 2A is on the test pod.

6. **Should Health absorb the `pod_report` rollup banner?** The
   Reports page surfaces a pod-wide "Audit critical" rollup that
   summarizes Firing-class findings. If Health is the new home for
   Firing, the rollup becomes redundant. Lean: leave the rollup on
   Reports for now (different audience — Reports is the
   weekly-review surface, Health is daily triage) and revisit after
   Phase 2B.

## Migration

### Phase 1 — SHIPPED 2026-06-02/03

Three PRs landed:

- [#2006](https://github.com/evolve-ops/evolve/pull/2006) — coalesce
  same-root-cause Proposals across bots (port of the Alerts page's
  `_alGroupSignals` normalize-title pattern to the Proposals
  renderer); humanize titles on the four worst-offender generators
  (`primary_model_floor_advisor`, `audit_poller`, `plugin_curator`,
  `bloat_investigator`); fix the empty-primary sensor bug.
- [#2011](https://github.com/evolve-ops/evolve/pull/2011) — `surface`
  field on the Charter dataclass + 8 charters populated per the
  routing table.
- [#2013](https://github.com/evolve-ops/evolve/pull/2013) +
  [#2016](https://github.com/evolve-ops/evolve/pull/2016) — operational
  `rewrite_proposal_titles_2026_06_03.py` tool to rewrite the
  pre-existing pending proposals on disk (their titles baked in at
  emit time; only future emissions would otherwise carry the new
  templates).

**Visible outcome on the test pod:** the 41-row queue collapsed to
~15 expandable groups, jargon-leaky titles now read as plain
English, and the data side carries the `surface` field that Phase 2
will route by.

### Phase 2 — three additive slices

Restructured 2026-06-03 after the close read of the existing
admin-ui surfaces (see §"Current surface inventory" above). Instead
of renaming Alerts → Health (which would ripple across Maintenance
/ Reports → Alerts subtab / Recommendations badge wiring), Phase 2
ships Health as an **additive sidebar item** that consolidates
findings into the new section structure. Legacy surfaces coexist
during the transition.

- **Phase 2A — New Health page (additive).** New `data-page="health"`
  sidebar item under Operate. Page renders three segmented sections:
  - **Firing** — actively-broken right now
  - **Drift** — slow drift / install hygiene
  - **Cleanup** — low-stakes housekeeping (collapsed by default)

  Routing:
  - Signals → section by severity (`alert` → Firing, `warn` → Drift,
    `info` → Cleanup), with producer overrides for known cases
    (e.g. `registry`-source generator-load failures → Firing
    regardless of severity, since the silent monitor IS the fire).
  - Proposals → section by `charter.surface` from PR #3. Proposals
    whose charter still has `surface: None` (the ~20 unclassified)
    fall through to Drift — the catchall — until Phase 2 calibration
    closes them out.

  Reuses PR #1's coalescer for both signals and proposals; reuses
  PR #2's `human_title`-via-`admin_surface_summary` rendering.

- **Phase 2B — Deprecate the Recommendations Inbox subtab.** Once
  Health is the operator's daily-driver triage surface, the Inbox
  subtab on Recommendations is redundant. Hide / remove it.
  **Keep** the In Process / History / Coaches / Observations subtabs
  on Recommendations as the "Self-Improvement deep-dive" view —
  they're orthogonal to Health.

- **Phase 2C — Improvements page placeholder.** New
  `data-page="improvements"` sidebar item under **Improve**. Empty
  state: *"Improvements appear here when Evolve has watched an app
  long enough to suggest changes. No suggestions yet — check back
  after a few days of usage."* Wires up the data-source that
  Phase 3's app-usage advisor generator will populate.

The three slices are independently shippable; Phase 2A is the
visible win, 2B is the cleanup, 2C is the scaffolding for Phase 3.

### Phase 3 — App-usage advisor generator

Unchanged from the original plan: separate spec, depends on the
generator class that reads app usage patterns and emits operator-
language product suggestions. Phase 2C's empty Improvements page
becomes its rendering surface.

## Related

- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md)
  — Signal store underneath both pages
- [spec-alert-no-repeat-2026-06-01.md](spec-alert-no-repeat-2026-06-01.md)
  — identical-content suppression (already in dispatcher)
- [spec-alert-subscriptions-2026-05-10.md](spec-alert-subscriptions-2026-05-10.md)
  — alert subscriptions; unaffected by this rework
- `project_evolve_three_bucket_ia` (memory) — current sidebar
  Operate/Improve/Settings; this spec slots into it cleanly
- `feedback_design_constraint_mildly_tech_capable` (memory) — the
  Plex-test user is the target for plain-language titles
- `project_alerts_signal_store` (memory) — locks the monitors →
  Signals, generators → Proposals split, which this spec assumes
