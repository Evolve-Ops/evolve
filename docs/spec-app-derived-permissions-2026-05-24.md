# App-derived permissions — design spec

**Status:** Draft. Pre-implementation; awaiting design approval before any code lands.

**Date:** 2026-05-24. **Pivoted 2026-05-25** — see Principle. Original framing was "permissions enforced from intent"; pivot is "permissions tracked from intent, enforcement opt-in." Pivot driven by the UX failure of the original design on the "build me a thing" flow.

**Origin:** Slack failure on team-bot-a 2026-05-24 (`run python3 ops/tools/unified_task_system.py (agent) failed`). Live triage in [project_oc_2026_5_18_exec_deny_migration](../memory/project_oc_2026_5_18_exec_deny_migration.md) found team-bot-a/admin-bot/team-bot-b/personal-bot/team-bot-c all stuck at `tools.exec.security="deny"` with empty allowlists, despite a manual fix on 2026-05-20 (clobbered by a 2026-05-22 redeploy via `_infer_exec_policy` in [deploy.py:1079](../packages/admin/evolve_admin/deploy.py:1079)).

**Adjacent:**

- [spec-upstream-oc-exec-policy-adoption-2026-05-12.md](spec-upstream-oc-exec-policy-adoption-2026-05-12.md) — the migration that revealed this gap.
- [feedback_generators_consider_intent](../memory/feedback_generators_consider_intent.md) — same pattern (context-free generators) applied previously to auth_drift_filler.
- [project_l1_l2_applier_architecture](../memory/project_l1_l2_applier_architecture.md) — the applier shape any reconciler needs to use for member-bot openclaw.json writes.
- [project_alerts_signal_store](../memory/project_alerts_signal_store.md) — where the new Signals land.
- [docs/spec-manifest-reflex.md](spec-manifest-reflex.md) — current app/manifest generator; the natural extension point.

---

## Problem

Two subsystems write to the same bot config from opposite directions and don't know about each other:

1. **The app system** (manifest scanner, manifest-reflex generator, forge, INSTALLED_APPS.md framing) installs apps and tells the bot's LLM that it can run scripts like `ops/tools/unified_task_system.py`.
2. **The exec-policy system** (`_infer_exec_policy` in `deploy.py`, security generators, OC upstream migrator) defaults to `deny` whenever `exec-approvals.json` is empty.

Nothing connects them. Every member bot ends up in a state where its INSTALLED_APPS.md announces capabilities its exec policy denies. The LLM dispatches the script anyway, OC denies the call, the LLM has already composed an optimistic reply, and the user sees a contradiction:

> Team-Bot-A: Added to the management meeting agenda — refund amount queued for discussion. I'll confirm the task ID once the sub-agent completes.
> ⚠️ 🔨 run python3 ops/tools/unified_task_system.py (agent) failed

This isn't a one-off bug. Pod-wide today: only security-bot (29 allowlist entries) can exec anything. The other six bots are silently broken.

It's also a **recurring** error, not a static one. Manual fixes via `openclaw exec-policy set --security full` are clobbered on the next Evolve redeploy because `_infer_exec_policy` re-runs and re-derives `deny`. Until the two subsystems are connected, the loop continues.

---

## Principle

**Apps declare intent. The system tracks intent. Enforcement is opt-in by the operator.**

A bot runs as its own user account on the operator's machine, in its own workspace. Locking down its exec by default treats a bot like a hostile process; in practice it's a trusted agent acting on the operator's behalf. The right default is: the bot can do anything in its own workspace, the same way a human can in their own shell. Manifests capture *what* the bot is doing (for audit, sharing, visibility, opt-in tightening); they don't gate *whether* the bot can do it.

This serves two goals the original framing pitted against each other:

- **UX (build-me-a-thing flow).** A bot asked to track protein and run a 6 PM cron just *does* it. No manifests up-front, no permission proposals, no silent failures. The exec works because the bot is acting in its own account. This is the [feedback_design_constraint_mildly_tech_capable](../memory/feedback_design_constraint_mildly_tech_capable.md) Plex test applied to the bot system — and the [project_low_friction_bot_creation](../memory/project_low_friction_bot_creation.md) differentiator depends on this flow not being broken by under-the-hood permission machinery.
- **Visibility and opt-in hardening.** The manifest system still tracks everything the bot does. Operators see "team-bot-a currently runs these 7 apps with these scripts and these crons" without an audit ever blocking the work. When an operator wants tighter posture, the manifest set is already the seed for an allowlist — one toggle, manifest-derived, ready to enforce.

This is the same intent-vs-baseline pattern as [feedback_generators_consider_intent](../memory/feedback_generators_consider_intent.md): you can't enforce a policy without knowing intent, and the bot's own apps are the most reliable expression of its intent. The pivot is in *what* we do with that knowledge — visibility and opt-in tightening, not silent enforcement.

### Default exec policy by bot role

| Bot role | Default `tools.exec.security` | Rationale |
|---|---|---|
| Member bot (team-bot-a, admin-bot, team-bot-b, personal-bot, team-bot-c) | **`full`** | Bot is trusted to operate in its own user account; UX-critical flows depend on this. |
| Primary bot (evo) | `deny` | **Interim posture** — evo currently shares its macOS user account with the privileged admin daemon, so any exec on evo is a path to admin-daemon-level reach via the `/approve` slash-command channel. [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) moves evo onto its own non-privileged macOS account, after which this carve-out is removed and evo lands at `full` like other member bots. |
| Sysadmin / hardened bot (security-bot) | `allowlist` | Operator deliberately chose tighter posture; manifest declarations + operator-set entries seed the allowlist. |
| Any bot with operator-set `execPolicy` in network.json | as specified | Explicit override; honored above defaults. |

The reconciler runs in all modes. In `full` mode it **tracks** (computes the would-be allowlist for visibility but writes nothing that changes runtime enforcement). In `allowlist` mode it **enforces** (writes `exec-approvals.json` to match manifest-derived + operator-set entries). The opt-in toggle is the operator flipping `execPolicy: "allowlist"` in network.json — at which point the manifest-derived allowlist takes effect immediately, no further authoring required.

---

## Architecture

### 1. App manifest permission declarations — hybrid model, single document

The manifest scanner already writes per-app records as a single JSON file at `workspace/manifests/i-XXXXXXXX.json` (mirrored to `{shared_dir}/applications/{bot_id}/`). The schema already carries ~50 top-level fields including `files`, `crons`, `inputs`, `outputs`, `interface_contract`, `requirements`. The reconciler reads these.

**Permissions live in the same manifest, as a normal top-level `permissions:` field.** The "one document per app" property is preserved — atomic shareability, single source of truth, single discovery convention. No sibling files.

**Inference is the floor.** Every script path under `files:` becomes an inferred exec allowlist entry. Every entry under `crons:` becomes an inferred cron permission. Inference always runs, regardless of whether a `permissions:` block is present.

**Explicit declarations are additive.** A manifest may optionally include `permissions:` to declare needs that inference can't see:

```json
"permissions": {
  "exec": [
    "sudo /usr/sbin/launchctl kickstart"
  ],
  "fs_read": [
    "/Users/Shared/evolve/proposals/"
  ],
  "fs_write": [
    "/Users/Shared/evolve/signals/firing/"
  ],
  "network_egress": [
    "api.openrouter.ai",
    "*.anthropic.com"
  ],
  "env": [
    "ANTHROPIC_API_KEY"
  ],
  "_note": "edited by operator 2026-05-24 — cron-only invocation, keep exec on tools/foo.py"
}
```

**Net permission set = inferred ∪ explicit.** Absence of an explicit declaration never means "no permissions needed" — inference still runs.

**Role by mode.** In `full` mode (member-bot default), this set is **advisory and tracking** — it populates the preview allowlist, drives review/drift findings, and is the seed for the one-click downgrade to `allowlist` mode. It does not enforce. In `allowlist` mode it is the **enforced declaration** — entries here are what makes the bot's scripts runnable. Same data, different effect.

#### Protecting operator-authored content: `AUTO_MANAGED_FIELDS`

The scanner and manifest-reflex runner already do field-aware merges rather than wholesale rewrites ([`_merge_two_manifests`](../packages/admin/evolve_admin/applications/scanner.py:1778); [`manifest_reflex_runner.py:20-23`](../packages/analyzer/manifest_reflex_runner.py:20) — *"update=true rows merge `files/crons/inputs/outputs` into the existing manifest"*). This was implicit. Formalize it.

A single named constant lists the fields auto-generation is allowed to write or overwrite:

```python
AUTO_MANAGED_FIELDS = frozenset({
    "files", "crons", "inputs", "outputs",
    "evidence_files", "improvement_history",
    "updated_at", "schema_version",
    # ... add new auto-managed fields here; everything else is operator-owned
})
```

- The scanner and manifest-reflex runner check this set before touching any field. Fields not in the set, when already populated, are carried through untouched.
- `permissions:` is **not** in `AUTO_MANAGED_FIELDS`. Once an operator (or an autonomous generator with operator-approved provenance) writes it, no scan or reflex run can clobber it.
- The optional `permissions._note` string lets operators leave human-readable provenance inline. Purely informational.
- "Wipe and re-scan" workflows MUST read the existing manifest first and carry operator-owned fields forward before regenerating auto-managed ones. The scanner already has this discipline at [scanner.py:1594](../packages/admin/evolve_admin/applications/scanner.py:1594) (*"don't overwrite good data"*); the registry makes the rule explicit and testable.
- **Test discipline:** a regression test pins that for every field in the manifest schema, either (a) the field is in `AUTO_MANAGED_FIELDS` and a code path writes it, or (b) the field is not in the set and no auto-generation path writes it. Adding a new field to the schema fails CI until classified.

#### Conflict-mode rule

If a future change makes a field auto-managed that used to be operator-owned (or vice versa), the runner does NOT silently rewrite existing manifests. It logs a Signal (`manifest_field_classification_changed`), leaves the existing value alone, and surfaces a proposal so the operator decides. Migration of classification is an explicit operator action, never silent.

### 2. The reconciler

A new module: `evolve_admin.app_permissions.reconciler` (named to avoid shadowing the top-level `permissions` package in `packages/analyzer/`). Single entrypoint:

```python
def reconcile_bot_permissions(bot_id: str, *, dry_run: bool = False) -> ReconcileResult:
    """Read installed apps, compute would-be permission set; write iff mode is allowlist."""
```

Called from `ensure_plugin_config` in `deploy.py` **before** `_infer_exec_policy` runs. The flow:

1. **Read** every app's `files`, `crons`, and optional `permissions:` block from `workspace/manifests/`.
2. **Compute** the unified permission set:
   - Infer from `files:` (script-classified entries only — see §1 / Q6) and `crons:`.
   - Merge with explicit `permissions:` from each manifest.
   - Tag every entry with provenance: `source: app-derived, app: <app_id>, mode: inferred|explicit`.
3. **Determine enforcement mode** by reading the bot's `execPolicy` (network.json) and current `tools.exec.security`:
   - `allowlist` → enforce.
   - `full` → track only.
   - `deny` → primary bot rule; track for visibility but do not write.
4. **Branch on mode:**

   **Allowlist (enforcing):**
   - Diff against current `exec-approvals.json`.
   - Add app-derived entries that are missing.
   - Leave `source: operator-set` entries alone (network.json override path, manual additions).
   - Remove `source: app-derived` entries whose app is no longer installed.
   - Write via the L2 applier ([project_l1_l2_applier_architecture](../memory/project_l1_l2_applier_architecture.md)) — `/tmp` staging + `sudo /bin/cp` + chmod 644 + gateway kickstart.

   **Full (tracking only):**
   - Write the *would-be* allowlist to a sibling file `exec-approvals.preview.json` (not consulted at runtime). The admin UI reads this to show the operator "here's what team-bot-a is actually doing" and to populate the one-click "downgrade to allowlist" toggle.
   - Do not modify `exec-approvals.json` itself — runtime enforcement remains `full`.

   **Deny (primary bot):**
   - Skip the write entirely. Primary bots have no exec by design.

5. `_infer_exec_policy` runs next and derives the correct top-level mode (`full` by default for member bots; `allowlist` if operator opted in; `deny` for primary). With the new defaults it will no longer land on `deny` for member bots, eliminating the original regression.

The reconciler is mode-aware in its write path but uniform in its analysis. Same data, different effect. The opt-in toggle (operator flipping `execPolicy: "allowlist"` in network.json) is what shifts the reconciler from tracking to enforcing — the manifest-derived allowlist is already computed and stored in the preview file, so the toggle takes effect on the next deploy with no manual authoring.

### 3. Permission provenance — the two-axis state

Every entry the reconciler produces — whether written to `exec-approvals.json` (allowlist mode) or to `exec-approvals.preview.json` (full mode) — carries a `source` tag:

| Source | Set by | Mutability |
|---|---|---|
| `app-derived` | reconciler, from app manifest (inferred or explicit) | reconciler only; security generators may propose retiring the underlying *app* |
| `operator-set` | network.json `execPolicy` block, manual operator action, admin UI hardening toggle | reconciler leaves untouched; security generators may propose narrowing |
| `legacy` | pre-reconciler entries (e.g. security-bot's 29 entries today) | one-time migration to either app-derived or operator-set; flagged until classified |

This distinction is load-bearing once enforcement is on (allowlist mode) — it's what the security-generator contract in §6 operates against. In `full` mode the tags are still computed and stored in the preview file, so the data is ready the moment an operator opts into hardening; nothing has to be derived after the fact.

### 4. The new generator: `app_permission_drift`

A pure-Python generator (no LLM cost — fits [feedback_rsi_low_cost_preference](../memory/feedback_rsi_low_cost_preference.md)). Runs per-bot. Behavior depends on enforcement mode:

**In `allowlist` mode (enforcing) — fixes broken state:**

- **Declared-but-not-allowed.** An app declares a script that isn't in the allowlist. Proposal: run reconciler, enumerate which apps need which entries. Critical severity if the script is referenced from INSTALLED_APPS.md.
- **Allowed-but-not-declared.** An `app-derived` entry whose declaring app is gone. Proposal: remove the entry. `operator-set` entries are out of scope — those are deliberate.

**In `full` mode (tracking) — informational:**

- **Workspace script not declared in any manifest.** "Team-Bot-A has scripts/foo.py but no app declares it. Add to an existing app's `files:` or retire the script?" Doesn't break anything — the bot can still run it. Lets operator keep manifests honest.
- **Manifest declares an exec entry for a file that doesn't exist.** "App X's `permissions.exec` lists `tools/bar.py` but the file is gone." Stale declaration, propose removal from manifest.

In `full` mode these are visibility findings, not breakages. In `allowlist` mode the first category becomes critical-severity (the script genuinely can't run).

This is the inverse of the existing `auth_drift_filler` family. Those push toward a security baseline regardless of intent. This one pushes toward correctness as declared by intent — and adapts its urgency to whether enforcement is on.

### 5. The new generator: `app_permission_review`

`app_permission_drift` (§4) keeps exec-approvals.json in sync with manifests. But manifests themselves can rot — an app sheds scripts, renames files, grows a new capability — and the declarations don't reflect any of it. Without a second generator, drift faithfully reproduces stale declarations.

`app_permission_review` runs during the app scan (and as a standalone scheduled job at slower cadence). It examines whether each app's declared `permissions:` are still **necessary** (files/endpoints still referenced) and **sufficient** (everything actually used is declared). Pure Python, no LLM cost.

#### Static checks per app

For each app's `permissions:` block:

- **Necessary?**
  - `exec: <path>` → does the file exist? Does any script in the app reference it? If neither, propose removal.
  - `fs_read` / `fs_write: <path>` → grep all scripts in the app for the path. No matches → propose narrowing/removal.
  - `crons: <schedule>` → is a corresponding cron registered (launchd / @cron file)? No → propose removal.
  - `network_egress: <host>` → grep all scripts for the host. No matches → propose removal.

- **Sufficient?**
  - For every script in `files:` → covered by an exec entry (inferred or explicit)?
  - For every cron declared in a script's shebang or invoked config → in `crons:`?
  - For every endpoint hardcoded in a script → in `network_egress:`?

- **Overkill?** (lower-confidence)
  - Explicit entries that look wider than the scripts justify (e.g. `*.anthropic.com` when only `api.anthropic.com` is grep-able). Surfaces as `severity: info`, never auto-narrowed.

Each finding produces a proposal targeting **the manifest's `permissions:` block** — never `exec-approvals.json` directly. The reconciler (§2) re-derives the permission set on the next pass.

#### Pod-aware second pass — the consolidation step

A per-app finding can't be acted on in isolation. A permission an app no longer needs may still be needed by a sibling app on the same bot. Before any "remove" proposal is finalized, the generator runs a second pass over the bot's full manifest set:

1. **First pass — collect candidate findings per app.** Pure local analysis: "app A has `exec: scripts/foo.py` but the file is gone."
2. **Second pass — cross-reference against sibling apps.** For each candidate removal, ask: does any other installed app on this bot reference the same resource (declared in its `permissions:`, listed in its `files:`, or grep-matched in its scripts)?
   - **No sibling references it** → emit the removal proposal as-is.
   - **A sibling declares it** → emit the removal proposal annotated *"the permission remains in effect via app B's declaration; this removal narrows app A's declared surface but does not change the bot's exec-approvals.json."* Operator gets context without false alarm.
   - **A sibling uses it but doesn't declare it** → emit a **move proposal** instead: remove from app A's manifest AND add to app B's manifest. The reconciler then keeps exec-approvals.json unchanged but the declaration finds its rightful home. This also catches the "app B's manifest is incomplete" case as a side effect.
3. **Sufficient findings** ("app uses X but doesn't declare it") get the same cross-reference: if any other app already declares X, propose adding to *this* app's manifest with the annotation *"already declared by app C; this proposal makes the dependency explicit."*

The generator is per-app in scope of what it analyzes, but **bot-aware** in scope of what it proposes. This is the consolidation step.

#### Why the reconciler is still the final consolidator

`app_permission_review` proposes changes to manifests; it never writes `exec-approvals.json`. After proposals are applied:

1. Manifests reflect each app's correct declared needs.
2. Reconciler walks all apps, unions their declarations, writes the consolidated allowlist.
3. A permission survives in `exec-approvals.json` if and only if at least one app declares it.

The reconciler is the structural consolidator (the union operation). The review generator's job is to keep individual manifests honest **while accounting for sibling apps** so its proposals don't fight each other. Between them, the system handles both per-app accuracy and bot-level consolidation without either layer needing to do the other's job.

#### Operator-affirmed entries

Some explicit `permissions:` entries are deliberate operator decisions that won't satisfy static checks — e.g., a cron-only script the scan doesn't see invoked interactively. To avoid the generator pestering on every scan:

- When an operator rejects a removal proposal, the rejection is recorded as a per-entry affirmation in `permissions._affirmed: ["exec:scripts/foo.py"]` on the manifest.
- The generator skips affirmed entries on subsequent runs. Affirmation is per-entry (not per-app), so legitimate future findings on other entries still surface.
- Confidence floor as the first filter: only propose narrowing when static evidence is unambiguous (file missing AND no grep match anywhere in the workspace, not just in the app's own files). Avoids most false positives before affirmation is needed.

#### Cadence

- **Primary trigger:** runs as a phase of `scan_workspace_pipeline` after manifest generation. Operators expect findings when they explicitly scan.
- **Secondary trigger:** standalone scheduled job (cadence TBD — probably weekly) so apps don't go un-reviewed between operator-initiated scans. Same code path; identical findings.

#### Runtime-evidence audit — explicitly out of scope for v1

Static analysis catches structural drift (file gone, host never grep-able). It can't catch subtler cases like "this script exists and could run but hasn't been invoked in 90 days." That's runtime evidence — turn logs, OC exec event stream — and warrants a separate generator (`app_permission_usage_audit`) with a different cost profile and signal quality. Mentioned here for completeness; deferred to a later phase. See follow-ons under §6.

---

### 6. Revised security-generator contract

Security generators (`auth_drift_filler`, hypothetical `cron_caps_filler`, etc.) get a fundamentally repositioned role: **propose opt-in tightening, never silently enforce minimums.** Audience is always the operator. Security generators never write to `exec-approvals.json` or change policy modes; they emit proposals with full context, and the operator decides.

Three proposal shapes:

| Shape | When | Example |
|---|---|---|
| **"Downgrade to allowlist mode"** | Bot is in `full` mode and the would-be allowlist (preview file from §2) looks coherent | "Team-Bot-A has 7 apps with traceable scripts and crons. Switch to `allowlist` mode? The seed allowlist is here — preview before deciding." Audience: operator. |
| **"Narrow this operator-set entry"** | An `operator-set` entry looks wider than needed (wildcard host, broad exec command) | "Security-Bot has `network_egress: *.anthropic.com` but only `api.anthropic.com` is referenced. Narrow?" |
| **"App might be overgranted"** | An app's declared `permissions:` are wider than its scripts justify | Same as `app_permission_review` overkill findings; the security generator can co-fire on app-derived entries it judges suspicious. Targets the **manifest**, not exec-approvals. |

**Never:**

- Silent removal of any entry.
- Direct writes to `exec-approvals.json`.
- Proposals the operator can't see in context.
- Forcing `allowlist` mode on a bot the operator hasn't opted in.

This codifies the tug-of-war resolution: security generators don't get to win an argument with the app system by going around it. They get to *propose* tighter posture — operators decide. When the bot's actual operation depends on a permission, the security generator's job is to give the operator the information needed to choose intelligently, never to break the bot while the operator's back is turned.

#### Ordering constraint

Security-generator proposals that target app manifests (the "app might be overgranted" shape) only work once provenance tags from §3 are reliable. Earliest landing: Phase B. Until then, those generators are constrained to the "downgrade to allowlist" and "narrow operator-set" shapes.

### 7. Revised compliance chip

Today's chip on the Safety card flags `security="full" + empty allowlist` as orange "permissive." It misses the actually-broken state AND it pathologizes the right default.

The chip pivots from "are exec permissions tight?" to "is the bot's posture coherent?"

| State | Color | Meaning |
|---|---|---|
| `full` + manifests cover all scripts/crons in workspace | **green** | Default mode, complete visibility, working as intended |
| `full` + workspace scripts not declared in any manifest | yellow | Visibility gap — declare or retire (informational, bot still works) |
| `allowlist` + manifest-derived allowlist matches reality | **green** | Operator opted in to tighter posture, system in agreement |
| `allowlist` + declared-but-not-allowed entries | **red** | Broken — apps declared things the allowlist denies (the original incident shape) |
| `deny` + bot has apps with files/crons | **red** | Broken — team-bot-a/admin-bot/team-bot-b/personal-bot/team-bot-c's state today |
| `allowlist` + `operator-set` entries with wildcards | yellow | Overreach — operator should consider narrowing |
| `legacy` entries (security-bot's 29 unclassified) | grey | Migration pending; not broken |

**Default for member bots is green.** Tightening is an operator choice, celebrated when chosen, not demanded by the chip. The chip's job is to surface broken states (red) and visibility gaps (yellow), not to enforce a doctrine about which mode is "correct."

The chip becomes a real-time read of the reconciler's output, not an independent check. Implementation: derived in `evaluate_exec_policy_compliance` ([upstream_version.py](../packages/admin/evolve_admin/upstream_version.py)), which today returns `compliant: bool` — extend to return `state` from the table above plus the reasoning string.

### 8. Orphan scripts on disk

Scripts in the bot's workspace that aren't declared by any app manifest behave differently by mode:

- **`full` mode (default):** the bot can run them. They're orphan only from a *manifest* standpoint. A Signal (`workspace_orphan_script`) surfaces them informationally: "team-bot-a has these `.py` files not declared in any app — bind, retire, or note as operator-set." Operator can act or ignore; nothing breaks.
- **`allowlist` mode:** orphans are not in the allowlist, so they can't run. Same Signal fires with higher severity ("these scripts exist but won't execute under current policy").

Either way the reconciler doesn't silently auto-grant orphans — declaration in a manifest is what gives a script a recognized home. The `full` default just means undeclared scripts aren't a *breakage*, only a *visibility gap*.

---

## Migration plan

### Phase A: flip the default + ship the tracking reconciler

**Goal:** end the team-bot-a Slack failure and the OC-migrator reversion loop. Member bots get exec back on the next deploy.

- Change `_infer_exec_policy` ([deploy.py:1079](../packages/admin/evolve_admin/deploy.py:1079)) member-bot default from `"deny"` to `"full"`. Primary-bot `"deny"` rule unchanged. Operator-set `execPolicy` Priority-1 override unchanged.
- Land `reconciler.reconcile_bot_permissions(...)` in tracking mode. Reads manifests, computes the would-be allowlist with provenance, writes to `exec-approvals.preview.json` only. No writes to `exec-approvals.json` for member bots in this phase.
- Adjust `evaluate_exec_policy_compliance` to the new chip semantics (§7). `full + manifests honest` is green.
- On deploy, the OC v5.18 migrator's drift heals itself: team-bot-a/admin-bot/team-bot-b/personal-bot/team-bot-c land at `full`, the previous `deny` state is gone, and the Slack failure pattern stops.
- Estimated 1 PR. Reversible — if `full` proves wrong for any bot the operator can set `execPolicy: "allowlist"` in network.json (Priority 1) to force allowlist mode immediately.

### Phase B: app review + drift generators, manifest-completeness pass

**Goal:** keep manifests honest now that they're load-bearing for visibility and opt-in tightening.

- Land `app_permission_review` (§5) as part of `scan_workspace_pipeline`. Findings flow into proposals.
- Land `app_permission_drift` (§4) as a scheduled job; runs as informational in `full` mode, critical in `allowlist`.
- Watch the admin UI: for each member bot, does the preview allowlist match what the bot is actually doing? Discrepancies surface as review proposals; operator approves or rejects.
- One-week soak.

### Phase C: opt-in hardening UI

**Goal:** give operators the one-click downgrade-to-allowlist path the principle promised.

- Admin UI toggle on each bot's Safety card: "Switch to allowlist mode" with the manifest-derived allowlist visible before the operator commits.
- Toggle flips `execPolicy: "allowlist"` in network.json (Priority 1 override). Next deploy: reconciler shifts from tracking to enforcing, the preview file becomes the live `exec-approvals.json`.
- Security-Bot's 29 `legacy` entries get classified during this phase — most become `operator-set` (they're sysadmin commands, not from an app); a few may bind to app manifests if the operator chooses to create them. Reconciler then handles security-bot uniformly with other allowlist-mode bots.
- Tested but not forced on anyone; member bots stay at `full` unless the operator opts in.

### Phase D: deprecate the network.json `execPolicy` Priority-1 override

After Phase C, the `execPolicy` override is reachable through the admin UI toggle, which writes to network.json on the operator's behalf. The raw override stays operator-editable but isn't the primary path anymore. Optional, can wait indefinitely.

### What this migration does NOT do

- It does not change security-bot. Security-Bot was already in `allowlist` mode with 29 entries before this work; it stays there. The only security-bot-touching step is the `legacy` → classified reclassification in Phase C.
- It does not change evo or any primary bot. `deny` is correct for primary bots and stays.
- It does not force any operator to harden. The pivot's core promise is that member bots work out of the box; hardening is opt-in, never the default.

---

## Resolved

1. ~~Where do explicit `permissions:` blocks live?~~ **Resolved 2026-05-24.** In the manifest, as a normal top-level `permissions:` field. Protected from auto-overwrite by the `AUTO_MANAGED_FIELDS` registry (§1). Preserves single-document-per-app shareability and matches the existing field-aware merge pattern.

2. ~~Apps autonomously installed by the bot — auto-grant or propose?~~ **Resolved 2026-05-24, pivoted 2026-05-25.** With the policy default changing to `full` for member bots, "auto-grant" is no longer the question for everyday flows. The bot can run anything in its own workspace by default. What the resolution still pins down:

   - **`full` mode (member bot default):** the bot's workspace scripts run without any manifest required. The "build me a protein tracker" flow works end-to-end. Manifests are still expected for visibility/sharing/audit, but they don't gate exec.
   - **`allowlist` mode (operator opt-in, or security-bot):** here the auto-grant question matters. Rule: manifest-declared files in `files:` with `layer == "script"` (the stamper tag) auto-enter the allowlist on the next reconciler pass. Test files, data files, and docs in `files:` are not auto-granted. Outside-workspace declarations (`fs_read: /Users/Shared/...`, `network_egress: <new host>`, sudo-form commands) always require an operator proposal — never auto-applied, regardless of mode.
   - **Outside-workspace, all modes:** even in `full` mode, the bot can't auto-grant itself anything beyond its own user account. Cross-user paths, system commands, and new network egress hosts always go through proposal-for-approval. This is the boundary the pivot does *not* cross.

3. ~~Can security generators propose tightening app declarations?~~ **Resolved 2026-05-24.** Yes — and the proposal target is the **app manifest's `permissions:` block**, never `exec-approvals.json` directly. Reconciler re-derives on next pass. **Ordering constraint:** this only works once provenance tags from §3 are in place. Phase A computes provenance into the preview file; Phase B is the earliest moment security-generator proposals against app manifests can land and be evaluated against reliable data.

4. ~~Reconciler behavior on partial app failure?~~ **Resolved 2026-05-24.** Per-app skip, not per-bot abort:
   - **Malformed single manifest** (JSON parse error, schema-version mismatch, missing required fields): leave that app's existing `exec-approvals.json` entries alone. Reconcile every other app normally. Signal severity `warn`, surfaces in the admin UI as "app X manifest unparseable; permissions for that app frozen pending fix."
   - **Catastrophic bot-level state** (workspace unreadable, `exec-approvals.json` itself unparseable, write failure to the bot's config dir): abort reconciliation for the bot entirely. Existing state untouched. Signal severity `error`.
   - Rationale: one broken app shouldn't punish the nine working ones, *especially* if the broken state is the very pod-wide-deny incident this spec exists to prevent.

5. ~~Land fs / network alongside exec, or stage?~~ **Resolved 2026-05-24.** Stage with explicit advisory labeling:
   - **Phase A–C ship `exec` only** with full reconciler enforcement (OC enforces `tools.exec.*` today).
   - **Schema reserves `fs_read`, `fs_write`, `network_egress`, `env`** from day one so future apps can declare them. The reconciler reads them and uses them for static review-generator checks and audit trail. They are explicitly labeled **advisory** in the schema docstring — they do not currently produce OC runtime enforcement.
   - **OC enforcement reality, as of this writing:** `tools.exec.*` enforced; `tools.fs.workspaceOnly: true|false` exists but is coarse (no per-path policy); per-host network policy not present in upstream. Spec docs and admin UI both label fs/network entries as "advisory — declared and audited, enforcement pending upstream."
   - **Promotion to enforced** happens when OC ships the corresponding policy primitive. No schema breakage at that point — the reconciler just starts writing the new field through.

## Open questions

6. ~~**What classifies a `files:` entry as a script for the purposes of Q2's auto-grant?**~~ **Resolved 2026-05-25 by the pre-Phase-A audit on the mini.** Pod-wide tally across 7 bots × 61 manifests × 270 file entries: **0** entries carry a `layer` tag. 100% of member-bot manifests are v7-arc instances (`schema=14, manifest_shape="v7-arc"`), and v7-arc records in `realized_files[]` don't carry the `layer` field at all — only `logical_name`, `path`, `file_id`, `marker_state`. The original "what if a `.py` is stamped layer=data" worry doesn't materialize because there is no stamping happening here.

   **Resolution:** classification falls entirely back to the path-extension rule (`.py` / `.sh` / `.bash` / `.zsh`). The reconciler reads both `files[]` (legacy v4/v5 manifests) AND `realized_files[]` (v7-arc instances) and classifies by extension; the `layer == "script"` short-circuit is retained as a non-load-bearing fast path for any legacy manifest that does carry the tag (none currently on the pod outside one evolve-account file).

   **Sanity check on the script set after the v7-arc fix:** 97 would-be exec entries pod-wide, including `team-bot-a/ops/tools/unified_task_system.py` — the exact script from the 2026-05-24 Slack failure that motivated this spec. Distribution: personal-bot-user 5, evolve 1 (primary — no preview), team-bot-a 10, team-bot-c 26, personal-bot 1, admin-bot 35, security-bot 19.

   **Side-finding from the same audit:** 0 cron entries declared in `crons[]` pod-wide despite at least team-bot-a, admin-bot, and security-bot having heartbeat-driven scheduled behaviors. The scanner populates `scheduled_actions` but not `crons[]`. Doesn't block Phase A (the reconciler still emits exec entries for script files); tracked as a follow-on before any cron-policy enforcement work.

   **Not-yet-handled corner case for Phase C's auto-grant rule (tracking only in Phase A):** test files (e.g. `admin-bot/tests/test_gmail_fetch.py`) classify as scripts under extension alone. Phase C's opt-in flow will need an `auto_grant_exclusions` path rule (likely `tests/`, `__pycache__/`, anything stamped `OWNED_BY_TEST`) or operator pre-commit review of the preview before flipping to allowlist mode.

---

## Out of scope

- LLM seeing exec-denied as a structured tool result. That's an upstream OC concern; we can't fix the confabulating-success pattern without OC surfacing the deny in a way the model can integrate. Worth filing upstream once the structural fix lands; tracked as a follow-on, not a blocker here.
- Audit-time policy enforcement (eBPF / signed manifests). Doesn't exist upstream; see [spec-upstream-oc-exec-policy-adoption-2026-05-12.md](spec-upstream-oc-exec-policy-adoption-2026-05-12.md).
- Per-channel permission overrides (different exec policy for Slack vs. Telegram). Not a current need; would be additive on top of this design if it ever becomes one.

---

## Success criteria

This design has worked when:

1. **Every member bot's compliance chip is green by default.** Tightening is an operator choice, not a system requirement, and the chip celebrates a coherent posture in whichever mode the operator chose.
2. **The "build me a protein tracker" flow works end-to-end** with no manifest authoring or permission approval required of the user. The bot writes a script, registers a cron, and the cron fires successfully at 6 PM the same day.
3. **The Slack failure pattern goes to zero** — no confabulated-success + `🔨 failed` chip for a script the bot was just asked to use.
4. **A subsequent OC upstream migration that re-flips `tools.exec.security` self-heals on the next deploy** because `_infer_exec_policy` re-derives the correct default (`full` for member bots).
5. **Operators who want hardening get a one-click path** — the admin UI toggle on the Safety card with the manifest-derived allowlist visible before commit. The opt-in is low-friction enough that operators who care about posture actually use it.
6. **Manifests reflect what the bot actually does.** Review and drift generators keep them honest; sharing, audit, and opt-in tightening all rely on this without anyone having to maintain manifests by hand.
7. **Security generators are useful, not adversarial.** They propose hardening with full context; operators evaluate. Member bots never break because a security generator silently denied something the app system declared.
