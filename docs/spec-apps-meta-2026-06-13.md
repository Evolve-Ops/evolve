# META:apps — Application substrate (mission + scope + invariants + backlog)

**Status:** seed (2026-06-13). This is the design source of truth for the `apps`
META coordinator. It is a *coordinator charter*, not a feature spec — it points to
the durable design corpus, fixes the ownership boundary, and carries the live
backlog + in-flight ledger pointer.

**Aspect:** `apps` — the **application substrate**. Evolve's strongest public
differentiator ([[project_app_framework_differentiator]]): applications as
falsifiable, goal-shaped contracts distinct from the skills that implement them.

---

## 1. Mission

Own the application substrate end to end:

- **Creation** — Forge (per-bot bespoke build) and the workspace scanner
  (auto-synthesize manifests from existing bot evidence).
- **Manifests** — the schema, its versioning/migration mechanics, validators, the
  v7-arc three-artifact architecture (Spec / Instance / Provenance / Lessons).
- **Tracking & monitoring** — the scanner, Coherence (Pass A / C1 / C2 / C3),
  reconciliation, and the Tier-2 (structural) + Tier-3 (LLM) audit **mechanism**.
- **Usage** — app↔session attribution and per-app usage analytics mechanism.
- **Lifecycle** — install / uninstall / materialize (Phase 4.5), reflect, adopt,
  extend, and **export / import / share** (within-pod and cross-pod).
- **Gallery** — curation + spec quality + delivery convention, and the
  **scanned-export pipeline** that creates new gallery apps from live usage.
- **Templates** — the composition ladder: apps → **bot templates** → **pod
  templates** (the latter greenfield).

## 2. Ownership boundary (settled 2026-06-13)

`apps` was carved out of work previously scattered across four METAs. The carve
is **broad substrate ownership** — the manifest engine + lifecycle + gallery +
templates get a single owner. Hand-offs (review-and-route, do **not** rebuild):

| Concern | Owner | Note |
|---|---|---|
| Manifest schema + lifecycle mechanics | **apps** | forge/scanner/migrate/reflect/adopt/extend/export/import/share |
| Gallery curation + new-gallery-app pipeline | **apps** | scanned-export, spec quality, delivery convention |
| Bot + pod templates | **apps** | the composition ladder |
| Audit/coherence/reconciliation **mechanism** | **apps** | correctness of the checks |
| App↔session usage attribution mechanism | **apps** | — |
| Activation **outcomes** + wave sequencing | → `user-value` | "does a user get value in 24h"; rides apps' mechanism. The manifest-v7-slicing *mechanism* moved here; user-value keeps user-facing waves + add-bot-wizard. |
| Capability primitives (plugins / MCP / install scanner) | → `skills` | apps' templates *reference* skills; skill resolution defers to skills' plugin mapping. The applications-vs-skills layering ([docs/applications-vs-skills.md](applications-vs-skills.md)) is the seam — never conflate. |
| Proposal **generation** quality (effectiveness layer, app-usage-advisor, gallery recommender) | → `rsi` | apps owns the manifest *data* those proposals read/write. |
| Signal-**producer** quality on app monitors (coherence/audit signal tuning) | → `reports` | a finding on every bot = an Evolve bug, not per-bot drift. |
| Gateway-level `role_capabilities` enforcement | → `edr`/security | deferred to "Atlas guard.py consolidation"; v24 makes the boundary *declared & checkable*, enforced-everywhere is a follow-on. |

## 3. Inherited design corpus (read on bootstrap)

Manifest architecture: [spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md)
(base) · [spec-manifest-v7-slicing-2026-06-10.md](spec-manifest-v7-slicing-2026-06-10.md)
(slice plan — note Slices 1/2/3a have since shipped) · [manifest-spec.md](manifest-spec.md)
(stale — describes single-file shape; rewrite is backlog item A3) ·
[manifest-authoring-guide.md](manifest-authoring-guide.md) ·
[application-manifests.md](application-manifests.md) · [applications.md](applications.md)

Audit / coherence: [spec-app-audit-2026-05-16.md](spec-app-audit-2026-05-16.md) ·
[spec-app-coherence-and-reconciliation-2026-06-05.md](spec-app-coherence-and-reconciliation-2026-06-05.md) ·
[decision-app-tests-2026-06-08.md](decision-app-tests-2026-06-08.md) (app-test surface
**removed** — 2% adoption; coverage moved to audit+coherence — [[project_app_test_surface_killed_2026_06_08]])

Forge / export / share: [spec-export-import-forge-2026-05-26.md](spec-export-import-forge-2026-05-26.md) ·
[spec-forge-side-effects-2026-06-02.md](spec-forge-side-effects-2026-06-02.md) ·
[spec-scanned-export-2026-06-02.md](spec-scanned-export-2026-06-02.md)

Gallery: [spec-gallery-delivery-convention-2026-06-11.md](spec-gallery-delivery-convention-2026-06-11.md) ·
[gallery-spec-quality-audit-2026-06-05.md](gallery-spec-quality-audit-2026-06-05.md)

Principles: [principle-apps-inherit-bot-llm.md](principle-apps-inherit-bot-llm.md) ·
[principle-apps-minimize-bootstrap-cost.md](principle-apps-minimize-bootstrap-cost.md) ·
[principle-per-bot-inference.md](principle-per-bot-inference.md)

## 4. Key invariants / guardrails

- **applications (goal contracts) vs skills (capability primitives) stay distinct.**
  Never conflate. Templates compose apps and *reference* skills; skill→plugin
  resolution is `skills`' call.
- **Schema mechanics:** `MANIFEST_SCHEMA_VERSION` (v24) tracks field **vocabulary
  only** — never branch on the counter at runtime. `manifest_shape` (`""` legacy /
  `"v7-arc"`) is the **per-file** shape discriminator; consumers branch on it.
  Mixed-shape pods are valid by construction. Bump the counter once per
  field-adding slice; add fields to **both** shapes with inert defaults.
- **Apps inherit the bot's LLM**, never self-credential ([[feedback_apps_inherit_bot_llm]]).
- **Per-bot is the cost unit**, not per-app ([[feedback_per_app_vs_per_bot_cost_unit]]).
- **Gallery builtin spec = install source, seeded once.** Repo edits don't reach
  installed bots — re-seed on the mini after gallery edits
  ([[feedback_gallery_builtin_spec_no_repo_propagation]]).
- **A finding on every bot** of a scan/producer = an Evolve platform bug, not
  per-bot drift ([[feedback_pod_wide_fingerprint_is_evolve_bug]]).
- **Privileged paths** (install/materialize/exec — forge installs exec as **argv
  vectors** + interpreter allowlist, #2602) get auditor-grade two-pass review.
  Token-bearing config 0600; manifests + scan-status under `workspace/evolve/`
  are evolve-writable (no sudo).
- **Style-guide + both-theme check** on every SPA surface (Apps / Gallery pages).
- Two-pass-in-chip review gates auto-merge; never `--auto` on this repo.

## 5. Backlog (2026-06-13)

Shipped very recently (the v7 arc): Slice 1 (`event_triggers`/`bot_guidance`),
Slice 2 (`privacy{}`/`audience_scoping{}`, schema v24 — #2660/#2693), Slice 3a
(native v7-arc writes — #2677/#2678).

**FIRST BITE → C1. Pod templates (greenfield).** Group bot-templates into a
pod-template; provision a whole pod from one. Build on the existing bot-template
infra (`packages/admin/evolve_admin/bot_templates/` + `gallery/bot-templates/`).
Needs a design-sync first (artifact shape, provisioning order, relation to
multi-pod's pod-bootstrap). No cross-aspect gating.

Remaining (ranked, gates noted):

- **A. Manifest v7 — finish the arc (Slice 3 proper)**
  - A1. **Lessons → Adopt end-to-end loop** — Adopt is pointer-only (refuses
    structural diffs with `need_forge_rebuild`); build Lesson → operator gate →
    Spec bump → Forge rebuild. *Gated alongside RSI U3; coordinate with `rsi`.*
  - A2. **Cross-pod export/import** (§9.2) — collision check, mandatory re-review,
    `imported/<pod_id>/` namespacing. *Gated on multi-pod ≥2 real pods.*
  - A3. **`manifest-spec.md` rewrite** to v7-arc primary (doc debt).
  - A4. **Slice-3a residuals** — backfill 3 pre-fix instances; `migrate_v7` sweep
    seed-blindness on failed conversion; load→hydrate→save instance fattening.
- **B. Gallery — creating new gallery apps**
  - B1. **Scanned-export refinement** — Stage 0 (0a–0f) is built; the post-Stage-0
    generalise/scrub/validate/review/stamp passes are deferred. Finish the
    "real bot app → scrubbed shareable gallery spec" path.
- **C. Templates**
  - C1. **Pod templates** (FIRST BITE, above).
  - C2. (existing) bot templates — maintenance only; solid.
- **D. Hand-offs (designed, owned elsewhere — review & route)**
  - Effectiveness layer / app-usage-advisor → `rsi`.
  - App-monitor signal-producer tuning → `reports`.
  - Gateway `role_capabilities` enforcement → `edr`/security.
- **E. Honest usage reporting (usage-tracking audit, 2026-06-13).** The Apps
  card showed "Active · N files touched in 30d" — a file-**mtime** proxy, not
  usage: read-only / external-output apps looked dead even when delivering
  daily, and unrelated edits looked like use. The audit found three items:
  - **E.A. Honest reporting (SHIPPED — this PR).** Surface the real per-app
    delivery signal (`delivery_monitor` ledger → `delivery_status.latest_status_by_app`)
    as the card's PRIMARY activity line; relabel the file-mtime metric as a
    muted *footprint / maintenance* signal and drop the false Active/Quiet/
    Inactive verdicts; non-scheduled apps say "usage not measured" honestly.
    The frozen `usage_metadata` fields (`invocation_count`/`last_run`/…, written
    once by `migrate_v7` and never refreshed) are confirmed **not rendered**
    anywhere in the SPA — nothing to remove, kept out of scope to render.
    Reporting-truth only: no change to the `delivery_monitor` / `usage_logger`
    producers.
  - **E.B. Per-app invocation + cost telemetry (BACKLOG).** Stamp agent turns
    with `app_id` so we get true invocation counts + per-app cost — replacing
    the frozen `usage_metadata`. Unblocks the app-usage-advisor (→ `rsi`).
  - **E.C. `success_criteria` runtime evaluation loop (BACKLOG).** Evaluate each
    app's declared `success_criteria` against runtime evidence and feed the
    verdict back. Co-design with `rsi` (effectiveness layer) + the verify daemon.
- **F. Agent-freelance "script-failure" slice** (team-bot-c live incident, 2026-06-16).
  team-bot-c's Task Management app ran `scripts/tasks.py`, the script failed during an
  agent turn, the raw OpenClaw `(agent) failed` warning leaked into chat, and the
  agent confabulated success ("Task LA015 created") — the FAILED-invocation variant
  of the agent-freelance bypass. Four bites, all on the
  [agent-freelance-bypass-phase2](spec-agent-freelance-bypass-phase2-2026-06-06.md)
  layering (Layer C = `plugin_intercept`):
  - **sf-b1 (#2937, SHIPPED).** Forge defaults at-risk script-backed apps to
    `plugin_intercept` (+ `raw_text` protocol). New apps only — no install migration.
  - **sf-b2 (#2936, SHIPPED).** Scanner/dashboard escalates at-risk `agent_invokes`
    apps info→WARNING with a "migrate to plugin_intercept" nudge on the app card.
  - **sf-b3 (#2938, SHIPPED).** Runtime `app_script_failure_audit` emits a Signal
    when an app-script invocation fails (extends `agent_bypass_audit`'s transcript
    walk to catch `(agent) failed` markers).
  - **sf-b4 (this slice).** Closed a **scan-time calibration gap** in sf-b2:
    `validate_bot_guidance` keyed at-risk solely on narrow imperative `bot_guidance`
    PROSE markers ("do NOT freelance", "run exactly python3 scripts/…"). team-bot-c's apps
    describe scripts in ordinary *documentation* prose ("`scripts/tasks.py` — Primary
    entry point …"), which trips **zero** markers; worse, on **v7-arc instances
    `bot_guidance` is not overlaid during hydration** (`hydrate_v7_arc_instance`
    overlays `event_triggers`/`success_criteria`/… but not `bot_guidance` or
    `invocation_mode`), so the prose path is structurally blind to *every* v7-arc app.
    Net: Task Management — the app that actually failed live — got **no** scan-time
    warning; only the runtime catch (sf-b3) fired, after the leak. (The "⚠ 1 warning"
    the operator saw on Memory System / Daily Briefing / Dropbox is the unrelated
    **coherence** `C-A1` badge, `recurring_behavior_only_suspect_actions`, not sf-b2.)
    **Fix:** `validate_bot_guidance` now ALSO treats a registered bot-local script
    reference — `scripts/*.py|sh` / `legacy-scripts/*.py|sh` in `evidence_files` /
    `files` / `realized_files` (which DO ride on the instance and survive hydration)
    — as at-risk-shaped, reusing sf-b2's existing `warning` + migrate-nudge plumbing
    (no new path, no JS change, never `build_blocker`, no install migration). Now
    flags Task Management + Dropbox (both genuinely script-backed); Memory System /
    Daily Briefing stay quiet (no bot-local invoked script).

## 6. In-flight ledger

The live in-flight ledger lives in memory [[project_apps_meta_2026_06_13]] (so a
fresh `/meta apps` and the fleet watcher both reconstruct it). As of 2026-06-13:
**no app PRs in flight** (only dependabot). First bite (pod templates) pending a
design-sync.

## 7. Deploy mechanism

Heterogeneous, **canary-gated** (`pod.release.mode=canary`):
- Gallery/manifest/template/doc changes ride the repo-puller (gallery is in-repo);
  **re-seed builtin gallery specs on the mini** after gallery edits (install-source,
  seeded once).
- Admin-ui kickstart for Apps/Gallery SPA + `*-routes` changes.
- Scanner/forge/audit run in the **packaged analyzer** — relevant daemon kickstart;
  watch the stale-module-cache trap ([[feedback_pull_deploy_stale_module_cache]]),
  and sudo-run scripts must use the venv interpreter
  ([[feedback_sudo_subprocess_interpreter_must_be_venv]]).
- `sudo evolve-admin deploy <bot>` for actual installs / materialize.

---

## 8. Pod templates — first-bite design (2026-06-13 design-sync)

**Decisions (operator-confirmed):**
- **Driving use case: capture an existing pod's shape.** Derive a pod-template
  from the live pod so "this pod" becomes reproducible. Round-trip (capture →
  template → re-provision diff) is the proof.
- **Provision scope: local multi-bot scaffold.** Provision N bots onto an
  *existing* pod. Remote / new-pod bootstrap is `multi-pod`'s M1 (gated on 8.3
  Linux install path + ≥2 real pods) — **out of scope here**, but the artifact is
  designed so it can later seed that path.
- **Forward-compat:** `pod.yaml` content is a `multi-pod` **typed-artifact
  payload** — wrap-able as `{type: "pod_template", schema_version, payload,
  signature}` ([design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md)
  §5). `network.json` is the only source of truth for pod membership
  ([[feedback_explicit_pod_membership]]) — the extractor reads it, never scans
  `/Users/`. **No credential custody** (distribution-not-custody) — integrations
  map by name/scope, never secrets.

**`pod.yaml` shape (sketch — Bite 1 pins it):**
```yaml
schema_version: 1
name: <slug>
display_name: ...
description: ...
pod:                       # pod-wide network.json seed (no secrets)
  release_mode: canary
  model_tiers: {...}
  integrations: {...}      # integration map by name/scope only
bots:
  - bot_id: <name>
    bot_template: <bot-template-name>   # ref into gallery/bot-templates/
    channel: <messaging channel>
    voice_preset: ...
    overrides: {...}
```

**Two-bite split (shared checkpoint branch):**
- **Bite 1 — schema + extractor (capture).** A `pod_templates/` package (loader +
  validator, mirroring `bot_templates/`) + an extractor that reads the live pod
  (network.json membership + each bot's installed bot-template/apps) and emits
  `gallery/pod-templates/<name>/pod.yaml`. *Proof:* extract the reference pod →
  a `pod.yaml` that validates.
- **Bite 2 — provisioner (local scaffold).** `pod.yaml` → loop the bot-template
  provisioner per referenced bot + merge the pod-wide `network.json` seed;
  dry-run + apply modes. *Proof:* dry-run-provision the captured template → diff
  vs the live pod shows it reproduces it (round-trip).

**Out of scope (both bites):** remote/new-pod bootstrap; credential custody;
cross-pod deposit; the signature on the typed envelope (declared in schema,
populated when multi-pod's deposit path needs it).

---

## 9. Defined / Discovered manifest lifecycle (design-sync 2026-06-24)

**The root cause this addresses.** The entire scanner false-positive saga
(#2885 → #2894 → #2898 → #2899 → #2900 → #3044 → #3059 → #3060 → #3062 →
#3095 → #3108 — six rounds, eleven PRs) was patching one disease: **the scanner
is simultaneously the author *and* the editor of every manifest, with no
authoritative anchor.** Every scan is free to re-mint, merge, archive, or rename,
so we kept whack-a-moling the heuristics that govern that freedom. The fix is a
single explicit **source-of-truth axis**, not another heuristic.

### 9.1 The model — one manifest, two statuses

A manifest gains a **`status`** field: `discovered` ↔ `defined`. *Not two file
types — one manifest with a lifecycle state.*

- **`discovered`** — the scanner's synthesis from observed files/scripts/crons.
  The scanner fully owns it: free to churn, merge, re-identify, or archive. Low
  stakes by construction (Evolve's guess, a draft). The whole "is this a phantom?"
  anxiety **evaporates** here — nobody promised it's real.
- **`defined`** — the operator (or, later, the end user) has **vouched** for it.
  The operator's intent is now the source of truth. The scanner may only
  *observe and propose*, never churn or destroy.

**Promotion** (`discovered → defined`) is the **one explicit operator gate** —
one click, **reversible** (demotion un-vouches). It is the act that tells Evolve
"the user wants an app with these goals."

**Born-status & migration:**
- New **forge / gallery installs → born `defined`** (installing *is* declaring
  intent). Maps onto the existing `source` values `user_created` /
  `gallery_installed` / `forge_built`.
- New **scanner discoveries → born `discovered`.** Maps onto `source ==
  "discovered"`.
- **Fleet migration: all existing manifests land as `discovered`** — the
  pod-admin reviews and promotes deliberately. **No bulk auto-promote** (it would
  launder today's *contaminated* manifests straight into "operator-vouched"; the
  per-app gate is where contamination gets caught — cf. the live Atlas
  tile/detail identity mismatch, §9.5).

### 9.2 What `defined` actually guarantees (and what it does NOT)

`defined` does **not** freeze fields. It guarantees exactly two things:

1. **Existence** — a `defined` app is **never auto-archived**, even if every file
   on disk vanishes. (Extends the existing `_is_operator_authored` archival shield
   at `scanner.py:656` to key on `status`.)
2. **Identity stability** — a `defined` app is **never merged, split, or
   re-identified** into another app (the Atlas-conflation failure mode). It stays
   *one coherent app with a stable id and anchored identity*.

Everything else — scope, files, crons, behavior — **stays fluid and current**
(§9.3). Promotion vouches *"this app is real and is **this** app,"* not *"this
description is frozen forever."*

**Anchored identity vs. fluid scope (the fork, resolved — anchor).** To prevent
auto-rewrite from re-introducing identity churn (Atlas's name flipping between
"Member Management" and "daily digest"), the manifest's self-description splits:

- **Anchored identity** = `name` + a short canonical "what this app *is*" line.
  **Fixed at promotion.** Changes only by explicit operator edit, or an
  LLM-proposed change the operator accepts. *This is the anti-churn guarantee.*
- **Fluid capability/scope** = detailed scope, behavior, files, crons. Scanner
  keeps it current; major changes hit the change log (§9.3).

**Implementation seam:** this maps onto the v7-arc split that **already exists** —
the **App Spec** (intent) vs the **Instance** (per-bot realization). `defined` ≈
*this app has an operator-vouched Spec*; the Instance stays the fluid observed
reality. So this is largely *promoting the Spec layer to operator-owned* + wiring
the classifier/log, not a from-scratch build.

### 9.3 Drift is fluid — narrate, don't gate

Apps drift: features get added, capabilities removed. The system **rolls with it**
— **no per-change operator approval.** Instead, the scanner keeps the manifest
fresh automatically and gives the operator a *narrative* via a **drift-significance
classifier** + a per-manifest **change log**:

- **Minor drift** (new `.md` / data file, doc tweak, cosmetic) → absorbed
  silently. No log entry.
- **Major drift** (a new / removed / meaningfully-changed *script*, *cron*,
  *scheduled_action*, or *skill/capability dependency* — i.e. behavior changed) →
  absorbed **and** logged.

The operator reviews the change log post-hoc and acts only if a change was wrong /
unintentional (a script got deleted and broke the app → repair; an unexpected
capability appeared → investigate). **The classifier is deterministic-first**
(executable-surface change = major; data/doc = minor), escalating to the LLM only
for the ambiguous "did this script's *behavior* meaningfully change" case
(consistent with this aspect's cheap-floor-first / LLM-as-escalation discipline).
It consumes the reconciliation pass's existing missing/extra-files deltas.

**Change-log artifact:** a per-manifest `change_log[]` (in the v7-arc
Provenance/Lessons artifact), surfaced on the manifest detail + a tile badge when
there's **unreviewed** major drift ("2 changes since you looked").

### 9.4 The scanner's job flips with status

- For a **discovered** app the scanner is a *guesser* — "make coherent sense of
  these files." Churn is fine; it's a draft.
- For a **defined** app the scanner becomes a *watchdog* — "does reality still
  match the vouched contract? narrate the drift." A defined app can never be a
  phantom (operator vouched) and can never be silently archived (intent outlives
  missing files). **This is the terminating condition the FP saga never had:** the
  stakes collapse — churn on discovered is fine, defined is protected.

### 9.5 Multi-audience surface (routed — apps owns the model, not the surface)

Most **end users never see the manifest** — it lives in the admin UI (pod-admin
only), not the bot primary user's view. Two surface ideas follow:

- Let the bot's primary user **read** their app manifests via the **`evo`**
  keyword path.
- **Drive promotion through the end user** as a verification step ("here are the
  apps I found on your bot; which do you actually want?") — the end user is often
  the best judge of "yes, this is mine."

**Ownership split:** `apps` owns the **status model + promote/demote mechanism**;
the **evo read surface + user-driven-promotion** route to **`evo-asst`** (the evo
assistant's tools/surface) + **`users`** (which user is *allowed* to promote — a
capability question). Coordinate when those bites come up; do not build the evo
surface inside `apps`.

### 9.6 Bite slicing

1. **Status model + promotion + existence guarantee** (FOUNDATION). Add `status`
   (both shapes, inert default `discovered`); born-status rules; migration =
   existing → `discovered`; promote/demote API (one-click, reversible, stamps
   `last_promoted_at`, marks anchored-identity fields authored); wire
   `status == defined` into the archival shield. **Reconcile with the
   *existing* "Promote" tile button** — its current semantics must be
   disambiguated or repurposed, not collided with. Reversible, mostly additive.
2. **Identity-stability for defined apps (scanner watchdog mode).** Scanner must
   not merge / split / re-identify / churn the anchored identity of a `defined`
   app. Builds on #3095 (`_are_distinct_apps` / `_merge_two_manifests` / dedup) +
   #3108 (unmerge-guard). Privileged-adjacent (live-manifest reconcile) →
   auditor-grade two-pass.
3. **Drift-significance classifier + change log.** Deterministic major/minor over
   reconciliation deltas; `change_log[]`; absorb-and-log for defined apps.
4. **Apps-UI: promotion control + change-log surface + unreviewed-drift badge.**
   Depends on 1; surfaces 3. Style-guide + both themes.

Bite 1 is the clear first mover (everything needs the status field). 2 and 3 can
follow in parallel (both depend only on 1); 4 last. The evo/users surface (§9.5)
routes out.

### 9.7 Bite 1 — implementation notes (as shipped)

Bite 1 (status model + born-status + migration + promote/demote + existence
shield) is implemented as described above, with these resolutions worth pinning
for Bites 2–4:

- **The field is `definition_status`, not `status`.** §9.1 names a "`status`
  field: `discovered ↔ defined`", but the manifest already has a load-bearing
  `status` field carrying the LIFECYCLE vocabulary (`active` / `paused` /
  `draft` / `deprecated` / `hidden` / `dormant`), which dozens of consumers and
  the v7-arc instance schema's own `status` enum branch on. Overloading it would
  be a silent semantic collision, and repurposing it is far beyond an
  inert-additive Bite-1 change. So the source-of-truth axis ships as a NEW field
  **`definition_status`** (`discovered` / `defined`); `status` stays the
  lifecycle axis and `source` stays the immutable creation axis. The three are
  orthogonal. Bites 2–4 reference `definition_status`.
- **Bite-1 guarantee scope = existence only (§9.2 guarantee 1).** The shield
  short-circuits the L3 archival classifier (`_archive_platform_file_only_stubs`),
  paralleling the existing `_is_operator_authored` shield. Per §9.2, that shield
  does NOT (and `_is_operator_authored` also does not) guard the same-pass
  `_dedup_manifests` merge/delete path — so **identity-stability (§9.2 guarantee
  2) is genuinely Bite 2 (§9.4)**; a `defined` app can still lose a dedup-merge
  until Bite 2 lands. Do not read Bite 1 as the full identity guarantee.
- **Anchored-identity marking is per the v7-arc seam.** On LEGACY manifests,
  promote marks `name` + the canonical identity line `description` authored in
  `provenance.field_origins` (source `confirmed`, channel
  `definition_status:promote`); demote relaxes ONLY marks carrying that channel,
  so a vouch set by another path (e.g. coherence "Mark as ready") survives a
  round-trip. On **v7-arc Instances** the `provenance` block is *install*
  provenance (no `field_origins`) and identity is anchored by the immutable
  Spec, so promote/demote flip `definition_status` only — the field-marking is
  skipped to avoid corrupting the install block. (Bite 2's deeper "promote the
  Spec layer to operator-owned" refinement, §9.2 seam, builds on this.)
- **Born-status / migration** map onto `source` exactly as §9.1 states, via the
  single helper `manifest.born_definition_status(source)`. The scanner stamp
  pass uses `setdefault` so a re-scan can never un-promote a vouched app; forge
  IMPROVEMENT runs (job_type ≠ install) never auto-promote. Wired at the forge
  **install** path (born `defined`, covers gallery + the spec-session
  user-create that becomes a forge install) and the scanner stamp pass (born
  `discovered`). The lightweight admin **bare-stub create**
  (`POST /api/applications/<bot>/create`) deliberately does NOT stamp
  born-`defined` — a bare stub is not yet a vouched app, so it stays
  `discovered` and the operator promotes it once it is real (it remains
  archival-safe meanwhile because `source == user_created` is already an
  `_is_operator_authored` shield).
- **Promote button reconciliation (§9.6 bite 1).** Three distinct "promote"
  concepts now coexist and are disambiguated, not collided: the gallery
  files-pack `↥ Promote` (export), the coherence "Promote to authored"
  (field-provenance flip → `POST …/promote`), and the new definition promote
  (`POST …/definition/{promote,demote}`, backend-only until the Bite-4 UI). No
  UI button added in Bite 1.

### 9.8 Bite 3 — implementation notes (as shipped)

Bite 3 (the drift-significance classifier + drift narrative log of §9.3) is
implemented as described above, with these resolutions worth pinning for Bite 4:

- **The field is `drift_log`, not `change_log`.** §9.3 names a per-manifest
  "`change_log[]`", but the v7-arc Instance ALREADY carries a load-bearing
  `change_log[]` — the forge/capability audit trail (`capability_added` /
  `blueprint_correction` / …) that `lessons_compress` rolls into Lessons, with
  a strict JSON schema (`additionalProperties: false`, fixed `kind` enum,
  `required: [entry_id, who, description]`). The drift-entry shape (`kind`
  add|remove|modify + `significance` + `reviewed`) is incompatible with it, and
  overloading it would corrupt the Lessons pipeline. So — exactly as Bite 1
  resolved the spec's "`status`" axis to `definition_status` (§9.7) — the drift
  narrative ships as a NEW distinct field **`drift_log[]`** on both shapes
  (legacy dataclass + v7-arc Instance schema), schema **v28**, inert default
  `[]`. The existing `change_log` is untouched. Bite 4 reads `drift_log`.
- **Entry shape (as shipped).** `{ts, kind: add|remove|modify, target_type:
  file|cron|action|dependency, target, significance: "major", summary,
  reviewed: bool, source: "scanner_drift", classifier: deterministic|llm}`.
  `source` (a discriminator) and `classifier` (audit) are additive over the
  §9.3 sketch; `ts` is the entry's timestamp. Only `major` drift is ever
  logged, so `significance` is constant today but explicit for forward-compat.
- **Deterministic-first; the LLM seam is real but dormant on this feed.** The
  classifier (`applications/drift_classifier.py`, a new module mirroring
  `purpose_classifier.py`) decides major/minor by surface TYPE with no model
  call: cron / scheduled_action / dependency are always behavioral → major; a
  file is classified by its path (code extension or a `scripts/`/`bin/` segment
  → major; data/doc → minor, absorbed silently). The LLM (injectable `llm_fn`,
  no provider literal, default-to-minor on error/low-confidence) is reached
  ONLY for the ambiguous "did this **script's content** meaningfully change"
  case. The current reconcile deltas are add/remove (+ a scheduled-action
  evidence-anchor *modify*, a deterministic-major behavioral surface), so a
  script-content *modify* event is not yet produced by the feed — the
  escalation branch is the spec-mandated seam, exercised by the unit tests via
  an injected `llm_fn`, and the scanner wires `llm_fn=None` (deterministic-only)
  until a sha-drift/script-modify feed exists. Zero LLM cost on every scan today.
- **`defined`-only narration (§9.4).** `narrate_drift` appends only when the
  manifest is `defined`; a `discovered` app gets fresh content from the
  reconcile pass and no narrative (it's a churnable draft). The feed
  (`ReconciliationSummary.drift_events`) is collected for every manifest —
  purely additive, the provenance gate's silent-vs-staged behavior is
  unchanged — and captures BOTH silently-absorbed (observational) and staged
  (authored) deltas, since the narrative is independent of the gate.
- **Idempotency.** The common case is self-clearing: the reconcile pass absorbs
  observational drift (drops the missing file from `files[]`), so the delta does
  not recur next scan. For the authored/staged case (the delta persists), an
  unreviewed-duplicate guard in `narrate_drift` suppresses re-logging the same
  `(kind, target_type, target)` until the operator reviews it; a genuine
  recurrence after review re-narrates.
- **Unreviewed marker = data, not UI.** `drift_classifier.count_unreviewed_drift`
  (filters `source == scanner_drift` ∧ `reviewed is False`) is the pure data
  source for the Bite-4 tile badge ("N changes since you looked"). No UI in
  Bite 3.
