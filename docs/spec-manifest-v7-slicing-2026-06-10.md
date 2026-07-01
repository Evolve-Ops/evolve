# Manifest v7 Slicing — Addendum (2026-06-10)

**Status:** decision doc (Round 1 spec session per [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §8)
**Base spec:** [spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) — this is a **delta**, not a rewrite. The base design is unchanged; this doc maps it into phase-bound slices per the design-sync decision (§7.5 of the roadmap: "v7 ships in phase-bound slices").
**Locked decisions (not relitigated here):** v7 ships in slices bound to roadmap phases; Slice 1 = `event_triggers[]` (→ U2.3), Slice 2 = `privacy{}` + `audience_scoping{}` (→ U4.2), Slice 3 = Spec/Instance/Lessons split + sharing (→ alongside U3).

---

## 1. The headline finding: v7 already shipped in slices

The base spec (and the roadmap's §2.3 audit, which calls v7 "Designed; ~5 sessions") describes v7 as one unbuilt ~5-session block. **The code says otherwise.** Since 2026-05-20, large parts of v7 have landed incrementally — exactly the slicing strategy this addendum was asked to design, applied avant la lettre. An honest inventory:

| v7 component (base-spec §) | State on main, 2026-06-10 | Where |
|---|---|---|
| `manifest_shape` discriminator (§ Naming note) | **Shipped** (schema v14) | [manifest.py:215](../packages/admin/evolve_admin/applications/manifest.py) — `MANIFEST_SHAPE_LEGACY` / `MANIFEST_SHAPE_V7_ARC` |
| v13 → v7-arc migration (§10) | **Shipped and run on the reference pod** (plus a backfill tool for fields the early migration dropped) | [migrate_v7.py](../packages/admin/evolve_admin/applications/migrate_v7.py), [migrate_v7_backfill.py](../packages/admin/evolve_admin/applications/migrate_v7_backfill.py) |
| `event_triggers[]` first-class (§3, Atlas Gap 1) | **Shipped for chat-message triggers** — `match{}` shape locked, `invocation{}` sub-object added, plugin interceptor (`invocation_mode: "plugin_intercept"`), install-time validator | [spec-agent-freelance-bypass-phase2-2026-06-06.md](spec-agent-freelance-bypass-phase2-2026-06-06.md) Layer A/C; fields at [manifest.py:986](../packages/admin/evolve_admin/applications/manifest.py) |
| `bot_guidance[]` first-class (§3, Atlas Gap 2) | **Shipped** (same PR wave) | [manifest.py:985](../packages/admin/evolve_admin/applications/manifest.py) |
| `privacy{}` (§3, Atlas Gap 3) | **Not built as first-class.** The migration writes inferred defaults onto migrated Specs; `lessons_share` honors `shareable_in_lessons`. No dataclass field, no validation, no authoring path, no UI | [migrate_v7.py:390](../packages/admin/evolve_admin/applications/migrate_v7.py), [lessons_share.py:284](../packages/admin/evolve_admin/applications/lessons_share.py) |
| `audience_scoping{}` (§3, Atlas Gap 4) | **Not built as first-class.** Migration infers defaults onto migrated Specs; the trigger-level `audience` field exists but its values are unpinned free strings; no enforcement layer | [migrate_v7.py:813](../packages/admin/evolve_admin/applications/migrate_v7.py) |
| Reflect phase (§8.1) | **Shipped** (orphan detection, marker repair, missing-on-disk; spec-drift in [spec_drift.py](../packages/admin/evolve_admin/applications/spec_drift.py)) | [reflect.py](../packages/admin/evolve_admin/applications/reflect.py) |
| Adopt phase (§8.2) | **Shipped, pointer-only v1** (refuses structural diffs with `need_forge_rebuild`) | [adopt.py](../packages/admin/evolve_admin/applications/adopt.py) |
| `extend_application` (§8.3) | **Shipped** | [extend_application.py](../packages/admin/evolve_admin/applications/extend_application.py) |
| Lessons compression + redaction (§6) | **Shipped** (evidence minimums enforced; deterministic share-time redaction) | [lessons_compress.py](../packages/admin/evolve_admin/applications/lessons_compress.py), [lessons_share.py](../packages/admin/evolve_admin/applications/lessons_share.py) |
| Within-pod sharing (§9.1) | **Shipped** | [share_routes.py](../packages/admin/evolve_admin/web/share_routes.py) |
| Cross-pod export/import (§9.2) | **Not built** (the scanned-export pipeline at [export_engine.py](../packages/admin/evolve_admin/applications/export_engine.py) is a different spec) | — |
| Native v7 writes (Forge/scanner mint v7-arc for **new** apps) | **Not built** — new builds and scanner detections still mint legacy-shape manifests | [forge_jobs.py](../packages/admin/evolve_admin/applications/forge_jobs.py), [manifest.py:2532](../packages/admin/evolve_admin/applications/manifest.py) |
| `manifest-spec.md` rewrite (§10.4 session 5) | **Not done** — the current spec doc still describes the single-file shape | [manifest-spec.md](manifest-spec.md) |

**Consequence for this addendum.** The question is not "how do we cut a 5-session block into three slices" — history already cut it. The real deliverables are: (a) define the *remaining* work per slice and bind it to roadmap phases, (b) pin the schema-version mechanics so mixed-shape pods stay valid at every step (they already are one), and (c) name the ordering costs the de-facto slicing has created. The roadmap's §2.3 row for v7 should be corrected when this doc lands.

---

## 2. Schema-version mechanics: counter vs. discriminator

### 2.1 How it actually works today

Two independent axes, and the code already treats them independently:

- **`MANIFEST_SCHEMA_VERSION`** (currently `20`, [manifest.py:213](../packages/admin/evolve_admin/applications/manifest.py)) is an incrementing **field-vocabulary counter**. Migrations are *field-presence-driven*: the migration loop adds missing fields with defaults, then stamps the counter as metadata ("a metadata-only bump — the field-add loop above is what actually brings the manifest's shape forward," [manifest.py:2644](../packages/admin/evolve_admin/applications/manifest.py)). Nothing branches on the counter at runtime.
- **`manifest_shape`** (`""` = legacy single-file, `"v7-arc"` = split Instance) is the **shape discriminator**. Every consumer that must care branches on it — `pass_runner`, `reflect`, `adopt`, `extend_application`, the analytics/apps routes, `lessons_compress`. A legacy manifest and a v7-arc Instance can sit in the same `manifests/` directory and both are valid.

This separation is what makes partial migration safe, and it is already load-bearing: the reference pod runs mixed-shape today (migrated apps are v7-arc Instances; freshly forged or scanner-discovered apps are legacy).

### 2.2 The drift finding

The counter discipline has already slipped: the field blocks commented "Schema v21" (freelance-bypass Phase 2: `bot_guidance`, `event_triggers`, `invocation_mode`) and "Schema v22" (workspace file sync) landed **without bumping the constant**, which still reads `20`. Harmless at runtime (migrations are presence-driven), but it means the stamp in logs/UI under-reports vocabulary, and it proves the counter is currently aspirational rather than enforced.

### 2.3 Rules for the slices (recommendation)

1. **The counter tracks field vocabulary, never shape.** Each slice that adds fields bumps it once. Fields are added to **both shapes** (the legacy dataclass and the v7-arc Spec/Instance extraction in `migrate_v7._extract_spec`), with inert defaults, so a manifest of either shape is valid the moment the code lands.
2. **The discriminator tracks artifact shape, and only the migration/native-write paths set it.** Slices 1 and 2 never touch `manifest_shape`. Slice 3's native-write cutover is the only step that changes who mints `"v7-arc"`.
3. **Consumers branch on the discriminator, never on the counter.** Already the code's practice; this rule makes it policy. A partially-migrated pod is therefore valid at every step by construction: shape-validity is per-file, vocabulary-validity is presence-with-defaults.
4. **Re-sync the counter and guard it.** Bump the constant to `22` to match the shipped v21/v22 blocks (or relabel the comments), and add a trivial test: any dataclass field block labeled "Schema vN" fails if `MANIFEST_SCHEMA_VERSION < N`. One small PR, do it before Slice 2 stamps `23`.

Concrete assignment: Slice 1's fields are already in (part of the v21 re-sync); **Slice 2 = schema v23** (`privacy`, `audience_scoping` on both shapes); **Slice 3 needs no counter bump for the cutover itself** (shape change, not vocabulary change — any Lessons-loop field additions take v24+ as needed).

---

## 3. Slice 1 — `event_triggers[]` first-class (→ U2.3)

### 3.1 What already landed (no work to redo)

Phase 2 of the agent-freelance-bypass work shipped the field beyond what the base spec sketched: locked `match{}` (channel enum, regex `pattern`/`exclude_pattern`, compile-at-load validation), an `invocation{}` sub-object (script, JSON-request-file protocol, stdout protocol, failure mode), the `invocation_mode` top-level enum, the install-time validator, and a plugin interceptor that runs triggers structurally (`plugin_intercept`) instead of trusting LLM prose. **Deliberately chat-only** — webhook/cron/file-event sources were scoped out ([spec-agent-freelance-bypass-phase2-2026-06-06.md](spec-agent-freelance-bypass-phase2-2026-06-06.md) "Out of scope").

### 3.2 What remains for U2.3 — the watcher question

U2.4 wants gallery watcher templates ("when X happens, tell me") built on U2.3. The decision is what "event" means for watchers whose sources are external (RSS, repos, web pages) rather than chat:

| Option | Shape | Cost | Risk |
|---|---|---|---|
| **A. Extend `event_triggers[].source`** beyond chat (webhook, file-event, poll) | New trigger plumbing in the plugin/gateway per source kind | High — each source kind is its own delivery mechanism | Builds push infrastructure for sources that are natively pull |
| **B. Watchers ride `schedules[]`** — a watcher is a cron poll + condition + channel post; `event_triggers[]` stays chat-only | Zero new manifest machinery; templates are apps (Evening Sweep and Commitment Tracker already model the shape, per the roadmap's own U2.4 note) | Low | "Event-triggered" is a 1–15 min poll, not push — acceptable for every watcher source we can name today |
| **C. Hybrid** — B now, A's webhook source when a real app forces it | B's cost now | Low | None — this is the base spec's own deferral principle (§11.1: don't restructure until a real app forces it) |

**Recommendation: C** (i.e., B now). The sibling U2 spec independently reinforces this: the proactive-delivery monitor ([spec-proactive-delivery-monitor-2026-06-10.md](spec-proactive-delivery-monitor-2026-06-10.md) §6.4) consumes only `scheduled_actions[]` and declares event-driven watchers unmonitorable by construction (no deterministic delivery window). Watchers that ride `schedules[]` get delivery-window monitoring for free; watchers on push triggers would need a not-yet-designed responsiveness monitor. Option A would therefore cost new trigger plumbing *and* new monitoring. (That spec reserves the trigger-agnostic `delivery_contract.evidence` block for whenever event-trigger monitoring is designed.) U2.3 then collapses to a checklist rather than a build:

- **Lands:** (i) confirmation pass that the shipped trigger schema needs nothing for the watcher wave; (ii) Forge install-time wiring validation already exists via the freelance validator — extend it to cross-check `schedules[]` the same way; (iii) **uninstall/deprecation unwiring of triggers** per base-spec §8.4 step 3 — the app-uninstall path exists (with a dependents warning) but touches nothing trigger-related today, which becomes load-bearing once watchers are common; (iv) U2.4's watcher/digest gallery templates (owned by the U2 build track, not this spec).
- **Deferred:** non-chat trigger sources; `stdout_protocol` generalization beyond the two registered protocols.
- **Migration/compat:** none — fields exist on both shapes; legacy manifests without triggers stay valid (`invocation_mode` defaults to `agent_invokes`).

---

## 4. Slice 2 — `privacy{}` + `audience_scoping{}` (→ U4.2)

The genuinely unbuilt slice, and the trust boundary U4.2 needs. Schema **v23**.

### 4.1 What lands

- **Fields on both shapes**, exactly per base-spec §3 (no schema redesign here): `privacy{user_data_collected, opt_out_command, consent_notice, retention_days, shareable_in_lessons}` and `audience_scoping{operator, approved_surfaces, role_capabilities, operator_bypasses}` with the §11.1 pinned structure / open vocabulary.
- **Validation:** a validator module in the established pattern ([scheduled_actions_validator.py](../packages/admin/evolve_admin/applications/scheduled_actions_validator.py), [bot_guidance_freelance_validator.py](../packages/admin/evolve_admin/applications/bot_guidance_freelance_validator.py)) — structural keys required when the block is present; `event_triggers[].audience` must name a key in `role_capabilities` (closing the unpinned-free-string gap Slice 1 left); apps that declare `event_triggers[]` on a group surface must carry a `privacy.consent_notice`.
- **Forge handling:** Build authors both blocks for new apps (Critique gains a "does the privacy block match what the blueprint actually collects" check); install surfaces the consent notice to the operator; `lessons_compress`/`lessons_share` keep honoring `shareable_in_lessons` (already wired).
- **Surfacing:** the audit+score surface extends to render "what this app collects, who can reach it" from the structured blocks — this is U4.3's data source, per the roadmap's note to extend audit+score rather than revive plain-language bullets.

### 4.2 Deliberately deferred

- **Gateway-level enforcement of `role_capabilities`** beyond the trigger-audience check. The base spec's §11.2 "Atlas guard.py consolidation" — pinning the role/surface/bypass vocabulary against a working implementation — stays deferred until that consolidation. v23 makes the boundary *declared and checkable*; *enforced-everywhere* is a follow-on.
- **`retention_days` automation** (a retention daemon acting on the field). Declared now, enforced later.

### 4.3 Migration/compat

- Legacy manifests: field-add with `null`/absent defaults — absence means "not yet declared," and the validator only gates apps that opt into the relevant behaviors (group-surface triggers, Lessons sharing). No flag day.
- v7-arc Specs: the migration already writes inferred defaults (`operator_only` for personal bots, `named_users` for team bots; `shareable_in_lessons: false`). A one-shot backfill pass re-stamps already-migrated Specs through the same inference so pre-v23 and post-v23 Specs agree — same playbook as `migrate_v7_backfill.py`.
- **Interfaces to sibling Round-1 specs:** the U4 autonomy-ladder spec defines *per-integration* postures; `audience_scoping` is *per-app*. They must share surface vocabulary (`approved_surfaces` values ⊆ the ladder's integration/surface identifiers) — flagged to that session. The add-bot wizard's consent step ([spec-add-bot-wizard-build-delta-2026-06-10.md](spec-add-bot-wizard-build-delta-2026-06-10.md) §7) records consent posture in conduct prose until v23 exists, then populates `privacy{}` on installed apps — a known, bounded double-write window (see §6).

---

## 5. Slice 3 — finish the arc: native writes, Lessons loop, cross-pod (→ alongside U3)

The split itself shipped; Slice 3 is **completing the arc**, not starting it:

1. **Native-write cutover.** Forge builds and scanner-minted manifests create Spec + v7-arc Instance + Provenance directly (today both mint legacy). This is the step that flips the discriminator's default for *new* artifacts and starts draining the dual-shape tax (§6). Includes retiring the legacy-minting default at [manifest.py:2532](../packages/admin/evolve_admin/applications/manifest.py).
2. **Lessons → Adopt end-to-end.** Compression and redaction exist; Adopt v1 is pointer-only. Land the §8.2 flow: Lesson's `proposed_spec_change` → operator gate → Spec version bump → Forge rebuild (lifting Adopt's `need_forge_rebuild` refusal into an actual rebuild path). This is the community-compounding loop U3 points at, and it should land beside U3 so Layer-2 suggestions and Lessons share one "evidence-cited, operator-gated" posture.
3. **Cross-pod export/import** per §9.2 (collision check, mandatory re-review, `imported/<source_pod_id>/` namespacing). Within-pod sharing already proves the distill path.
4. **Docs cutover:** rewrite [manifest-spec.md](manifest-spec.md) to describe v7-arc as the primary shape; the 05-20 design doc becomes historical.

**Deferred (unchanged from base-spec §11.2):** two-way back-flow, federated Specs, signed Lessons, purge semantics.

**Migration/compat:** the cutover changes only what *new* artifacts look like; existing legacy manifests keep working and migrate opportunistically (next `migrate_v7` run or next structural touch). No step requires a maintenance-window flag day — an improvement over the base spec's §10 one-shot framing, bought by the discriminator.

---

## 6. Ordering constraints and the cost of slicing (the honest part)

Asked directly: is slicing worse than the original block? **No — but it is not free, and the costs are specific:**

1. **The dual-shape tax runs until Slice 3.1.** Every consumer branches on `manifest_shape` (a dozen modules already do), and every *new* feature touching manifests must be written twice-aware. The original block ended this in 5 sessions; the slicing plan extends it across U2→U4→U3 (months). Worse, legacy manifests keep *accruing* (every Forge build, every scanner detection) — the migration backlog grows while we wait. **Mitigation (recommended):** pull the native-write cutover (§5.1) forward as a standalone "Slice 3a" as soon as Slice 2 lands, rather than holding it for the full U3 wave. It is independent of the Lessons loop and cross-pod work, and it stops the bleeding. This is the one place the de-facto slicing left real money on the table.
2. **Slice 1 shipped with an unpinned reference into Slice 2.** `event_triggers[].audience` nominally references `audience_scoping.role_capabilities`, which doesn't exist yet — today those values are free strings the validator can't check. Bounded (the freelance validator checks everything else), but every trigger authored before v23 needs a vocabulary-conformance pass when Slice 2 lands. Cost of slicing; would not have existed in the block.
3. **The wizard-consent double-write window** (§4.3): U1's wizard ships before U4.2, so consent posture lives in conduct prose first and `privacy{}` second. Acceptable because the wizard's consent step is conversational anyway; flagged so neither session is surprised.
4. **No ordering inversion found.** Slice 2 depends on nothing in Slice 3; Slice 1's remainder depends on nothing in Slice 2 (the audience pinning is a Slice-2 obligation, not a Slice-1 blocker); Slice 3 benefits from 1+2 being done (Specs it shares carry the full field set) but doesn't require them. The phase binding U2.3 → U4.2 → U3 is consistent with the dependency graph.

---

## 7. Summary recommendation

| Slice | Binds to | Real remaining scope | Schema |
|---|---|---|---|
| 1 — `event_triggers[]` | U2.3 | Confirmation + uninstall unwiring + watcher templates on `schedules[]` (Option C); no new trigger sources | counter re-sync to 22 |
| 2 — `privacy{}` + `audience_scoping{}` | U4.2 | The genuinely new build: fields both shapes, validator, Forge authoring, audit+score surfacing; enforcement vocabulary deferred to guard consolidation | v23 |
| 3 — finish the arc | alongside U3 (**pull 3.1 native-write cutover forward to post-Slice-2**) | Native writes, Lessons→Adopt loop, cross-pod, docs cutover | shape flip; v24+ only if new fields |

**Open questions for the design sync:**

1. Approve pulling the native-write cutover (Slice 3a) ahead of the rest of Slice 3? (Recommended — §6.1.)
2. Counter re-sync: bump to 22 vs. relabel the v21/v22 comments? (Recommended: bump + the guard test — §2.3.4.)
3. Does the U4 autonomy-ladder session own the shared surface vocabulary, or does Slice 2? (One owner; recommend the ladder spec owns it, Slice 2 conforms.)
4. Watcher latency: is 1–15 min poll cadence acceptable as "event-triggered" for the U2.4 templates, or does any named watcher need push? (No current candidate needs push; confirm before committing to Option C.)
